import logging
import multiprocessing
import os
import signal
import threading
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import engine, control_engine
from app.identity.locking import IdentityResolutionDeferred
from app.jobs import handlers, queue, scheduler
from app.jobs import lifecycle
from app.models import Job
from app.logging_config import configure_logging, safe_job_error

log = logging.getLogger(__name__)


def shutdown_job(owner, stopping, finished):
    while not finished.is_set():
        if not stopping.wait(0.05):
            continue
        owner.cancelled.set()
        # A separate control connection can interrupt a blocked libpq call.
        # Never publish retry until all owned SQL sessions have disappeared.
        try:
            with lifecycle.control(), Session(control_engine) as db:
                lifecycle.signal_backends(db, owner.name)
                db.commit()
                time.sleep(0.2)
                # Release even an idle/aborted helper's row locks before trying
                # to lock the queue row. Registration is already locally stopped.
                lifecycle.terminate_owned(db, owner.name)
                db.commit()
                job = db.scalar(select(Job).where(
                    Job.id == owner.job_id, Job.status == "running",
                    Job.started_at == owner.started_at).with_for_update())
                lifecycle.fence(db, owner.name)
                lifecycle.terminate_owned(db, owner.name)
                if job:
                    queue.defer(db, owner.job_id, seconds=0)
                db.commit()
        except Exception:
            # Leave the running row for stale recovery, never falsely report retry.
            log.exception("Shutdown recovery failed for job %s", owner.job_id)
            os._exit(1)
        os._exit(0)


def run_job(job_id: int, job_type: str, payload: dict, *, started_at=None, stopping=None) -> None:
    if started_at is None:
        with Session(engine) as db:
            job = db.get(Job, job_id)
            if job is None or job.status != "running" or job.started_at is None:
                raise lifecycle.OwnershipLost("Job does not have a claimed running attempt")
            started_at = job.started_at
    owner = lifecycle.Owner(job_id, started_at, job_type)
    lifecycle.active = owner
    stopping = stopping if stopping is not None else threading.Event()
    finished = threading.Event()
    watcher = threading.Thread(target=shutdown_job, args=(owner, stopping, finished), daemon=True)
    watcher.start()
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
        # Validate the attempt before any handler (including non-SQL side
        # effects) runs. Checkout serializes with stale recovery and rejects
        # a previously recovered/replaced attempt.
        with Session(engine) as db:
            db.connection()
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
        if stopping.is_set():
            owner.cancelled.set()
            watcher.join()
        with Session(engine) as db:
            queue.defer(db, job_id)
            db.commit()
    except lifecycle.OwnershipLost:
        # Do not report failure or mutate a replacement attempt after fencing.
        # The shutdown watcher/finally path still drains a stopping worker.
        raise
    except Exception as exc:
        if stopping.is_set():
            owner.cancelled.set()
            watcher.join()
        log.exception("Job failed: %s", job_id)
        with Session(engine) as db:
            queue.fail(db, job_id, safe_job_error(exc))
            db.commit()
    finally:
        stop.set()
        heart.join(timeout=1)
        if stopping.is_set():
            owner.cancelled.set()
            watcher.join()
        finished.set()
        watcher.join(timeout=1)
        lifecycle.active = None


def child_main() -> None:
    """A spawned child claims and finishes one job before claiming another."""
    configure_logging(get_settings().log_level)
    lifecycle.worker_process = True
    stopping = threading.Event()

    def shutdown(_signum, _frame):
        stopping.set()
        if lifecycle.active:
            lifecycle.active.cancelled.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while not stopping.is_set():
        try:
            with Session(engine) as db:
                job = queue.claim(db)
                details = (job.id, job.type, job.payload, job.started_at) if job else None
                db.commit()
            if details:
                run_job(*details[:3], started_at=details[3], stopping=stopping)
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
    # Release leadership before cancelling children. Each child cancels ONLY its
    # attempt's sessions and publishes retry after they disappear.
    try:
        leader.close()
    finally:
        deadline = time.monotonic() + grace_seconds
        for child in children:
            if child.is_alive():
                child.terminate()
        for child in children:
            child.join(timeout=max(0, deadline - time.monotonic()))
        remaining = [child for child in children if child.is_alive()]
        for child in remaining:
            log.warning("Worker child %s exceeded shutdown grace; killing", child.pid)
            child.kill()
        # Leave time to reap killed children before Docker's 30s SIGKILL.
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
    lifecycle.worker_process = True
    grace_seconds = float(os.environ.get("WORKER_STOP_GRACE_SECONDS", "20"))
    if not 0 <= grace_seconds <= 20:
        raise ValueError("WORKER_STOP_GRACE_SECONDS must be between 0 and 20 (Compose stops at 30)")
    stopping = threading.Event()
    children = []

    def shutdown(_signum, _frame):
        stopping.set()
        # Do not wait for a blocked maintenance query before notifying children.
        for child in children:
            if child.is_alive():
                child.terminate()

    def cancel_maintenance():
        stopping.wait()
        try:
            with lifecycle.control(), Session(control_engine) as db:
                lifecycle.signal_backends(db, lifecycle.default_name())
                db.commit()
        except Exception:
            log.exception("Could not cancel worker maintenance SQL during shutdown")

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=cancel_maintenance, daemon=True).start()
    context = multiprocessing.get_context("spawn")
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