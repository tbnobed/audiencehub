import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy.orm import Session
from app.config import get_settings
from app.db import engine
from app.jobs import queue, handlers, scheduler
from app.logging_config import configure_logging

log = logging.getLogger(__name__)


def run_job(job_id: int, job_type: str, payload: dict) -> None:
    stop = threading.Event()

    def keep_alive():
        while not stop.wait(15):
            with Session(engine) as db:
                queue.heartbeat(db, job_id)
                db.commit()

    heart = threading.Thread(target=keep_alive, daemon=True)
    heart.start()
    try:
        handlers.run(job_type, payload)
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
        while True:
            active = {f for f in active if not f.done()}
            if time.monotonic() - last_maintenance > 30:
                with Session(engine) as db:
                    queue.requeue_stale(db)
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