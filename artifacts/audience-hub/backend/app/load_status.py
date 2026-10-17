"""Read-only monitoring: cheap job polls, infrequent explicitly scoped metrics."""
import math
import time
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

ACTIVE = {"queued", "running"}
FAILED = {"failed", "cancelled", "canceled"}


def snapshot(engine, ids, tracked_identity):
    with Session(engine) as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        imports = list(db.execute(text("""
            SELECT i.*, b.phase AS batch_phase FROM imports i
            LEFT JOIN identity_batches b ON b.id=i.identity_batch_id
            WHERE i.id=ANY(:ids) ORDER BY i.id
        """), {"ids": ids}).mappings())
        batches = list({str(i["identity_batch_id"]) for i in imports if i["identity_batch_id"]})
        jobs = list(db.execute(text("""
            SELECT DISTINCT ON (payload->>'import_id',payload->>'batch_id',type)
                   id,type,status,payload,progress,attempts,heartbeat_at,started_at,finished_at
            FROM jobs WHERE payload->>'import_id'=ANY(:ids)
               OR payload->>'batch_id'=ANY(:batches)
            ORDER BY payload->>'import_id',payload->>'batch_id',type,id DESC
        """), {"ids": [str(i) for i in ids], "batches": batches}).mappings())
        # Legacy unscoped resolvers only. New batch jobs are scoped durably.
        if imports and not batches:
            tracked_identity.update(db.scalars(text("""
                SELECT id FROM jobs WHERE type='identity.resolve_batch'
                AND NOT (payload ? 'batch_id') AND NOT (payload ? 'import_id')
                AND status IN ('queued','running')
                AND (:unfinished OR created_at BETWEEN :start AND :end OR EXISTS(
                    SELECT 1 FROM source_records sr WHERE sr.resolved_at IS NULL
                    AND sr.last_import_id IN (SELECT id FROM imports WHERE id=ANY(:ids)
                        AND record_type NOT IN ('gift','event'))))
            """), {"unfinished": any(i["status"] in ACTIVE for i in imports),
                    "start": min(i["created_at"] for i in imports),
                    "end": max(i["finished_at"] or i["created_at"] for i in imports),
                    "ids": ids}))
        if tracked_identity:
            jobs.extend(db.execute(text("""
                SELECT id,type,status,payload,progress,attempts,heartbeat_at,started_at,finished_at
                FROM jobs WHERE id=ANY(:ids)
            """), {"ids": list(tracked_identity)}).mappings())
        # Legacy follow-up traits are linked by the resolver's durable dedupe key.
        resolver_ids = [str(j["id"]) for j in jobs if j["type"] == "identity.resolve_batch"]
        if resolver_ids:
            known = {j["id"] for j in jobs}
            jobs.extend(j for j in db.execute(text("""
                SELECT id,type,status,payload,progress,attempts,heartbeat_at,started_at,finished_at
                FROM jobs WHERE dedupe_key=ANY(:keys)
            """), {"keys": [f"traits:after-identity:{i}" for i in resolver_ids]}).mappings()
                        if j["id"] not in known)
    return imports, jobs


def metrics(engine, ids):
    """No huge joins; cached for five minutes, refreshed before final PASS."""
    with Session(engine) as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout='15s'"))
        return dict(db.execute(text("""
            SELECT
              (SELECT count(*) FROM source_records WHERE resolved_at IS NULL
               AND last_import_id IN (SELECT id FROM imports WHERE id=ANY(:ids)
                 AND record_type NOT IN ('gift','event'))) AS unresolved_contacts,
              (SELECT count(*) FROM gifts WHERE profile_id IS NULL) AS unattached_gifts,
              (SELECT count(*) FROM events WHERE profile_id IS NULL) AS unattached_events,
              (SELECT count(*) FROM profiles) AS profiles,
              (SELECT count(*) FROM profile_merges) AS merges,
              (SELECT count(*) FROM gifts) AS gifts
        """), {"ids": ids}).mappings().one())


