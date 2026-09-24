import time


def run(job_type: str, payload: dict) -> None:
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