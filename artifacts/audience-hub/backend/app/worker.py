import logging
import multiprocessing
import os
import signal
import threading
import time
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import engine
from app.identity.locking import IdentityResolutionDeferred
from app.jobs import handlers, queue, scheduler
from app.logging_config import configure_logging, safe_job_error

log = logging.getLogger(__name__)


def run_job(job_id: int, job_type: str, payload: dict) -> None:
    stop = threading.Event()

    def keep_alive():
        heartbeat_interval = min(15, max(0.2, get_settings().job_stale_seconds / 3))
        while not stop.wait(heartbeat_interval):
            try:
                with Session(engine) as db:
                    queue.heartbeat(db, job_id)
                    db.commit()
            except Exception:
                log.exception("Heartbeat failed for job %s", job_id)

    heart = threading.Thread(target=keep_alive, daemon=True)
    heart.start()
    try:
        if job_type == "identity.resolve_batch":
            with Session(engine) as db:
                if queue.identity_import_running(db):
                    raise IdentityResolutionDeferred("Identity resolution is waiting for running imports")
        if job_type == "import.run":
            from app.imports.service import run_import

            def report(progress):
                with Session(engine) as progress_db:
                    if not queue.heartbeat(progress_db, job_id, progress):
                        raise RuntimeError("Import job no longer owns its progress heartbeat")
                    progress_db.commit()

            run_import(int(payload["import_id"]), progress_callback=report)
        else:
            handlers.run(job_type, payload, job_id=job_id)
        with Session(engine) as db:
            queue.succeed(db, job_id)
            db.commit()
    except IdentityResolutionDeferred:
        with Session(engine) as db:
            queue.defer(db, job_id)
            db.commit()
    except Exception as exc:
        log.exception("Job failed: %s", job_id)
        with Session(engine) as db:
            queue.fail(db, job_id, safe_job_error(exc))
            db.commit()
    finally:
        stop.set()
        heart.join(timeout=1)


def child_main() -> None:
    """A spawned child claims and finishes one job before claiming another."""
    configure_logging(get_settings().log_level)
    stopping = threading.Event()

    def shutdown(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while not stopping.is_set():
        try:
            with Session(engine) as db:
                job = queue.claim(db)
                details = (job.id, job.type, job.payload) if job else None
                db.commit()
            if details:
                run_job(*details)
            else:
                stopping.wait(0.5)
        except Exception:
            log.exception("Worker child failed to claim/process a job; retrying")
            stopping.wait(1)
    engine.dispose()


def maintenance(leader: scheduler.SchedulerLeader, stale_seconds: int) -> None:
    # Reclamation is safe in every replica (SKIP LOCKED); only the elected
    # leader may run scheduler.tick. The leader holds its own DB session lock.
    with Session(engine) as db:
        queue.requeue_stale(db, stale_after=timedelta(seconds=stale_seconds))
        db.commit()
    if leader.ensure(engine):
        with Session(engine) as db:
            scheduler.tick(db)
            db.commit()


def replenish_children(children, concurrency: int, context) -> None:
    for child in list(children):
        if not child.is_alive():
            child.join()
            children.remove(child)
            log.warning("Worker child %s exited with code %s; restarting",
                        child.pid, child.exitcode)
    while len(children) < concurrency:
        child = context.Process(target=child_main)
        child.start()
        children.append(child)


def stop_children(children, leader: scheduler.SchedulerLeader, grace_seconds: float) -> None:
    # Release leadership *before* waiting for slow handlers so another replica
    # can keep scheduling. Never requeue live work here: the heartbeat/stale
    # policy decides when a killed child's unfinished job becomes claimable.
    try:
        leader.close()
    finally:
        deadline = time.monotonic() + grace_seconds
        for child in children:
            if child.is_alive():
                child.terminate()  # child's SIGTERM handler finishes its current job
        for child in children:
            child.join(timeout=max(0, deadline - time.monotonic()))
        remaining = [child for child in children if child.is_alive()]
        for child in remaining:
            log.warning("Worker child %s exceeded shutdown grace; killing", child.pid)
            child.kill()
        # Leave a little time to reap killed children before Docker's 30m SIGKILL.
        kill_deadline = time.monotonic() + 5
        for child in remaining:
            child.join(timeout=max(0, kill_deadline - time.monotonic()))
            if child.is_alive():
                log.error("Worker child %s did not exit after SIGKILL", child.pid)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.worker_concurrency < 1:
        raise ValueError("WORKER_CONCURRENCY must be at least 1")
    grace_seconds = float(os.environ.get("WORKER_STOP_GRACE_SECONDS", "1750"))
    if not 0 <= grace_seconds <= 1790:
        raise ValueError("WORKER_STOP_GRACE_SECONDS must be between 0 and 1790 (Compose stops at 1800)")
    stopping = threading.Event()

    def shutdown(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    context = multiprocessing.get_context("spawn")
    children = []
    leader = scheduler.SchedulerLeader()
    maintenance_interval = min(30, max(0.5, settings.job_stale_seconds / 2))
    next_maintenance = 0.0
    try:
        while not stopping.is_set():
            replenish_children(children, settings.worker_concurrency, context)
            if time.monotonic() >= next_maintenance:
                try:
                    maintenance(leader, settings.job_stale_seconds)
                except Exception:
                    log.exception("Worker maintenance failed; retrying")
                    leader.close()
                next_maintenance = time.monotonic() + maintenance_interval
            stopping.wait(0.5)
    finally:
        try:
            stop_children(children, leader, grace_seconds)
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()