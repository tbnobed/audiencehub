from datetime import timedelta
from uuid import uuid4
from sqlalchemy import func, select, update, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.models import Job, now


def enqueue(db: Session, job_type: str, payload: dict | None = None, *,
            dedupe_key: str | None = None, priority: int = 100, max_attempts: int = 3) -> Job:
    if dedupe_key:
        existing = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key, Job.status.in_(("queued", "running"))))
        if existing:
            return existing
    # Match the migration's partial index literally: bound status values cannot
    # imply its predicate when PostgreSQL selects a generic prepared plan.
    stmt = insert(Job).values(type=job_type, payload=payload or {}, dedupe_key=dedupe_key,
                              priority=priority, max_attempts=max_attempts).on_conflict_do_nothing(
                                  index_elements=[Job.dedupe_key],
                                  index_where=text("status IN ('queued', 'running')")).returning(Job.id)
    while True:
        job_id = db.scalar(stmt)
        if job_id is not None:
            return db.get(Job, job_id)
        existing = db.scalar(select(Job).where(
            Job.dedupe_key == dedupe_key, Job.status.in_(("queued", "running"))))
        if existing is not None:
            return existing
        # READ COMMITTED: the conflicting job may finish between INSERT and
        # SELECT, leaving no active row. Retry INSERT rather than return None.


def startup_selfcheck(bind) -> None:
    """Exercise index inference without ever publishing runnable work.

    A fresh key guarantees INSERT is exercised, not the fast-path SELECT.
    Cancel and flush in the same uncommitted transaction, then roll back all
    probe data. Other sessions (including workers) can never see the noop.
    Errors deliberately propagate and prevent API startup.
    """
    with Session(bind) as db:
        try:
            job = enqueue(db, "noop", {"seconds": 0},
                          dedupe_key=f"startup-selfcheck:{uuid4().hex}")
            job.status = "cancelled"
            job.finished_at = now()
            db.flush()
        finally:
            db.rollback()


def claim(db: Session) -> Job | None:
    candidate = db.scalar(select(Job).where(Job.status == "queued", Job.run_after <= func.clock_timestamp())
                          .order_by(Job.priority, Job.id).with_for_update(skip_locked=True).limit(1))
    if candidate:
        candidate.status = "running"
        candidate.attempts += 1
        candidate.started_at = now()
        candidate.heartbeat_at = now()
        db.flush()
    return candidate


def heartbeat(db: Session, job_id: int, progress: dict | None = None) -> bool:
    values = {"heartbeat_at": now()}
    if progress is not None:
        values["progress"] = progress
    return bool(db.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(**values)).rowcount)


def identity_import_running(db: Session) -> bool:
    """Queue-level courtesy check; the advisory lock is the race-free guard."""
    return bool(db.scalar(text("""
        SELECT EXISTS (
            SELECT 1 FROM jobs j JOIN imports i
              ON j.payload->>'import_id' = i.id::text
            WHERE j.type='import.run' AND j.status='running'
              AND i.record_type IN ('contact', 'consent', 'enrichment')
        )
    """)))


def defer(db: Session, job_id: int, seconds: int = 30) -> None:
    """Yield claimed work without spending its failure/retry budget."""
    db.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(
        status="queued", run_after=func.clock_timestamp() + timedelta(seconds=seconds),
        attempts=func.greatest(Job.attempts - 1, 0),
        started_at=None, heartbeat_at=None, finished_at=None,
    ))


def succeed(db: Session, job_id: int) -> None:
    db.execute(update(Job).where(Job.id == job_id, Job.status == "running")
               .values(status="succeeded", finished_at=now(), heartbeat_at=now()))


def fail(db: Session, job_id: int, error: str) -> None:
    job = db.get(Job, job_id, with_for_update=True)
    if not job or job.status != "running":
        return
    # Error text must never contain raw payloads or PII.
    job.error = error[:500]
    job.heartbeat_at = None
    if job.attempts >= job.max_attempts:
        job.status = "failed"
        job.finished_at = now()
    else:
        job.status = "queued"
        job.run_after = now() + timedelta(seconds=min(3600, 2 ** job.attempts * 10))


def requeue_stale(db: Session, stale_after: timedelta = timedelta(minutes=5),
                  job_ids: list[int] | None = None) -> int:
    stmt = select(Job).where(Job.status == "running", Job.heartbeat_at < now() - stale_after)
    if job_ids is not None:
        stmt = stmt.where(Job.id.in_(job_ids))
    jobs = db.scalars(stmt.with_for_update(skip_locked=True)).all()
    for job in jobs:
        job.heartbeat_at = None
        job.error = "Worker heartbeat expired"
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            job.finished_at = now()
        else:
            job.status = "queued"
            job.run_after = now()
    db.flush()
    return len(jobs)


def retry(db: Session, job_id: int) -> Job | None:
    job = db.get(Job, job_id, with_for_update=True)
    if not job or job.status != "failed":
        return None
    job.status = "queued"
    job.attempts = 0
    job.error = None
    job.run_after = now()
    job.finished_at = None
    db.flush()
    return job