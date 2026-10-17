"""Real PostgreSQL and OS signals; every test uses a disposable database."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from test_reset_demo import reset_engine
from app.jobs import lifecycle, queue
from app.reset_demo import safe_query_preview


PROBE = """
import threading
from sqlalchemy import text
from sqlalchemy.orm import Session
from app import worker
from app.db import engine
def sql():
    with engine.connect() as c:
        assert c.scalar(text("SELECT setting::int FROM pg_settings WHERE name='statement_timeout'")) == 60000
        assert c.scalar(text("SELECT setting::int FROM pg_settings WHERE name='idle_in_transaction_session_timeout'")) == 120000
        assert c.scalar(text("SHOW tcp_keepalives_idle")) in ("30", "0")
        assert c.scalar(text("SHOW application_name")).startswith("kinship-worker-")
        c.execute(text(__import__('os').environ["PROBE_SQL"]))
def run(*args, **kwargs):
    t = threading.Thread(target=sql, daemon=True)
    t.start()
    sql()
    t.join()
worker.handlers.run = run
worker.child_main()
"""


def wait_for(callback, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for worker lifecycle condition")


@pytest.mark.parametrize("supervisor", [False, True], ids=["child", "spawn-supervisor"])
@pytest.mark.parametrize("blocked", [False, True], ids=["long-sql", "advisory-wait"])
def test_sigterm_cancels_every_owned_helper_and_retries_without_touching_api(reset_engine, tmp_path, supervisor, blocked):
    engine = reset_engine
    with Session(engine) as db:
        job = queue.enqueue(db, "noop")
        job_id = job.id
        db.commit()
    env = dict(os.environ, DATABASE_URL=engine.url.render_as_string(hide_password=False))
    env["PROBE_SQL"] = "SELECT pg_advisory_xact_lock(967413)" if blocked else "SELECT pg_sleep(90)"
    unrelated = engine.connect()
    if blocked:
        unrelated.execute(text("SELECT pg_advisory_xact_lock(967413)"))
    command = [sys.executable, "-c", PROBE]
    if supervisor:
        probe = tmp_path / "worker_probe.py"
        probe.write_text(PROBE.replace("worker.child_main()", "if __name__ == '__main__':\n    worker.main()"))
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        env["WORKER_CONCURRENCY"] = "1"
        command = [sys.executable, str(probe)]
    process = subprocess.Popen(command, env=env,
                               cwd=Path(__file__).resolve().parents[1],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        unrelated.execute(text("SET application_name='kinship-api'"))
        if not blocked:
            unrelated.commit()
        unrelated_pid = unrelated.scalar(text("SELECT pg_backend_pid()"))
        def ready():
            with engine.connect() as db:
                return db.scalar(text("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname=current_database() AND application_name LIKE 'kinship-worker-%'
                          AND query=:query AND state='active'
                """), {"query": env["PROBE_SQL"]}) >= 2
        wait_for(ready)
        start = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
        assert time.monotonic() - start < 15
        with engine.connect() as db:
            assert db.execute(text("SELECT status, attempts FROM jobs WHERE id=:id"),
                              {"id": job_id}).one() == ("queued", 0)
            assert db.scalar(text("""SELECT count(*) FROM pg_stat_activity
                WHERE datname=current_database() AND application_name LIKE 'kinship-worker-%'""")) == 0
        assert unrelated.scalar(text("SELECT pg_backend_pid()")) == unrelated_pid
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        unrelated.close()


def test_stale_recovery_terminates_exact_attempt_and_preserves_other_sessions(reset_engine):
    engine = reset_engine
    with Session(engine) as db:
        job = queue.enqueue(db, "noop")
        queue.claim(db)
        job_id, started = job.id, job.started_at
        db.commit()
    name = lifecycle.application_name(job_id, started)
    connection = engine.raw_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('application_name',%s,false)", (name,))
        connection.commit()
        with engine.begin() as db:
            db.execute(text("UPDATE jobs SET heartbeat_at=now()-interval '1 hour' WHERE id=:id"),
                       {"id": job_id})
        with Session(engine) as db:
            assert queue.requeue_stale(db) == 1
            db.commit()
        with engine.connect() as db:
            assert db.scalar(text("SELECT status FROM jobs WHERE id=:id"), {"id": job_id}) == "queued"
            assert db.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE application_name=:name"),
                             {"name": name}) == 0
        with Session(engine) as db:
            replacement = queue.claim(db)
            assert replacement.started_at != started
            db.commit()
        lifecycle.active = lifecycle.Owner(job_id, started, "noop")
        with Session(engine) as db:
            assert not queue.heartbeat(db, job_id)
            queue.succeed(db, job_id)
            queue.fail(db, job_id, "old attempt")
            queue.defer(db, job_id)
            db.commit()
        with engine.connect() as db:
            assert db.scalar(text("SELECT status FROM jobs WHERE id=:id"), {"id": job_id}) == "running"
    finally:
        lifecycle.active = None
        connection.invalidate()
        connection.close()


def test_sql_diagnostics_do_not_expose_literals():
    preview = safe_query_preview("SELECT 'private@example.org', $$secret-token$$, 123 /* password */")
    assert "private" not in preview and "secret" not in preview and "password" not in preview
    assert len(preview) <= 120


