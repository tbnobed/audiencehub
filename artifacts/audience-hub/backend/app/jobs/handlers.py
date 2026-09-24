import time


def run(job_type: str, payload: dict) -> None:
    if job_type != "noop":
        raise ValueError("Unknown job type")
    # A bounded delay makes the running state visible without external I/O.
    time.sleep(min(max(float(payload.get("seconds", 0.4)), 0), 30))