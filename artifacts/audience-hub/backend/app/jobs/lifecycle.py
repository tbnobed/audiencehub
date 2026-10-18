"""Process-wide job SQL ownership (one job per spawned worker process).

Helpers and heartbeat threads deliberately share this scope. Control-plane
connections opt out explicitly; no payloads or identifiers enter session names.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
import threading
import time

from sqlalchemy import text


class OwnershipLost(RuntimeError):
    pass


@dataclass
class Owner:
    job_id: int
    started_at: datetime
    job_type: str
    cancelled: threading.Event = field(default_factory=threading.Event)

    @property
    def name(self):
        return application_name(self.job_id, self.started_at)


active: Owner | None = None
worker_process = False
_control = ContextVar("job_control_connection", default=False)


def application_name(job_id, started_at):
    # started_at is an attempt nonce, unlike attempts (defer decrements it).
    nonce = hashlib.sha256(started_at.astimezone(timezone.utc).isoformat().encode()).hexdigest()[:24]
    return f"kinship-worker-{job_id}-{nonce}"


def current():
    return None if _control.get() else active


@contextmanager
def control():
    token = _control.set(True)
    try:
        yield
    finally:
        _control.reset(token)


def default_name():
    return f"kinship-worker-{os.getpid()}" if worker_process else "kinship-api"


def timeout_ms(job_type):
    from app.config import get_settings
    settings = get_settings()
    seconds = {
        "import.run": settings.bulk_statement_timeout_seconds,
        "source.pull": settings.bulk_statement_timeout_seconds,
        "zeta.import": settings.bulk_statement_timeout_seconds,
        "zeta.parity": settings.bulk_statement_timeout_seconds,
        "identity.resolve_batch": settings.bulk_statement_timeout_seconds,
        "ingestion.resolve": settings.bulk_statement_timeout_seconds,
        "traits.recompute": settings.bulk_statement_timeout_seconds,
        "segment.materialize": settings.segment_statement_timeout_seconds,
        "activation.run": settings.activation_statement_timeout_seconds,
        "deletion.execute": settings.deletion_statement_timeout_seconds,
        "privacy.expire_uploads": settings.api_statement_timeout_seconds,
        "activation.expire_exports": settings.api_statement_timeout_seconds,
        "sleep": settings.api_statement_timeout_seconds,
        "noop": settings.api_statement_timeout_seconds,
    }.get(job_type, settings.api_statement_timeout_seconds)
    return seconds * 1000


def fence(db, name):
    # Same lock as connection registration, held until the queue change commits.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 73519))"),
               {"name": name})


def signal_backends(db, name, *, terminate=False):
    function = "pg_terminate_backend" if terminate else "pg_cancel_backend"
    return db.execute(text(f"""
        SELECT pid, {function}(pid) FROM pg_stat_activity
        WHERE datname=current_database() AND usename=current_user
          AND application_name=:name AND pid<>pg_backend_pid()
    """), {"name": name}).all()


def terminate_owned(db, name, timeout=5):
    signal_backends(db, name, terminate=True)
    deadline = time.monotonic() + timeout
    while db.scalar(text("""
        SELECT count(*) FROM pg_stat_activity
        WHERE datname=current_database() AND usename=current_user
          AND application_name=:name AND pid<>pg_backend_pid()
    """), {"name": name}):
        # pg_stat_activity snapshots must not be reused in this transaction.
        db.execute(text("SELECT pg_stat_clear_snapshot()"))
        if time.monotonic() >= deadline:
            raise RuntimeError("Owned PostgreSQL sessions did not terminate; job was not requeued")
        time.sleep(0.05)