def test_late_checkout_cannot_resume_a_recovered_attempt(reset_engine):
    engine = reset_engine
    with Session(engine) as db:
        job = queue.enqueue(db, "noop")
        queue.claim(db)
        job_id, started = job.id, job.started_at
        db.commit()
    with engine.begin() as db:
        db.execute(text("UPDATE jobs SET status='queued' WHERE id=:id"), {"id": job_id})
    probe = """
from datetime import datetime
from app.db import engine
from app.jobs import lifecycle
with engine.connect() as c:
    assert c.exec_driver_sql("SHOW application_name").scalar() == "kinship-api"
lifecycle.active = lifecycle.Owner(int(__import__('os').environ['PROBE_JOB']),
    datetime.fromisoformat(__import__('os').environ['PROBE_STARTED']), 'noop')
try:
    with engine.connect():
        raise AssertionError("Old attempt unexpectedly obtained a connection")
except lifecycle.OwnershipLost:
    pass
"""
    env = dict(os.environ, DATABASE_URL=engine.url.render_as_string(hide_password=False),
               PROBE_JOB=str(job_id), PROBE_STARTED=started.isoformat())
    result = subprocess.run([sys.executable, "-c", probe], env=env,
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("state", ["queued", "replacement", "unclaimed", "missing"])
def test_run_job_rejects_invalid_attempt_before_handler(reset_engine, state):
    engine = reset_engine
    with Session(engine) as db:
        job = queue.enqueue(db, "noop")
        job_id = job.id
        if state in ("queued", "replacement"):
            queue.claim(db)
            started = job.started_at
            queue.defer(db, job_id, seconds=0)
            db.commit()
            if state == "replacement":
                assert queue.claim(db).started_at != started
        else:
            started = None
            if state == "missing":
                db.delete(job)
        db.commit()
    with engine.connect() as db:
        before = db.execute(text("SELECT status, attempts, started_at, error FROM jobs WHERE id=:id"),
                            {"id": job_id}).first()
    probe = """
from datetime import datetime
import os
from app import worker
from app.jobs import lifecycle
called = []
worker.handlers.run = lambda *args, **kwargs: called.append(True)
started = os.environ['PROBE_STARTED']
try:
    worker.run_job(int(os.environ['PROBE_JOB']), 'noop', {},
        started_at=datetime.fromisoformat(started) if started else None)
except lifecycle.OwnershipLost:
    pass
else:
    raise AssertionError("Invalid attempt was accepted")
assert not called, "Invalid attempt reached a handler"
assert lifecycle.active is None
"""
    env = dict(os.environ, DATABASE_URL=engine.url.render_as_string(hide_password=False),
               PROBE_JOB=str(job_id), PROBE_STARTED=started.isoformat() if started else "")
    result = subprocess.run([sys.executable, "-c", probe], env=env,
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode()
    with engine.connect() as db:
        assert db.execute(text("SELECT status, attempts, started_at, error FROM jobs WHERE id=:id"),
                          {"id": job_id}).first() == before


@pytest.mark.parametrize("raises", [False, True])
def test_interactive_segment_timeout_restores_prior_deadline(reset_engine, raises):
    from app.segments.api import timed
    with Session(reset_engine) as db:
        db.execute(text("SET LOCAL statement_timeout='77s'"))

        def query():
            assert db.scalar(text("SELECT setting::int FROM pg_settings WHERE name='statement_timeout'")) == 10000
            if raises:
                raise ValueError("safe test failure")
            return 42

        if raises:
            with pytest.raises(ValueError, match="safe test failure"):
                timed(db, query)
        else:
            assert timed(db, query) == 42
        assert db.scalar(text("SELECT setting::int FROM pg_settings WHERE name='statement_timeout'")) == 77000


def test_registration_wait_is_bounded_before_job_timeout_is_installed(reset_engine):
    engine = reset_engine
    with Session(engine) as db:
        job = queue.enqueue(db, "traits.recompute")
        queue.claim(db)
        job_id, started = job.id, job.started_at
        db.commit()
    probe = """
from datetime import datetime
import os
import time
from sqlalchemy.exc import DBAPIError
from app.db import engine
from app.jobs import lifecycle
lifecycle.active = lifecycle.Owner(int(os.environ['PROBE_JOB']),
    datetime.fromisoformat(os.environ['PROBE_STARTED']), 'traits.recompute')
start = time.monotonic()
try:
    with engine.connect():
        raise AssertionError("Registration unexpectedly obtained a connection")
except DBAPIError as exc:
    assert getattr(exc.orig, 'sqlstate', None) == '57014'
    assert 4 <= time.monotonic() - start < 10
"""
    env = dict(os.environ, DATABASE_URL=engine.url.render_as_string(hide_password=False),
               PROBE_JOB=str(job_id), PROBE_STARTED=started.isoformat())
    with Session(engine) as blocker:
        lifecycle.fence(blocker, lifecycle.application_name(job_id, started))
        result = subprocess.run([sys.executable, "-c", probe], env=env,
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr.decode()
