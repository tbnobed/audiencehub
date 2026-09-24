from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.jobs.queue import enqueue
from app.models import ScheduledRun


def schedule_once(db: Session, task: str, window_key: str, job_type: str,
                  payload: dict | None = None) -> bool:
    job = enqueue(db, job_type, payload, dedupe_key=f"schedule:{task}:{window_key}")
    result = db.execute(insert(ScheduledRun).values(
        task=task, window_key=window_key, job_id=job.id
    ).on_conflict_do_nothing(index_elements=["task", "window_key"]).returning(ScheduledRun.task))
    return result.scalar_one_or_none() is not None


def tick(db: Session) -> None:
    # M1 has no data-refresh tasks yet. An hourly no-op verifies scheduled-run dedupe.
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    schedule_once(db, "system.heartbeat", hour, "noop", {"seconds": 0})