def outcome(ids, imports, jobs, unresolved=None):
    if not ids:
        return "FAIL", "No imports selected; nothing to monitor."
    if len(imports) != len(ids):
        return "UNKNOWN", "One or more selected imports no longer exist."
    for job in jobs:
        if job["status"] in FAILED:
            return "FAIL", f"Job {job['id']} is {job['status']}."
        if job["status"] not in ACTIVE | {"succeeded"}:
            return "UNKNOWN", f"Job {job['id']} has unknown state {job['status']}."
    for item in imports:
        if item["status"] in FAILED | {"uploaded"}:
            return "FAIL", f"Import {item['id']} is {item['status']}."
        if item["status"] not in ACTIVE | {"completed"}:
            return "UNKNOWN", f"Import {item['id']} has unknown state {item['status']}."
        if item["status"] in ACTIVE and not any(
            j["type"] == "import.run" and j["status"] in ACTIVE
            and str(j["payload"].get("import_id")) == str(item["id"]) for j in jobs
        ):
            return "UNKNOWN", f"Import {item['id']} has no active import job."
    if any(j["status"] in ACTIVE for j in jobs):
        return "PENDING", "Queued/running work remains."
    if any(i.get("batch_phase") not in (None, "completed") for i in imports):
        return "UNKNOWN", "Batch phase barrier is incomplete but no tracked job is active."
    for batch in {i.get("identity_batch_id") for i in imports} - {None}:
        if not any(j["type"] == "traits.recompute" and j["status"] == "succeeded"
                   and str(j["payload"].get("batch_id")) == str(batch) for j in jobs):
            return "UNKNOWN", f"Batch {batch} final traits/rollups completion is not recorded."
    if unresolved is None:
        return "VERIFY", "Verifying contact backlog."
    if unresolved:
        return "UNKNOWN", f"{unresolved} unresolved contacts but no active resolver job."
    return "PASS", "Selected imports, identity, and tracked traits/rollups completed."


def phase_details(jobs):
    now = datetime.now(timezone.utc)
    elapsed = {}
    current = set()
    for job in jobs:
        progress = job.get("progress") or {}
        attempt_start = job.get("started_at")
        scoped = ("attempt" in progress and progress["attempt"] == job.get("attempts")
                  and progress.get("attempt_started_at") ==
                  (attempt_start.isoformat() if attempt_start else None))
        # Legacy timers and an interrupted earlier attempt cannot establish
        # current-attempt elapsed time. The reporter renders these as unknown.
        if not scoped:
            progress = {}
        # Imports predate phase telemetry. Use their actual execution interval,
        # not queue age; summing durations matches the job-seconds label.
        if job["type"] == "import.run" and "importing" not in progress.get("phase_elapsed_ms", {}) \
                and progress.get("phase") != "importing" and job.get("started_at") \
                and (job["status"] == "running" or job.get("finished_at")):
            end = job.get("finished_at") or now
            elapsed["importing"] = elapsed.get("importing", 0) + max(
                0, (end - job["started_at"]).total_seconds())
        for phase, ms in progress.get("phase_elapsed_ms", {}).items():
            elapsed[phase] = elapsed.get(phase, 0) + float(ms) / 1000
        phase = progress.get("phase")
        if job["status"] in ACTIVE:
            phase = phase or {"import.run": "importing", "identity.resolve_batch": "resolving",
                              "traits.recompute": "traits"}.get(job["type"], "unknown")
            current.add(phase)
        started = progress.get("phase_started_at")
        if started and progress.get("phase") and job["status"] != "queued":
            try:
                end = job.get("finished_at") or now
                elapsed[phase] = elapsed.get(phase, 0) + max(
                    0, (end - datetime.fromisoformat(started)).total_seconds())
            except (ValueError, TypeError):
                pass
    return ",".join(sorted(current)) or "none", elapsed


