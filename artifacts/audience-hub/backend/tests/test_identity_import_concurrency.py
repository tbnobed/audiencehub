"""Committed concurrency tests: ONLY run in benchmark's disposable database."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.db import engine
from app.identity.locking import IdentityResolutionDeferred, import_identity_lock
from app.identity.resolver import resolve_batch
from app.imports.service import run_import
from app.jobs import queue
from app.models import Import, Job, Source
from app.worker import run_job


pytestmark = pytest.mark.skipif(
    os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
    reason="requires kinship benchmark --test-suite disposable database",
)


@pytest.fixture(autouse=True)
def clean_test_jobs():
    yield
    with Session(engine) as db:
        db.execute(delete(Job).where(
            Job.payload["concurrency_test"].as_boolean() == True
        ))
        db.commit()


def _resolver_job():
    with Session(engine) as db:
        job = Job(type="identity.resolve_batch", payload={"concurrency_test": True}, status="running",
                  attempts=1, max_attempts=1)
        db.add(job)
        db.commit()
        return job.id


def _assert_deferred(job_id, before):
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job.status == "queued"
        assert job.attempts == 0
        assert job.error is None
        assert job.started_at is None
        assert job.heartbeat_at is None
        assert job.run_after >= before
        assert 29 <= (job.run_after - before).total_seconds() < 60


def test_shared_lock_survives_commits_and_worker_defers_without_failure():
    job_id = _resolver_job()
    with import_identity_lock(engine):
        with Session(engine) as db:
            db.execute(text("SELECT 1"))
            db.commit()
            with pytest.raises(IdentityResolutionDeferred):
                resolve_batch(db)
            db.rollback()
        before = datetime.now(timezone.utc)
        run_job(job_id, "identity.resolve_batch", {})
        _assert_deferred(job_id, before)
    with Session(engine) as db:
        resolve_batch(db)
        db.rollback()


@pytest.mark.parametrize("record_type", ["contact", "consent", "enrichment"])
def test_running_import_queue_check_defers_resolver(record_type, tmp_path):
    with Session(engine) as db:
        source = Source(key=f"queue_{uuid.uuid4().hex}", name="Queue test", kind="csv",
                        record_types=[record_type])
        db.add(source)
        db.flush()
        imported = Import(source_id=source.id, filename="unused.csv",
                          file_path=str(tmp_path / "unused.csv"), record_type=record_type)
        db.add(imported)
        db.flush()
        importing = Job(type="import.run", payload={"import_id": imported.id, "concurrency_test": True},
                        status="running", attempts=1)
        db.add(importing)
        db.commit()
        import_job_id = importing.id
    try:
        resolver_id = _resolver_job()
        before = datetime.now(timezone.utc)
        run_job(resolver_id, "identity.resolve_batch", {})
        _assert_deferred(resolver_id, before)
    finally:
        with Session(engine) as db:
            queue.succeed(db, import_job_id)
            db.commit()


def test_two_contact_imports_and_resolver_overlapping_identifiers(tmp_path):
    prefix = uuid.uuid4().hex
    import_ids = []
    source_ids = []
    with Session(engine) as db:
        for index in range(2):
            source = Source(key=f"concurrent_{prefix}_{index}", name="Concurrent test",
                            kind="csv", record_types=["contact"])
            db.add(source)
            db.flush()
            source_ids.append(source.id)
            path = tmp_path / f"contacts_{index}.csv"
            # Same identifiers, opposite row order: exercise unique-index writes
            # and deterministic resolver conflict-key ordering.
            rows = list(range(40))
            if index:
                rows.reverse()
            path.write_text("external_id,email,first_name\n" + "".join(
                f"{row},shared-{prefix}-{row}@example.org,Contact\n" for row in rows
            ))
            imported = Import(
                source_id=source.id, filename=path.name, file_path=str(path),
                record_type="contact", rows_total=40,
                mapping={"columns": {key: key for key in
                                     ("external_id", "email", "first_name")}, "options": {}},
            )
            db.add(imported)
            db.flush()
            import_ids.append(imported.id)
        db.commit()

    ready = [threading.Event(), threading.Event()]
    release = threading.Event()

    def execute_import(index):
        paused = False

        def progress(_value):
            nonlocal paused
            if not paused:
                paused = True
                ready[index].set()
                assert release.wait(30), "Resolver test did not release import"

        return run_import(import_ids[index], progress_callback=progress)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute_import, index) for index in range(2)]
        try:
            assert all(item.wait(20) for item in ready), "Both imports must hold shared locks"
            # No import jobs exist: this specifically tests the authoritative
            # lock for direct run_import calls, not the queue courtesy check.
            resolver_id = _resolver_job()
            before = datetime.now(timezone.utc)
            run_job(resolver_id, "identity.resolve_batch", {})
            _assert_deferred(resolver_id, before)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=60)

    with Session(engine) as db:
        imports = db.scalars(select(Import).where(Import.id.in_(import_ids))).all()
        assert all(item.rows_ok == 40 and item.rows_rejected == 0 for item in imports)
        while True:
            result = resolve_batch(db)
            db.commit()
            if result["records"] < 10_000:
                break
        counts = db.execute(text("""
            SELECT count(*), count(DISTINCT profile_id),
                   count(*) FILTER (WHERE resolved_at IS NULL OR profile_id IS NULL)
            FROM source_records WHERE source_id=ANY(:ids)
        """), {"ids": source_ids}).one()
        assert tuple(counts) == (80, 40, 0)