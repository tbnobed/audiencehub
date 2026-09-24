from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.jobs.queue import enqueue
from app.models import ScheduledRun


# Session-level lock, held on a dedicated connection for the lifetime of the
# elected worker parent. Do not return a locked connection to the SQLAlchemy pool.
SCHEDULER_LOCK_KEY = 0x4155485542534348


class SchedulerLeader:
    def __init__(self):
        self.connection = None

    def ensure(self, engine) -> bool:
        if self.connection is not None:
            try:
                self.connection.execute(text("SELECT 1"))
                return True
            except Exception:
                self.close()
        connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": SCHEDULER_LOCK_KEY},
            ).scalar_one()
            if acquired:
                self.connection = connection
                return True
            connection.close()
            return False
        except Exception:
            connection.invalidate()
            connection.close()
            raise

    def close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is not None:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": SCHEDULER_LOCK_KEY},
                )
            except Exception:
                connection.invalidate()
            finally:
                connection.close()


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