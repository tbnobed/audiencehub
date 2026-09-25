"""Queue regression checks; PG cases use the disposable benchmark harness only."""
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.jobs import queue
from app.models import Job


def test_conflict_predicate_is_literal_not_parameters():
    class Capture:
        calls = 0

        def scalar(self, statement):
            self.calls += 1
            if self.calls == 1:
                return None
            compiled = statement.compile(dialect=postgresql.dialect())
            assert "WHERE status IN ('queued', 'running') DO NOTHING" in str(compiled)
            assert "queued" not in compiled.params.values()
            assert "running" not in compiled.params.values()
            return 1

        def get(self, model, key):
            return key

    assert queue.enqueue(Capture(), "noop", dedupe_key="compile-only") == 1


@pytest.fixture
def isolated_queue_engine():
    if not os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS"):
        pytest.skip("requires disposable benchmark PostgreSQL harness")
    from app.db import database_url

    # Never alter the harness's jobs, let alone a production jobs table.
    schema = f"queue_test_{uuid4().hex}"
    admin = create_engine(database_url())
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(database_url(), connect_args={
        "prepare_threshold": 0,
        "options": f"-c search_path={schema} -c plan_cache_mode=force_generic_plan",
    })
    try:
        path = Path(__file__).resolve().parents[1] / "alembic/versions/0001_foundation.py"
        spec = importlib.util.spec_from_file_location("queue_foundation", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_enqueue_twice_and_prepared_conflict_inference(isolated_queue_engine, status):
    engine = isolated_queue_engine
    inserts = []

    def capture(conn, clauseelement, multiparams, params, execution_options):
        if getattr(clauseelement, "is_insert", False):
            inserts.append(clauseelement)

    event.listen(engine, "before_execute", capture)
    try:
        with Session(engine) as db:
            first = queue.enqueue(db, "noop", {"seconds": 0}, dedupe_key="same-key")
            first.status = status
            db.flush()
            second = queue.enqueue(db, "noop", {"seconds": 20}, dedupe_key="same-key")
            assert first.id == second.id
            # The fast SELECT above must not mask the ON CONFLICT bug. Execute
            # the exact captured enqueue INSERT repeatedly with generic plans.
            statement = inserts[0]
            for _ in range(8):
                assert db.scalar(statement) is None
            assert db.scalar(select(func.count()).select_from(Job)) == 1
            prepared = db.execute(text(
                "SELECT statement, generic_plans FROM pg_prepared_statements"
            )).all()
            assert any("ON CONFLICT" in sql and plans > 0 for sql, plans in prepared)
            db.rollback()
    finally:
        event.remove(engine, "before_execute", capture)


def test_selfcheck_is_invisible_and_leaves_existing_work(isolated_queue_engine):
    engine = isolated_queue_engine
    with Session(engine) as db:
        original = queue.enqueue(db, "noop", dedupe_key="existing-work")
        original_id = original.id
        db.commit()
    cancellations = []

    def observe(db, flush_context):
        for job in db.identity_map.values():
            if isinstance(job, Job) and job.status == "cancelled":
                cancellations.append(job.dedupe_key)
                with engine.connect() as observer:
                    assert observer.scalar(text(
                        "SELECT count(*) FROM jobs WHERE dedupe_key = :key"
                    ), {"key": job.dedupe_key}) == 0

    event.listen(Session, "after_flush", observe)
    try:
        queue.startup_selfcheck(engine)
        queue.startup_selfcheck(engine)
    finally:
        event.remove(Session, "after_flush", observe)
    assert len(set(cancellations)) == 2
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.get(Job, original_id).status == "queued"


def test_selfcheck_fails_closed_for_wrong_index(isolated_queue_engine):
    engine = isolated_queue_engine
    # Only the generated scratch schema is changed. No deployed index rebuild.
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_jobs_active_dedupe"))
        connection.execute(text(
            "CREATE UNIQUE INDEX uq_jobs_active_dedupe ON jobs (dedupe_key) "
            "WHERE status = 'queued'"
        ))
    with pytest.raises(ProgrammingError) as error:
        queue.startup_selfcheck(engine)
    assert error.value.orig.sqlstate == "42P10"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM jobs")) == 0


def test_startup_does_not_swallow_selfcheck_failure(monkeypatch):
    from app import main
    from fastapi.testclient import TestClient

    def fail(bind):
        raise RuntimeError("queue startup selfcheck failed")

    monkeypatch.setattr(main.queue, "startup_selfcheck", fail)
    monkeypatch.setattr(main.oidc, "configure_oidc", lambda: None)
    with pytest.raises(RuntimeError, match="queue startup selfcheck failed"):
        with TestClient(main.app):
            pass