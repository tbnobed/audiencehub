#!/usr/bin/env python3
"""Exercise stale-job recovery using only a bounded synthetic sleep job."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))


def worker_process(environment):
    return subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=BACKEND,
        env=environment,
    )


def stop_owned_worker(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stale-seconds",
        type=int,
        default=int(os.environ.get("JOB_STALE_SECONDS", "300")),
        help="Worker heartbeat age required for stale recovery (default: JOB_STALE_SECONDS or 300)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        help="Overall wait limit (default: stale threshold + 150 seconds)",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=int,
        default=60,
        help="Synthetic sleep duration, from 1 to 60 seconds (default: 60)",
    )
    args = parser.parse_args()
    if args.stale_seconds < 1:
        parser.error("--stale-seconds must be at least 1")
    if args.timeout_seconds is not None and args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")
    if not 1 <= args.sleep_seconds <= 60:
        parser.error("--sleep-seconds must be between 1 and 60")

    os.environ["JOB_STALE_SECONDS"] = str(args.stale_seconds)
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.jobs.queue import enqueue
    from app.models import Job

    with Session(engine) as db:
        job = enqueue(
            db,
            "sleep",
            {"synthetic": True, "seconds": args.sleep_seconds},
            dedupe_key=f"manual-worker-recovery:{uuid.uuid4()}",
            priority=-1000000,
            max_attempts=3,
        )
        job_id = job.id
        db.commit()

    environment = os.environ.copy()
    worker = None
    try:
        worker = worker_process(environment)
        running_deadline = time.monotonic() + 30
        while time.monotonic() < running_deadline:
            with Session(engine) as db:
                current = db.get(Job, job_id)
                if current and current.status == "running":
                    break
            if worker.poll() is not None:
                raise RuntimeError(f"Worker exited before claiming synthetic job (exit {worker.returncode})")
            time.sleep(0.2)
        else:
            raise TimeoutError("Worker did not claim the synthetic sleep job")

        # Give the handler time to enter its bounded sleep before killing only
        # the child process created by this script.
        time.sleep(0.5)
        stop_owned_worker(worker)
        worker = None
        print(f"Killed owned worker during synthetic sleep job {job_id}.")

        print(f"Waiting {args.stale_seconds}s for the configured stale threshold.")
        time.sleep(args.stale_seconds + 0.2)
        worker = worker_process(environment)
        timeout = args.timeout_seconds or args.stale_seconds + 150
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with Session(engine) as db:
                current = db.scalar(select(Job).where(Job.id == job_id))
                if current and current.status in ("succeeded", "failed"):
                    print(f"Job {job_id}: {current.status}; attempts={current.attempts}")
                    if current.status != "succeeded":
                        raise RuntimeError(f"Synthetic recovery job ended as {current.status}")
                    return
            if worker.poll() is not None:
                raise RuntimeError(f"Restarted worker exited (exit {worker.returncode})")
            time.sleep(0.5)
        raise TimeoutError(
            f"Job {job_id} did not recover within {timeout}s "
            f"(stale threshold {args.stale_seconds}s)"
        )
    finally:
        stop_owned_worker(worker)


if __name__ == "__main__":
    main()