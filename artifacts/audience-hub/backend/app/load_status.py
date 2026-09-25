"""Read-only, bounded monitoring of a fixed set of imports and their work."""
import time
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

ACTIVE = {"queued", "running"}


def snapshot(engine, ids, tracked_identity):
    with Session(engine) as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        imports = list(db.execute(text("""
            SELECT id, filename, status, rows_total, rows_ok, rows_rejected,
                   warning_count, duration_ms, created_at, started_at, finished_at
            FROM imports WHERE id = ANY(:ids) ORDER BY id
        """), {"ids": ids}).mappings())
        # Retried imports may have historical failed jobs; the newest attempt
        # of each job type is authoritative, not every job ever submitted.
        jobs = list(db.execute(text("""
            SELECT DISTINCT ON (payload->>'import_id', type)
                   id, type, status, payload, finished_at
            FROM jobs WHERE payload->>'import_id' = ANY(:ids)
            ORDER BY payload->>'import_id', type, id DESC
        """), {"ids": [str(i) for i in ids]}).mappings())
        unresolved = db.scalar(text("""
            SELECT count(*) FROM source_records
            WHERE last_import_id = ANY(:ids) AND resolved_at IS NULL
        """), {"ids": ids})
        # Identity batches are globally deduped and have no import_id. Capture
        # active work while these imports run, then follow it through completion.
        if imports:
            unfinished = unresolved or any(i["status"] in ACTIVE for i in imports)
            tracked_identity.update(db.scalars(text("""
                SELECT id FROM jobs WHERE type='identity.resolve_batch'
                AND status IN ('queued','running')
                AND (:unfinished OR created_at BETWEEN :start AND :end)
            """), {"unfinished": bool(unfinished),
                    "start": min(i["created_at"] for i in imports),
                    "end": max((i["finished_at"] or i["created_at"]) for i in imports)}))
        if tracked_identity:
            jobs.extend(db.execute(text("""
                SELECT id, type, status, payload, finished_at FROM jobs
                WHERE id = ANY(:ids)
            """), {"ids": list(tracked_identity)}).mappings())
        totals = db.execute(text("""
            SELECT (SELECT count(*) FROM profiles) AS profiles,
                   (SELECT count(*) FROM profile_merges) AS merges,
                   (SELECT count(*) FROM gifts) AS gifts
        """)).mappings().one()
    return imports, jobs, unresolved, totals


def outcome(ids, imports, jobs, unresolved):
    if not ids:
        return "FAIL", "No imports selected; nothing to monitor."
    if len(imports) != len(ids):
        return "FAIL", "One or more selected imports no longer exist."
    bad = [j for j in jobs if j["status"] not in ACTIVE | {"succeeded"}]
    if bad:
        return "FAIL", "Terminal/unknown job state: " + ", ".join(
            f"{j['id']}={j['status']}" for j in bad)
    for item in imports:
        if item["status"] not in ACTIVE | {"completed"}:
            return "FAIL", f"Import {item['id']} is {item['status']} (not runnable/completed)."
        if item["status"] in ACTIVE and not any(
            j["type"] == "import.run" and j["status"] in ACTIVE
            and str(j["payload"].get("import_id")) == str(item["id"])
            for j in jobs
        ):
            return "FAIL", f"Import {item['id']} has no active import job."
    if any(j["status"] in ACTIVE for j in jobs):
        return "PENDING", "Queued/running work remains."
    if unresolved:
        return "FAIL", f"{unresolved} unresolved source records but no active resolver job."
    return "PASS", "All selected imports and tracked jobs completed."


def report(imports, jobs, unresolved, totals, state, reason, elapsed, emit):
    emit(f"Load status: {state}; monitored={elapsed:.1f}s; "
         f"queued={sum(j['status'] == 'queued' for j in jobs)} "
         f"running={sum(j['status'] == 'running' for j in jobs)} unresolved={unresolved}")
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
    emit(f"Database totals: profiles={totals['profiles']} merges={totals['merges']} "
         f"gifts={totals['gifts']}; total_wallclock={wall}")
    emit(f"{state}: {reason}")


def run_load_status(engine, *, wait=False, import_ids=None, timeout=7200,
                    poll_interval=2, progress_interval=30, clock=time.monotonic,
                    sleep=time.sleep, emit=None):
    """Exit 0=PASS, 1=FAIL, 2=PENDING (snapshot only). Never mutate data."""
    import math
    if any(not math.isfinite(v) or v <= 0
           for v in (timeout, poll_interval, progress_interval)):
        raise ValueError("Monitoring intervals and timeout must be positive.")
    if emit is None:
        emit = lambda line: print(line, flush=True)
    if import_ids is None:
        with Session(engine) as db:
            import_ids = list(db.scalars(text("SELECT id FROM imports ORDER BY id")))
    ids = sorted(set(import_ids))
    tracked_identity = set()
    start = clock()
    next_report = start
    while True:
        imports, jobs, unresolved, totals = snapshot(engine, ids, tracked_identity)
        state, reason = outcome(ids, imports, jobs, unresolved)
        now = clock()
        if state == "PENDING" and wait and now - start >= timeout:
            state, reason = "FAIL", f"Timed out after {timeout:g}s waiting for queued/running work."
        if state != "PENDING" or not wait or now >= next_report:
            report(imports, jobs, unresolved, totals, state, reason, now - start, emit)
            next_report = now + progress_interval
        if state != "PENDING" or not wait:
            return {"PASS": 0, "FAIL": 1, "PENDING": 2}[state]
        sleep(min(poll_interval, max(0, timeout - (now - start)),
                  max(0, next_report - now)))