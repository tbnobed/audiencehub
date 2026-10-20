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
    # Keep matching disabled until an admin reviews a dry run. Address/household
    # maintenance runs independently and never changes identity memberships.
    graph_policy = db.execute(text("SELECT * FROM identity_graph_policy WHERE id=1")).mappings().one()
    if graph_policy["enabled"] and now.hour >= graph_policy["schedule_hour"]:
        window = now.strftime("%Y-%m-%d")
        task = "identity.graph.daily"
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                   {"key": f"{task}:{window}"})
        if db.get(ScheduledRun, (task, window)) is None:
            from app.identity.graph_jobs import queue_run
            identifier = queue_run(db, False)
            db.add(ScheduledRun(task=task, window_key=window, job_id=identifier))
    schedule_once(db, "maintenance.daily", now.strftime("%Y-%m-%d"), "maintenance.run", {})
    for segment_id in db.scalars(text("""SELECT DISTINCT a.segment_id FROM meta_audiences a
      JOIN sources s ON s.id=a.source_id WHERE s.is_active""")):
        schedule_once(db, f"meta.results:{segment_id}", now.strftime("%Y-%m-%d"),
                      "meta.results", {"segment_id": segment_id})
    from app.source_pulls import tick_sources
    tick_sources(db, now)
    from app.activations.schedule import tick_activations
    tick_activations(db, now)
    schedule_once(db, "activation.exports.retention", now.strftime("%Y-%m-%d"),
                  "activation.expire_exports", {})
    # Keep an inexpensive hourly heartbeat for scheduler health and run one full
    # trait recompute per UTC day.
    hour = now.strftime("%Y-%m-%dT%H")
    schedule_once(db, "system.heartbeat", hour, "noop", {"seconds": 0})
    schedule_once(
        db, "traits.nightly_full", now.strftime("%Y-%m-%d"),
        "traits.recompute", {"mode": "full"},
    )
    if now.hour >= 4:
        for segment_id in db.scalars(text("""SELECT id FROM segments
          WHERE status='active' AND refresh_schedule IN ('daily','0 4 * * *') ORDER BY id""")):
            schedule_once(db, f"segment.daily:{segment_id}", now.strftime("%Y-%m-%d"),
                          "segment.materialize", {"segment_id": segment_id,
                                                  "traits_window": now.strftime("%Y-%m-%d")})
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