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
    assert cli(pg, "--import-id", "99999").returncode == 1


@pytest.mark.parametrize("status", ["failed", "cancelled", "canceled", "unknown"])
def test_terminal_job_states_fail_even_when_import_completed(pg, status):
    iid = add_import(pg[0], job=status)
    lines = []
    assert run_load_status(pg[0], wait=True, import_ids=[iid], emit=lines.append) == 1
    assert any(status in line for line in lines)


def test_running_without_job_and_unsubmitted_are_not_pass(pg):
    iid = add_import(pg[0], "running")
    assert run_load_status(pg[0], wait=True, import_ids=[iid]) == 1
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


def test_unresolved_orphan_fails_and_shared_child_failure_is_seen(pg):
    engine = pg[0]
    iid = add_import(engine)
    with engine.begin() as db:
        db.execute(text("""
            INSERT INTO source_records (source_id,external_id,raw_hash,last_import_id)
            SELECT id,'pending','hash',:iid FROM sources LIMIT 1
        """), {"iid": iid})
    assert run_load_status(engine, wait=True, import_ids=[iid]) == 1
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
                           clock=lambda: ticks[0], sleep=sleep, emit=lines.append) == 1
    assert ticks[0] == 5 and "Timed out" in lines[-1]
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