def report(imports, jobs, counts, state, reason, elapsed, emit, metrics_age=0):
    phase, phases = phase_details(jobs)
    emit(f"Load status: {state}; monitored={elapsed:.1f}s; phase={phase}; "
         f"queued={sum(j['status']=='queued' for j in jobs)} "
         f"running={sum(j['status']=='running' for j in jobs)}")
    emit("Phase elapsed (current-attempt job-seconds; legacy/unscoped timing unknown): " + " ".join(
        f"{p}={phases[p]:.1f}s" if p in phases else f"{p}=unknown/not-recorded"
        for p in ("importing", "resolving", "attaching", "traits", "rollups")))
    for row in imports:
        seconds = (row["duration_ms"] or 0) / 1000
        rate = f"{(row['rows_ok'] + row['rows_rejected']) / seconds:.1f}" if seconds else "n/a"
        emit(f"  import {row['id']} {row['filename']}: {row['status']} "
             f"ok={row['rows_ok']} rejected={row['rows_rejected']} "
             f"warn={row['warning_count']} total={row['rows_total']} rows/s={rate}")
    starts = [i["started_at"] or i["created_at"] for i in imports]
    ends = [i["finished_at"] for i in imports if i["finished_at"]]
    ends += [j["finished_at"] for j in jobs if j["finished_at"]]
    end = max(ends) if state == "PASS" and ends else datetime.now(timezone.utc)
    wall = f"{max(0, (end - min(starts)).total_seconds()):.1f}s" if starts else "n/a"
    if counts:
        emit(f"Selected unresolved_contacts={counts['unresolved_contacts']}; "
             f"global unattached_gifts={counts['unattached_gifts']} "
             f"unattached_events={counts['unattached_events']} (not an identity backlog; "
             f"unmatchable rows may remain); metrics_age={metrics_age:.0f}s")
        emit(f"Database totals: profiles={counts['profiles']} merges={counts['merges']} "
             f"gifts={counts['gifts']}; cached_metrics_age={metrics_age:.0f}s; total_wallclock={wall}")
    else:
        emit(f"Metrics unavailable; total_wallclock={wall}")
    emit(f"{state}: {reason}")


def run_load_status(engine, *, wait=False, import_ids=None, timeout=14400,
                    poll_interval=2, progress_interval=30, metrics_interval=300,
                    stale_seconds=120, clock=time.monotonic, sleep=time.sleep, emit=None):
    """Exit 0 PASS, 1 confirmed FAIL, 2 PENDING, 3 STILL RUNNING, 4 UNKNOWN.

    Timeout limits monitoring only; never cancels or mutates work.
    """
    if any(not math.isfinite(v) or v <= 0 for v in (
        timeout, poll_interval, progress_interval, metrics_interval, stale_seconds
    )):
        raise ValueError("Monitoring intervals and timeout must be positive.")
    if emit is None:
        emit = lambda line: print(line, flush=True)
    if import_ids is None:
        with Session(engine) as db:
            import_ids = list(db.scalars(text("SELECT id FROM imports ORDER BY id")))
    ids = sorted(set(import_ids))
    tracked_identity = set()
    start = next_report = clock()
    counts, counted_at = None, -math.inf
    while True:
        imports, jobs = snapshot(engine, ids, tracked_identity)
        state, reason = outcome(ids, imports, jobs)
        now = clock()
        if state == "VERIFY" or now - counted_at >= metrics_interval:
            try:
                counts = metrics(engine, ids)
                counted_at = clock()
            except Exception as exc:
                counts = None
                counted_at = clock()
                emit(f"Metrics unavailable ({type(exc).__name__}); no completion inferred.")
            if state == "VERIFY":
                state, reason = (outcome(ids, imports, jobs, counts["unresolved_contacts"])
                                 if counts else ("UNKNOWN", "Cannot verify contact backlog."))
        now = clock()
        timed_out = wait and now - start >= timeout
        if state == "PENDING" and timed_out:
            utcnow = datetime.now(timezone.utc)
            active = [j for j in jobs if j["status"] in ACTIVE]
            fresh = active and all(j["status"] == "running" and j.get("heartbeat_at")
                and (utcnow - j["heartbeat_at"]).total_seconds() <= stale_seconds for j in active)
            state = "STILL RUNNING" if fresh else "UNKNOWN"
            reason = (f"Monitoring timeout after {timeout:g}s; fresh worker heartbeat, not a load failure."
                      if fresh else f"Monitoring timeout after {timeout:g}s; queued/stale work, liveness unconfirmed.")
        if state != "PENDING" or not wait or now >= next_report:
            report(imports, jobs, counts, state, reason, now - start, emit, now - counted_at)
            next_report = now + progress_interval
        if state != "PENDING" or not wait:
            if state in {"STILL RUNNING", "UNKNOWN", "PENDING"}:
                emit("Resume monitoring (does not restart work): python -m app.cli load-status --wait "
                     + f"--timeout {timeout:g} " + " ".join(f"--import-id {i}" for i in ids))
            return {"PASS": 0, "FAIL": 1, "PENDING": 2, "STILL RUNNING": 3, "UNKNOWN": 4}[state]
        sleep(min(poll_interval, max(0, timeout - (now - start)), max(0, next_report - now)))
