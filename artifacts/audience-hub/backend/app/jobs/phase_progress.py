"""Durable phase telemetry, committed independently of the data transaction.

Call begin_phase(engine, job_id, phase) before actual work, and
finish_phase(engine, job_id) only after the final data commit. Worker heartbeat
is liveness, not evidence that data counters advanced.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import text


def begin_phase(engine, job_id, phase):
    if job_id is None:
        return
    if phase not in {"importing", "resolving", "attaching", "traits", "rollups"}:
        raise ValueError(f"Unknown load phase: {phase}")
    _record(engine, job_id, phase)


def finish_phase(engine, job_id):
    if job_id is not None:
        _record(engine, job_id, None)


def record_attachment_step(engine, job_id, step, status, elapsed_ms):
    """Independent telemetry only; never modifies the data transaction/phase."""
    if job_id is None:
        return
    timestamp = datetime.now(timezone.utc).isoformat()
    with engine.begin() as db:
        progress = _attempt_progress(db, job_id)
        steps = dict(progress.get("attachment_steps", {}))
        steps[step] = {"status": status, "elapsed_ms": elapsed_ms, "updated_at": timestamp}
        progress.update(attachment_step=step, attachment_steps=steps, updated_at=timestamp)
        db.execute(text("UPDATE jobs SET progress=CAST(:progress AS jsonb) WHERE id=:id"),
                   {"id": job_id, "progress": json.dumps(progress)})


def _record(engine, job_id, phase):
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        progress = _attempt_progress(db, job_id)
        elapsed = dict(progress.get("phase_elapsed_ms", {}))
        previous, started = progress.get("phase"), progress.get("phase_started_at")
        if previous and started:
            elapsed[previous] = elapsed.get(previous, 0) + max(
                0, (now - datetime.fromisoformat(started)).total_seconds() * 1000)
        progress.update(phase=phase, phase_started_at=now.isoformat() if phase else None,
                        updated_at=now.isoformat(), phase_elapsed_ms=elapsed)
        db.execute(text("UPDATE jobs SET progress=CAST(:progress AS jsonb) WHERE id=:id"),
                   {"id": job_id, "progress": json.dumps(progress)})


def _attempt_progress(db, job_id):
    """Never close an earlier attempt's open phase using this attempt's clock."""
    job = db.execute(text("""
        SELECT progress,attempts,started_at FROM jobs WHERE id=:id FOR UPDATE
    """), {"id": job_id}).mappings().one_or_none()
    if job is None:
        raise RuntimeError(f"Missing job {job_id} for phase progress")
    progress = dict(job["progress"] or {})
    attempt_start = job["started_at"].isoformat() if job["started_at"] else None
    if (progress.get("attempt") != job["attempts"]
            or progress.get("attempt_started_at") != attempt_start):
        history = list(progress.get("phase_history", []))
        if any(k in progress for k in ("phase", "phase_elapsed_ms", "attachment_steps")):
            history.append({
                "attempt": progress.get("attempt"),
                "attempt_started_at": progress.get("attempt_started_at"),
                # These counters were closed before interruption; never extend
                # an open interval across queue time or a stopped workspace.
                "completed_phase_elapsed_ms": progress.get("phase_elapsed_ms", {}),
                "attachment_steps": progress.get("attachment_steps", {}),
                "incomplete_phase": progress.get("phase"),
                "timing_scope": ("previous_attempt" if "attempt" in progress else "unknown_legacy"),
            })
        for key in ("phase", "phase_started_at", "phase_elapsed_ms",
                    "attachment_step", "attachment_steps"):
            progress.pop(key, None)
        progress.update(attempt=job["attempts"], attempt_started_at=attempt_start,
                        phase_elapsed_ms={}, phase_history=history[-5:],
                        timing_scope="current_attempt")
    return progress
