"""CLI integration against a private migrated PostgreSQL cluster; no app DB writes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest
from sqlalchemy import create_engine, text

from app.load_status import run_load_status


@pytest.fixture(scope="module")
def private_pg():
    for binary in ("initdb", "pg_ctl"):
        if not shutil.which(binary):
            pytest.skip(f"Local PostgreSQL required: {binary}")
    with tempfile.TemporaryDirectory(prefix="kinship-status-") as directory:
        root = Path(directory)
        data = root / "data"
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        env.update(DATABASE_URL=f"postgresql+psycopg://monitor@/postgres?host={root}",
                   APP_ENV="test", AUTH_MODE="dev")
        subprocess.run(["initdb", "-D", str(data), "-U", "monitor", "-A", "trust",
                        "--no-locale", "--encoding=UTF8"], env=env, check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["pg_ctl", "-D", str(data), "-l", str(root / "pg.log"),
                        "-o", f"-k {root} -h ''", "-w", "start"],
                       env=env, check=True, stdout=subprocess.DEVNULL)
        engine = create_engine(env["DATABASE_URL"])
        try:
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                           cwd=Path(__file__).resolve().parents[1], env=env, check=True,
                           stdout=subprocess.DEVNULL)
            yield engine, env
        finally:
            engine.dispose()
            subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                           env=env, check=True, stdout=subprocess.DEVNULL)


@pytest.fixture
def pg(private_pg):
    engine, env = private_pg
    with engine.begin() as db:
        db.execute(text("TRUNCATE sources, jobs RESTART IDENTITY CASCADE"))
        db.execute(text("""
            INSERT INTO sources (key,name,kind,record_types,priority,is_active)
            VALUES ('status-test','Status test','csv',ARRAY['contact'],10,true)
        """))
    return engine, env


def add_import(engine, status="completed", job=None):
    with engine.begin() as db:
        iid = db.scalar(text("""
            INSERT INTO imports (source_id,filename,record_type,status,rows_total,
                rows_ok,rows_rejected,warning_count,duration_ms,started_at,finished_at)
            SELECT id,'status.csv','contact',:status,12,10,2,3,2000,
                now() - interval '2 seconds',
                CASE WHEN :status='completed' THEN now() ELSE NULL END
            FROM sources LIMIT 1 RETURNING id
        """), {"status": status})
    if job:
        add_job(engine, "import.run", job, {"import_id": iid})
    return iid


def add_job(engine, kind, status, payload=None):
    with engine.begin() as db:
        return db.scalar(text("""
            INSERT INTO jobs (type,status,payload,priority,attempts,max_attempts,progress)
            VALUES (:kind,:status,CAST(:payload AS jsonb),100,0,3,'{}') RETURNING id
        """), {"kind": kind, "status": status, "payload": json.dumps(payload or {})})


def cli(pg, *args):
    return subprocess.run([sys.executable, "-m", "app.cli", "load-status", *args],
                          cwd=Path(__file__).resolve().parents[1], env=pg[1],
                          capture_output=True, text=True, timeout=30)


def test_cli_empty_completed_scoping_and_metrics(pg):
    result = cli(pg, "--wait")
    assert result.returncode == 1 and "No imports selected" in result.stdout
    iid = add_import(pg[0])
    add_import(pg[0], "failed")
    result = cli(pg, "--import-id", str(iid), "--wait")
    assert result.returncode == 0, result.stderr
    for value in ("PASS", "ok=10 rejected=2 warn=3", "rows/s=6.0",
                  "profiles=0 merges=0 gifts=0", "total_wallclock=2.0s"):
        assert value in result.stdout
    assert cli(pg).returncode == 1
    assert cli(pg, "--import-id", "99999").returncode == 4


@pytest.mark.parametrize("status", ["failed", "cancelled", "canceled", "unknown"])
def test_terminal_job_states_fail_even_when_import_completed(pg, status):
    iid = add_import(pg[0], job=status)
    lines = []
    assert run_load_status(pg[0], wait=True, import_ids=[iid], emit=lines.append) == (
        4 if status == "unknown" else 1)
    assert any(status in line for line in lines)


def test_running_without_job_and_unsubmitted_are_not_pass(pg):
    iid = add_import(pg[0], "running")
    assert run_load_status(pg[0], wait=True, import_ids=[iid]) == 4
    iid = add_import(pg[0], "uploaded")
    assert run_load_status(pg[0], wait=True, import_ids=[iid]) == 1


def test_fakeclock_queued_running_child_progress_completion(pg):
    engine = pg[0]
    iid = add_import(engine, "running", "queued")
    child = add_job(engine, "identity.resolve_batch", "queued")
    ticks = [0]
    lines = []

    def sleep(seconds):
        ticks[0] += seconds
        with engine.begin() as db:
            if ticks[0] == 30:
                db.execute(text("UPDATE jobs SET status='running'"))
            if ticks[0] == 32:
                db.execute(text("UPDATE imports SET status='completed', finished_at=now()"))
                db.execute(text("UPDATE jobs SET status='succeeded',finished_at=now() "
                                "WHERE type='import.run'"))
            if ticks[0] == 62:
                db.execute(text("UPDATE jobs SET status='succeeded',finished_at=now() WHERE id=:id"),
                           {"id": child})

    assert cli(pg, "--import-id", str(iid)).returncode == 2
    assert run_load_status(engine, wait=True, import_ids=[iid], clock=lambda: ticks[0],
                           sleep=sleep, emit=lines.append) == 0
    assert ticks[0] == 62
    assert len([line for line in lines if line.startswith("Load status:")]) == 4
    assert any("monitored=30.0s" in line for line in lines)
    assert any("monitored=60.0s" in line for line in lines)


def test_unresolved_orphan_unknown_and_shared_child_failure_is_seen(pg):
    engine = pg[0]
    iid = add_import(engine)
    with engine.begin() as db:
        db.execute(text("""
            INSERT INTO source_records (source_id,external_id,raw_hash,last_import_id)
            SELECT id,'pending','hash',:iid FROM sources LIMIT 1
        """), {"iid": iid})
    assert run_load_status(engine, wait=True, import_ids=[iid]) == 4
    child = add_job(engine, "identity.resolve_batch", "queued")
    ticks = [0]

    def sleep(seconds):
        ticks[0] += seconds
        with engine.begin() as db:
            db.execute(text("UPDATE jobs SET status='cancelled' WHERE id=:id"), {"id": child})

    assert run_load_status(engine, wait=True, import_ids=[iid], clock=lambda: ticks[0],
                           sleep=sleep) == 1
    assert ticks[0] == 2


def test_timeout_delayed_queue_and_latest_retry(pg):
    engine = pg[0]
    iid = add_import(engine, "running", "queued")
    ticks = [0]
    lines = []
    def sleep(seconds):
        ticks[0] += seconds
    assert run_load_status(engine, wait=True, import_ids=[iid], timeout=5,
                           clock=lambda: ticks[0], sleep=sleep, emit=lines.append) == 4
    assert ticks[0] == 5 and any("Monitoring timeout" in line for line in lines)
    with engine.begin() as db:
        db.execute(text("UPDATE jobs SET status='failed'"))
        db.execute(text("UPDATE imports SET status='completed',finished_at=now()"))
    add_job(engine, "import.run", "succeeded", {"import_id": iid})
    add_job(engine, "identity.resolve_batch", "failed")
    assert run_load_status(engine, wait=True, import_ids=[iid]) == 0


def test_completed_import_waits_for_child_queued_before_completion(pg):
    engine = pg[0]
    iid = add_import(engine, "running", "succeeded")
    add_job(engine, "identity.resolve_batch", "queued")
    with engine.begin() as db:
        db.execute(text("UPDATE imports SET status='completed',finished_at=now()"))
    assert run_load_status(engine, import_ids=[iid]) == 2


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_explicit(timeout):
    with pytest.raises(ValueError, match="positive"):
        run_load_status(None, timeout=timeout)


@pytest.mark.parametrize("fresh,expected", [(True, 3), (False, 4)])
def test_timeout_fresh_heartbeat_is_not_failure(pg, fresh, expected):
    engine = pg[0]
    iid = add_import(engine, "running", "running")
    with engine.begin() as db:
        db.execute(text("UPDATE jobs SET heartbeat_at=now() - "
                        "make_interval(secs => :age)"), {"age": 0 if fresh else 600})
    ticks, lines = [0], []
    result = run_load_status(engine, wait=True, import_ids=[iid], timeout=5,
                             clock=lambda: ticks[0],
                             sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds),
                             emit=lines.append)
    assert result == expected
    assert not any("FAIL" in line for line in lines)
    assert any("STILL RUNNING" in line for line in lines) == fresh
    assert f"--import-id {iid}" in lines[-1]


def test_gift_event_source_records_are_not_unresolved_contacts(pg):
    engine = pg[0]
    gift, event = add_import(engine), add_import(engine)
    with engine.begin() as db:
        db.execute(text("UPDATE imports SET record_type='gift' WHERE id=:id"), {"id": gift})
        db.execute(text("UPDATE imports SET record_type='event' WHERE id=:id"), {"id": event})
        for iid in (gift, event):
            db.execute(text("""
                INSERT INTO source_records(source_id,external_id,raw_hash,last_import_id)
                SELECT id,:external,'hash',:iid FROM sources LIMIT 1
            """), {"iid": iid, "external": f"transaction-{iid}"})
        db.execute(text("""
            INSERT INTO gifts(source_id,external_id,amount)
            SELECT id,'unmatched',10 FROM sources LIMIT 1
        """))
    lines = []
    assert run_load_status(engine, import_ids=[gift, event], emit=lines.append) == 0
    assert any("unresolved_contacts=0" in line and "unattached_gifts=1" in line
               and "unattached_events=0" in line for line in lines)


def test_metrics_are_cached_not_counted_every_poll(pg, monkeypatch):
    from app import load_status
    engine = pg[0]
    iid = add_import(engine, "running", "queued")
    real_metrics, calls = load_status.metrics, []
    def counted(*args):
        calls.append(1)
        return real_metrics(*args)
    monkeypatch.setattr(load_status, "metrics", counted)
    ticks = [0]
    assert run_load_status(engine, import_ids=[iid], wait=True, timeout=65,
                           clock=lambda: ticks[0],
                           sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds),
                           emit=lambda _: None) == 4
    assert len(calls) == 1


def test_batch_requires_final_traits_and_ignores_other_batch(pg):
    engine = pg[0]
    iid = add_import(engine)
    with engine.begin() as db:
        bid = db.scalar(text("INSERT INTO identity_batches(phase) VALUES('completed') RETURNING id"))
        other = db.scalar(text("INSERT INTO identity_batches(phase) VALUES('completed') RETURNING id"))
        db.execute(text("UPDATE imports SET identity_batch_id=:bid WHERE id=:iid"),
                   {"bid": bid, "iid": iid})
    add_job(engine, "identity.resolve_batch", "succeeded", {"batch_id": bid})
    add_job(engine, "identity.resolve_batch", "failed", {"batch_id": other})
    assert run_load_status(engine, import_ids=[iid]) == 4
    trait = add_job(engine, "traits.recompute", "queued", {"batch_id": bid})
    assert run_load_status(engine, import_ids=[iid]) == 2
    with engine.begin() as db:
        db.execute(text("UPDATE jobs SET status='succeeded' WHERE id=:id"), {"id": trait})
    assert run_load_status(engine, import_ids=[iid]) == 0


def test_phase_telemetry_commits_independently_and_reports(pg):
    from app.jobs.phase_progress import begin_phase, finish_phase
    from app.load_status import phase_details
    engine = pg[0]
    jid = add_job(engine, "identity.resolve_batch", "running")
    begin_phase(engine, jid, "resolving")
    begin_phase(engine, jid, "attaching")
    with engine.connect() as db:
        row = dict(db.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": jid}).mappings().one())
    phase, elapsed = phase_details([row])
    assert phase == "attaching" and elapsed["resolving"] >= 0
    assert elapsed["attaching"] >= 0
    finish_phase(engine, jid)
    with engine.connect() as db:
        progress = db.scalar(text("SELECT progress FROM jobs WHERE id=:id"), {"id": jid})
    assert progress["phase"] is None
    assert set(progress["phase_elapsed_ms"]) == {"resolving", "attaching"}


def test_completed_five_imports_and_live_import_elapsed():
    from datetime import datetime, timedelta, timezone
    from app.load_status import phase_details
    now = datetime.now(timezone.utc)
    jobs = [
        {"type": "import.run", "status": "succeeded", "progress": {},
         "started_at": now - timedelta(seconds=20), "finished_at": now - timedelta(seconds=10)}
        for _ in range(5)
    ]
    phase, elapsed = phase_details(jobs)
    assert phase == "none"
    assert elapsed["importing"] == 50  # Actual job-seconds, not overlapping wall time.
    jobs.append({"type": "import.run", "status": "running", "progress": {},
                 "started_at": now - timedelta(seconds=5), "finished_at": None})
    phase, elapsed = phase_details(jobs)
    assert phase == "importing"
    assert 55 <= elapsed["importing"] < 57


def test_database_totals_explicitly_label_cached_age(pg):
    from app.load_status import metrics, report, snapshot
    engine = pg[0]
    iid = add_import(engine)
    imports, jobs = snapshot(engine, [iid], set())
    lines = []
    report(imports, jobs, metrics(engine, [iid]), "PASS", "Done", 90,
           lines.append, metrics_age=90)
    assert any(line.startswith("Database totals:") and "cached_metrics_age=90s" in line
               for line in lines)


def test_phase_retry_does_not_charge_one_hour_downtime(pg):
    from app.jobs.phase_progress import begin_phase, finish_phase, record_attachment_step
    from app.load_status import phase_details
    engine = pg[0]
    jid = add_job(engine, "identity.resolve_batch", "running")
    with engine.begin() as db:
        db.execute(text("UPDATE jobs SET attempts=1,started_at=now()-interval '1 hour' WHERE id=:id"),
                   {"id": jid})
    begin_phase(engine, jid, "resolving")
    begin_phase(engine, jid, "attaching")
    record_attachment_step(engine, jid, "gifts", "completed", 1234)
    with engine.begin() as db:
        # Interrupted attempt leaves an old open interval; no finish callback.
        db.execute(text("""
            UPDATE jobs SET progress=jsonb_set(progress,'{phase_started_at}',
                to_jsonb((now()-interval '1 hour')::text)),
                attempts=2,started_at=now() WHERE id=:id
        """), {"id": jid})
    begin_phase(engine, jid, "resolving")
    with engine.connect() as db:
        row = dict(db.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": jid}).mappings().one())
    progress = row["progress"]
    assert progress["attempt"] == 2 and progress["phase_elapsed_ms"] == {}
    assert "attachment_steps" not in progress and "attachment_step" not in progress
    assert progress["phase_history"][-1]["attempt"] == 1
    assert progress["phase_history"][-1]["incomplete_phase"] == "attaching"
    assert progress["phase_history"][-1]["attachment_steps"]["gifts"]["elapsed_ms"] == 1234
    phase, elapsed = phase_details([row])
    assert phase == "resolving" and 0 <= elapsed["resolving"] < 5
    assert "attaching" not in elapsed
    finish_phase(engine, jid)
    with engine.connect() as db:
        progress = db.scalar(text("SELECT progress FROM jobs WHERE id=:id"), {"id": jid})
    assert 0 <= progress["phase_elapsed_ms"]["resolving"] < 5000


def test_legacy_progress_has_unknown_duration_and_is_reset_on_first_step(pg):
    from app.jobs.phase_progress import record_attachment_step
    from app.load_status import phase_details
    engine = pg[0]
    jid = add_job(engine, "identity.resolve_batch", "running")
    with engine.begin() as db:
        db.execute(text("""
            UPDATE jobs SET attempts=2,started_at=now(),progress=jsonb_build_object(
                'phase','attaching','phase_started_at',(now()-interval '1 hour')::text,
                'phase_elapsed_ms',jsonb_build_object('resolving',3600000))
            WHERE id=:id
        """), {"id": jid})
        row = dict(db.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": jid}).mappings().one())
    _, elapsed = phase_details([row])
    assert elapsed == {}  # Legacy attempt ownership is explicitly unknown.
    record_attachment_step(engine, jid, "events", "completed", 10)
    with engine.connect() as db:
        progress = db.scalar(text("SELECT progress FROM jobs WHERE id=:id"), {"id": jid})
    assert progress["attempt"] == 2 and progress["phase_elapsed_ms"] == {}
    assert "phase_started_at" not in progress
    assert progress["phase_history"][-1]["timing_scope"] == "unknown_legacy"
    assert set(progress["attachment_steps"]) == {"events"}


def test_automatic_cohort_membership_refreshes_while_import_selection_is_fixed(pg):
    """A running standalone wakeup gains its batch and shared traits child."""
    engine = pg[0]
    selected, peer = add_import(engine), add_import(engine)
    resolver = add_job(engine, "identity.resolve_batch", "running", {"import_id": selected})
    wakeup = add_job(engine, "identity.resolve_batch", "queued", {"import_id": peer})
    ticks, lines, cohort = [0], [], {}

    def sleep(seconds):
        ticks[0] += seconds
        if ticks[0] == 2:
            # Simulate the atomic commit of the first automatic cohort pass.
            with engine.begin() as db:
                batch = db.scalar(text("""
                    INSERT INTO identity_batches(phase,finished_at)
                    VALUES('completed',now()) RETURNING id
                """))
                db.execute(text("UPDATE imports SET identity_batch_id=:bid WHERE id=ANY(:ids)"),
                           {"bid": batch, "ids": [selected, peer]})
                db.execute(text("""
                    UPDATE jobs SET payload=payload || jsonb_build_object('batch_id',CAST(:bid AS bigint))
                    WHERE id=ANY(:ids)
                """), {"bid": batch, "ids": [resolver, wakeup]})
                db.execute(text("UPDATE jobs SET status='succeeded',finished_at=now() WHERE id=:id"),
                           {"id": resolver})
                cohort["traits"] = db.scalar(text("""
                    INSERT INTO jobs(type,status,payload,dedupe_key,priority,attempts,max_attempts,progress)
                    VALUES('traits.recompute','queued',jsonb_build_object('batch_id',CAST(:bid AS bigint)),
                           :key,100,0,3,'{}') RETURNING id
                """), {"bid": batch, "key": f"traits:batch:{batch}"})
            # New unrelated import must NOT silently enter the frozen selection.
            add_import(engine, "failed")
        elif ticks[0] == 4:
            with engine.begin() as db:
                db.execute(text("UPDATE jobs SET status='succeeded',finished_at=now() WHERE id=:id"),
                           {"id": wakeup})
                db.execute(text("UPDATE jobs SET status='running' WHERE id=:id"),
                           {"id": cohort["traits"]})
        elif ticks[0] == 6:
            with engine.begin() as db:
                db.execute(text("UPDATE jobs SET status='succeeded',finished_at=now() WHERE id=:id"),
                           {"id": cohort["traits"]})

    assert run_load_status(engine, wait=True, import_ids=[selected], timeout=10,
                           clock=lambda: ticks[0], sleep=sleep, emit=lines.append,
                           progress_interval=2) == 0
    assert ticks[0] == 6  # Not the identity completion at 2 or noop completion at 4.
    assert any("monitored=4.0s; phase=traits" in line for line in lines)
    assert not any(line.startswith(f"  import {peer} ") for line in lines)
    assert lines[-1].startswith("PASS:")


def test_seed_loader_forwards_only_its_ids_once(pg, tmp_path, monkeypatch):
    from app import importer
    from app import load_status
    from app.seed import generate_seed, load_generated_files
    engine = pg[0]
    add_import(engine, "failed")
    generated = generate_seed(profiles=1, output_dir=tmp_path)
    calls = []
    monkeypatch.setattr(importer, "engine", engine)
    monkeypatch.setattr(importer, "get_settings", lambda: type("Settings", (), {"upload_dir": str(tmp_path)})())
    def wait(engine, **kwargs):
        calls.append(kwargs)
        # Worker simulation, isolated DB only.
        with engine.begin() as db:
            db.execute(text("UPDATE imports SET status='completed' WHERE id=ANY(:ids)"),
                       {"ids": kwargs["import_ids"]})
        return 0
    monkeypatch.setattr(load_status, "run_load_status", wait)
    ids = load_generated_files(generated["files"])
    assert len(calls) == 1 and calls[0]["wait"] is True
    assert len(ids) == 5 and calls[0]["import_ids"] == ids
    with engine.connect() as db:
        assert db.scalar(text("SELECT status FROM imports WHERE id=1")) == "failed"


def test_seed_cli_load_does_not_wait_twice(pg, tmp_path, monkeypatch):
    from app import cli as cli_module
    from app import seed
    calls = []
    monkeypatch.setattr(sys, "argv", [
        "kinship", "seed", "--profiles", "1", "--output-dir", str(tmp_path), "--load"])
    monkeypatch.setattr(seed, "load_generated_files", lambda files: calls.append("load") or [8, 9])
    monkeypatch.setattr(cli_module, "_compute_seed_traits",
                        lambda ids: calls.append(("traits", ids)))
    cli_module.main()
    assert calls == ["load", ("traits", [8, 9])]