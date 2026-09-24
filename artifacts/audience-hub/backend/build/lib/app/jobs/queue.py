from datetime import timedelta
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.models import Job, now


def enqueue(db: Session, job_type: str, payload: dict | None = None, *,
            dedupe_key: str | None = None, priority: int = 100, max_attempts: int = 3) -> Job:
    if dedupe_key:
        existing = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key, Job.status.in_(("queued", "running"))))
        if existing:
            return existing
    stmt = insert(Job).values(type=job_type, payload=payload or {}, dedupe_key=dedupe_key,
                              priority=priority, max_attempts=max_attempts).on_conflict_do_nothing(
                                  index_elements=[Job.dedupe_key],
                                  index_where=Job.status.in_(("queued", "running"))).returning(Job.id)
    job_id = db.scalar(stmt)
    if job_id is None:
        return db.scalar(select(Job).where(Job.dedupe_key == dedupe_key, Job.status.in_(("queued", "running"))))
    return db.get(Job, job_id)


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