import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from sqlalchemy.orm import Session
from app.config import get_settings
from app.db import engine
from app.jobs import queue, handlers, scheduler
from app.logging_config import configure_logging

log = logging.getLogger(__name__)


def run_job(job_id: int, job_type: str, payload: dict) -> None:
    stop = threading.Event()

    def keep_alive():
        heartbeat_interval = min(15, max(0.2, get_settings().job_stale_seconds / 3))
        while not stop.wait(heartbeat_interval):
            with Session(engine) as db:
                queue.heartbeat(db, job_id)
                db.commit()

    heart = threading.Thread(target=keep_alive, daemon=True)
    heart.start()
    try:
        if job_type == "import.run":
            from app.imports.service import run_import

            def report(progress):
                with Session(engine) as progress_db:
                    queue.heartbeat(progress_db, job_id, progress)
                    progress_db.commit()

            run_import(int(payload["import_id"]), progress_callback=report)
        else:
            handlers.run(job_type, payload, job_id=job_id)
        with Session(engine) as db:
            queue.succeed(db, job_id)
            db.commit()
    except Exception:
        log.exception("Job failed: %s", job_id)
        with Session(engine) as db:
            queue.fail(db, job_id, "Job handler failed; see server logs")
            db.commit()
    finally:
        stop.set()
        heart.join(timeout=1)


def main():
    configure_logging(get_settings().log_level)
    with ThreadPoolExecutor(max_workers=get_settings().worker_concurrency) as pool:
        active = set()
        last_maintenance = 0.0
        maintenance_interval = min(30, max(0.5, get_settings().job_stale_seconds / 2))
        while True:
            active = {f for f in active if not f.done()}
            if time.monotonic() - last_maintenance > maintenance_interval:
                with Session(engine) as db:
                    queue.requeue_stale(
                        db, stale_after=timedelta(seconds=get_settings().job_stale_seconds))
                    scheduler.tick(db)
                    db.commit()
                last_maintenance = time.monotonic()
            if len(active) < get_settings().worker_concurrency:
                with Session(engine) as db:
                    job = queue.claim(db)
                    if job:
                        details = (job.id, job.type, job.payload)
                        db.commit()
                        active.add(pool.submit(run_job, *details))
            time.sleep(0.5)


if __name__ == "__main__":
    main()