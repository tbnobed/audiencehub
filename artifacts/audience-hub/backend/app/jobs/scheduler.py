from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.jobs.queue import enqueue
from app.models import ScheduledRun


def schedule_once(db: Session, task: str, window_key: str, job_type: str,
                  payload: dict | None = None) -> bool:
    # A completed job is no longer held by queue dedupe. Check the durable
    # scheduled-run key *before* enqueueing, or every scheduler tick creates
    # another job for the same daily window.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
               {"key": f"{task}:{window_key}"})
    if db.get(ScheduledRun, (task, window_key)) is not None:
        return False
    job = enqueue(db, job_type, payload, dedupe_key=f"schedule:{task}:{window_key}")
    result = db.execute(insert(ScheduledRun).values(
        task=task, window_key=window_key, job_id=job.id
    ).on_conflict_do_nothing(index_elements=["task", "window_key"]).returning(ScheduledRun.task))
    return result.scalar_one_or_none() is not None


def tick(db: Session) -> None:
    now = datetime.now(timezone.utc)
    # Keep an inexpensive hourly heartbeat for scheduler health and run one full
    # trait recompute per UTC day.
    hour = now.strftime("%Y-%m-%dT%H")
    schedule_once(db, "system.heartbeat", hour, "noop", {"seconds": 0})
    schedule_once(
        db, "traits.nightly_full", now.strftime("%Y-%m-%d"),
        "traits.recompute", {"mode": "full"},
    )
    dirty_ready = db.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM trait_dirty_profiles
            WHERE dirtied_at <= clock_timestamp() - interval '10 minutes'
        )
    """)).scalar_one()
    if dirty_ready:
        enqueue(
            db, "traits.recompute", {"mode": "dirty"},
            dedupe_key="traits:incremental",
        )