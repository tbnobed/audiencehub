import json
import logging
import sys
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.imports.api import _json_import
from app.jobs.queue import enqueue
from app.logging_config import JsonFormatter, safe_job_error


def test_exception_metadata_never_formats_sql_values_or_exception_text():
    class DriverError(Exception):
        sqlstate = "23505"
        diag = SimpleNamespace(constraint_name="uq_jobs_active_dedupe")

    secret = "private-person@example.org secret donor name"
    try:
        raise IntegrityError("INSERT private SQL", {"email": secret}, DriverError(secret))
    except IntegrityError as error:
        record = logging.LogRecord("worker", logging.ERROR, __file__, 1,
                                   "Job failed: %s", (345,), sys.exc_info())
        formatted = JsonFormatter().format(record)
        summary = safe_job_error(error)
    data = json.loads(formatted)["exception"]
    assert data["class"] == "IntegrityError"
    assert data["driver_class"] == "DriverError"
    assert data["sqlstate"] == "23505"
    assert data["constraint"] == "uq_jobs_active_dedupe"
    assert data["frames"]
    assert all(set(frame) == {"file", "function", "line"} for frame in data["frames"])
    for forbidden in (secret, "private SQL", "params"):
        assert forbidden not in formatted + summary
    assert "23505" in summary
    assert "uniqueness constraint" in summary
    assert "Traceback (sanitized" in data["traceback"]


def test_deadlock_traceback_includes_cause_and_every_frame_without_pii():
    secret_email = "donor.private@example.org"
    secret_phone = "+1 (415) 555-0199"

    class DeadlockDetected(Exception):
        sqlstate = "40P01"

    def database_operation():
        # SQL and parameters are deliberately unsafe even when wrapped.
        raise OperationalError("SELECT secret_column FROM donors WHERE email=:email",
                               {"email": secret_email, "phone": secret_phone},
                               DeadlockDetected(f"deadlock for {secret_email} {secret_phone}"))

    def handler():
        try:
            database_operation()
        except OperationalError as cause:
            raise RuntimeError(f"could not process {secret_email} {secret_phone}") from cause

    try:
        handler()
    except RuntimeError as error:
        record = logging.LogRecord("app.worker", logging.ERROR, __file__, 1,
                                   "Job failed: %s", (42,), sys.exc_info())
        formatted = JsonFormatter().format(record)
        summary = safe_job_error(error)
    data = json.loads(formatted)["exception"]
    traceback = data["traceback"]
    assert data["class"] == "RuntimeError"
    assert data["message"] == "Deadlock detected; transaction was rolled back; retry the job"
    assert summary.startswith("Job handler failed: RuntimeError: Deadlock detected")
    assert "40P01" in summary or "Deadlock detected" in summary
    assert "RuntimeError:" in traceback
    assert "OperationalError:" in traceback
    assert "DeadlockDetected:" in traceback
    assert "in handler" in traceback
    assert "in database_operation" in traceback
    assert traceback.count('  File "') >= 3
    for forbidden in (secret_email, secret_phone, "SELECT secret_column", "secret_column",
                      "secret donor", "WHERE email", "parameters"):
        assert forbidden not in formatted + summary


def test_unknown_exception_message_is_not_reproduced():
    secret = "private.person@example.org"
    try:
        raise RuntimeError(f"customer details: {secret} and unusual donor name")
    except RuntimeError as error:
        record = logging.LogRecord("app.worker", logging.ERROR, __file__, 1,
                                   "Job failed", (), sys.exc_info())
        result = JsonFormatter().format(record) + safe_job_error(error)
    assert secret not in result
    assert "unusual donor name" not in result
    assert "unexpected error" in result


def test_arbitrary_constraint_name_is_not_exposed():
    error = RuntimeError("never print")
    error.diag = SimpleNamespace(constraint_name="private_donor_name")
    assert "private_donor_name" not in safe_job_error(error)


def test_import_status_uses_committed_job_even_if_import_transaction_is_open():
    for status in ("queued", "running"):
        assert _json_import({"status": "mapped", "job_status": status})["status"] == "running"
    result = _json_import({"status": "mapped", "job_status": "failed",
                           "job_error": "Job handler failed: ValueError"})
    assert result["status"] == "failed"
    assert result["job_error"] == "Job handler failed: ValueError"


def test_enqueue_retries_if_conflicting_job_finishes_before_followup_select():
    # Sequence: initial lookup misses; INSERT conflicts; follow-up lookup
    # misses because winner completed; second INSERT returns a fresh id.
    results = iter([None, None, None, 123])
    job = SimpleNamespace(id=123)

    class Db:
        def scalar(self, statement):
            return next(results)

        def get(self, model, job_id):
            assert job_id == 123
            return job

    assert enqueue(Db(), "identity.resolve_batch", {}, dedupe_key="identity") is job


@pytest.mark.skipif(not os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS"),
                    reason="concurrency test requires the disposable benchmark database")
def test_concurrent_identity_enqueue_returns_same_active_job():
    from app.db import engine
    from app.models import Job

    barrier = Barrier(4)
    key = f"diagnostics-concurrency-{uuid.uuid4().hex}"

    def submit():
        with Session(engine) as db:
            barrier.wait(timeout=10)
            job = enqueue(db, "identity.resolve_batch", {}, dedupe_key=key)
            job_id = job.id
            db.commit()
            return job_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: submit(), range(4)))
    assert len(set(ids)) == 1
    with Session(engine) as db:
        job = db.get(Job, ids[0])
        assert job.status == "queued"
        db.delete(job)
        db.commit()