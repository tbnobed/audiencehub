import time


def run(job_type: str, payload: dict, job_id: int | None = None) -> None:
    if job_type == "identity.resolve_batch":
        from sqlalchemy.orm import Session

        from app.db import engine
        from app.identity.resolver import resolve_batch

        with Session(engine) as db:
            limit = min(max(int(payload.get("limit", 10_000)), 1), 10_000)
            while True:
                result = resolve_batch(db, limit=limit, job_id=job_id)
                db.commit()
                if result["records"] < limit:
                    break
        return
    if job_type == "sleep":
        # Bounded, no-I/O delay for exercising manual worker recovery. Defaults
        # to 60 seconds; shorter values are useful for isolated integration tests.
        seconds = min(max(float(payload.get("seconds", 60)), 0), 60)
        time.sleep(seconds)
        return
    if job_type != "noop":
        raise ValueError("Unknown job type")
    # A bounded delay makes the running state visible without external I/O.
    time.sleep(min(max(float(payload.get("seconds", 0.4)), 0), 30))