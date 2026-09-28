"""Opt-in disposable M4 acceptance runner; never run against an application DB.

Extends the raw-row approach in scripts/traits_acceptance.py with registry
coverage, timestamp bounds, incremental global ranks, and setup/full-job timing.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time as daytime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
import platform
import random
import resource
import time


def donor_status(days: list[date], as_of: date) -> str:
    days = sorted(d for d in days if d <= as_of)
    if not days:
        return "prospect"
    age = (as_of - days[-1]).days
    if age <= 365:
        if (as_of - days[0]).days <= 365:
            return "new"
        if len(days) > 1 and (days[-1] - days[-2]).days > 730:
            return "reactivated"
        return "active"
    return "lapsing" if age <= 730 else "lapsed"


def quintiles(values: dict, descending: bool = False) -> dict:
    ordered = sorted(values, key=lambda p: (
        values[p] is None,
        -values[p] if descending and values[p] is not None else values[p], p))
    base, remainder = divmod(len(ordered), 5)
    result, offset = {}, 0
    for tile in range(1, 6):
        size = base + (tile <= remainder)
        for p in ordered[offset:offset + size]:
            result[p] = 6 - tile
        offset += size
    return result


def reference(db, sample: list[int], as_of: date) -> dict:
    """Raw rows only: Python aggregation and global donor ranking, no trait SQL."""
    from sqlalchemy import text
    active = set(db.execute(text(
        "SELECT id FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted"
    )).scalars())
    population = {}
    gifts, events, sources = defaultdict(list), defaultdict(list), defaultdict(set)
    selected = set(sample)
    # Stream the entire donor population: SQL deliberately performs no aggregation.
    query = text("SELECT g.*, s.key AS source_key FROM gifts g JOIN sources s ON s.id=g.source_id")
    for g in db.execute(query.execution_options(stream_results=True)).mappings():
        p = g["profile_id"]
        if p in selected:
            sources[p].add(g["source_key"])
        if p not in active or (g["gift_date"] and g["gift_date"] > as_of):
            continue
        count, amount, last = population.get(p, (0, Decimal(0), None))
        day = g["gift_date"]
        population[p] = (count + 1, amount + g["amount"],
                         max(last, day) if last and day else last or day)
        if p in selected:
            gifts[p].append(dict(g))
    end = datetime.combine(as_of + timedelta(days=1), daytime(), timezone.utc)
    for e in db.execute(text(
        "SELECT e.*, s.key AS source_key FROM events e JOIN sources s ON s.id=e.source_id "
        "WHERE e.profile_id=ANY(:ids)"), {"ids": sample}).mappings():
        sources[e["profile_id"]].add(e["source_key"])
        if e["occurred_at"] < end:
            events[e["profile_id"]].append(dict(e))
    for p, key in db.execute(text(
        "SELECT r.profile_id,s.key FROM source_records r JOIN sources s ON s.id=r.source_id "
        "WHERE r.profile_id=ANY(:ids)"), {"ids": sample}):
        sources[p].add(key)
    ranks = [
        quintiles({p: (as_of - v[2]).days if v[2] else None for p, v in population.items()}),
        quintiles({p: v[0] for p, v in population.items()}, True),
        quintiles({p: v[1] for p, v in population.items()}, True),
    ]
    result = {}
    for p in sample:
        gs, es = gifts[p], events[p]
        days = sorted(g["gift_date"] for g in gs if g["gift_date"])
        recent = [g for g in gs if g["gift_date"] and g["gift_date"] >= as_of - timedelta(days=364)]
        recent_events = [e for e in es if e["occurred_at"] >= end - timedelta(days=30)]
        amounts = [g["amount"] for g in gs]
        engagements = [(datetime.combine(g["gift_date"], daytime(), timezone.utc), g["id"], g["source_key"])
                       for g in gs if g["gift_date"]]
        engagements += [(e["occurred_at"], e["id"], e["source_key"]) for e in es]
        scores = [r.get(p) for r in ranks]
        result[p] = dict(
            gift_count_total=len(gs), ltv_total=sum(amounts, Decimal(0)),
            gift_amount_12m=sum((g["amount"] for g in recent), Decimal(0)),
            gift_count_12m=len(recent), first_gift_date=days[0] if days else None,
            last_gift_date=days[-1] if days else None,
            largest_gift_amount=max(amounts) if amounts else None,
            avg_gift_amount=(sum(amounts) / len(amounts)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) if amounts else None,
            is_recurring_active=any(g["is_recurring"] and g["gift_date"] and g["gift_date"] >= as_of - timedelta(days=44) for g in gs),
            days_since_last_gift=(as_of - days[-1]).days if days else None,
            donor_status=donor_status(days, as_of),
            rfm_recency=scores[0], rfm_frequency=scores[1], rfm_monetary=scores[2],
            rfm_score="".join(map(str, scores)) if scores[0] is not None else None,
            event_count_30d=len(recent_events),
            last_event_at=max((e["occurred_at"] for e in es), default=None),
            video_views_30d=sum(e["name"] == "Video Watched" for e in recent_events),
            last_engagement_channel=max(engagements)[2] if engagements else None,
            source_keys=sorted(sources[p]),
        )
    return result


def measure(directory: Path, profiles: int, seed_dir: str | None = None) -> None:
    from sqlalchemy import event, text
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.seed import generate_seed, load_generated_files
    from app.jobs import handlers
    from app.jobs.queue import succeed
    from app.traits.registry import TRAITS_BY_KEY
    from app.traits.engine import recompute_traits, backfill_trait_snapshots
    import app.load_status

    if os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        raise RuntimeError("Use app.benchmark --traits; isolated runner required")
    if engine.url.host or not str(engine.url.query.get("host", "")).startswith("/tmp/kinship-bench-"):
        raise RuntimeError("Refusing non-private benchmark socket")
    pinned = date(2024, 12, 31)
    report = dict(requested_people=profiles, scale="medium", random_seed=20250308,
                  as_of=str(pinned), timings={}, python=platform.python_version(),
                  cpu_count=os.cpu_count(), cpu_quota=Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
                  memory_limit=Path("/sys/fs/cgroup/memory.max").read_text().strip(),
                  discrepancies=[], status="running")

    def checkpoint(stage):
        report["stage"] = stage
        report["peak_python_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        (directory / "traits.json").write_text(json.dumps(report, default=str, indent=2) + "\n")
        print(f"M4 checkpoint: {stage}; timings={report['timings']}", flush=True)

    def timed(name, fn):
        checkpoint(name)
        start = time.perf_counter()
        value = fn()
        report["timings"][name] = time.perf_counter() - start
        checkpoint(name + " complete")
        return value

    checkpoint("resource preflight")
    if seed_dir:
        from app.seed import CSV_FILES
        seed_path = Path(seed_dir).resolve()
        stats = json.loads((seed_path / "generator_stats.json").read_text())
        if (stats["requested_people"], stats["scale"], stats["random_seed"]) != (profiles, "medium", 20250308):
            raise ValueError("Reused seed metadata does not match requested medium fixture")
        generated = {"files": {name: seed_path / name for name in CSV_FILES},
                     "counts": {name: data["rows_generated"] for name, data in stats["files"].items()}}
        report["reused_seed_directory"] = str(seed_path)
    else:
        generated = timed("generate_seed", lambda: generate_seed(
            profiles=profiles, scale="medium", random_seed=20250308, output_dir=directory / "seed"))
    report["generated_counts"] = generated["counts"]

    def drain_imports(*args, **kwargs):
        # Deterministic scheduling only; real loader and import handler are unchanged.
        while True:
            with Session(engine) as db:
                job = db.execute(text("SELECT id,payload FROM jobs WHERE type='import.run' "
                                      "AND status='queued' ORDER BY id LIMIT 1")).mappings().first()
                if not job:
                    break
                db.execute(text("UPDATE jobs SET status='running' WHERE id=:id"), {"id": job["id"]})
                db.commit()
            from app.imports.service import run_import
            run_import(job["payload"]["import_id"])
            with Session(engine) as db:
                succeed(db, job["id"])
                db.commit()
        return 0

    original_wait = app.load_status.run_load_status
    app.load_status.run_load_status = drain_imports
    try:
        timed("real_seed_imports_serial", lambda: load_generated_files(generated["files"]))
    finally:
        app.load_status.run_load_status = original_wait
    timed("identity_resolution", lambda: handlers.run("identity.resolve_batch", {}))
    with Session(engine) as db:
        db.execute(text("UPDATE jobs SET status='cancelled' WHERE status='queued'"))
        db.commit()
        report["counts"] = {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
                            for t in ("profiles", "gifts", "events", "source_records")}
        report["active_profiles"] = db.execute(text(
            "SELECT count(*) FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted")).scalar_one()
        report["unresolved_records"] = db.execute(text(
            "SELECT count(*) FROM source_records WHERE resolved_at IS NULL")).scalar_one()
        report["imports"] = [dict(r) for r in db.execute(text(
            "SELECT filename,status,rows_total,rows_ok,rows_rejected FROM imports ORDER BY id")).mappings()]
        db.execute(text("ANALYZE"))
        db.commit()

    sql_times = defaultdict(float)
    def before(conn, cursor, statement, parameters, context, executemany):
        context.m4_start = time.perf_counter()
    def after(conn, cursor, statement, parameters, context, executemany):
        label = ("trait_upsert" if "INSERT INTO profile_traits" in statement else
                 "monthly_snapshot" if "INSERT INTO trait_snapshots" in statement else
                 "other_including_dashboard_rollups")
        sql_times[label] += time.perf_counter() - context.m4_start
    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    start_clock = datetime.now(timezone.utc)
    from unittest.mock import patch
    import app.traits.engine as trait_engine
    import app.dashboards.rollups as rollups
    original_rollups = rollups.refresh_dashboard_rollups
    def measured_rollups(db, **kwargs):
        return timed("dashboard_rollups", lambda: original_rollups(db, **kwargs))
    def measured_recompute(db, **kwargs):
        updated = timed("recompute_including_snapshot_and_dashboards",
                        lambda: recompute_traits(db, as_of=pinned, **kwargs))
        report["updated_profiles"] = updated
        return updated
    def measured_backfill(db):
        return timed("historical_snapshot_backfill", lambda: backfill_trait_snapshots(db, as_of=pinned))
    with patch.object(trait_engine, "recompute_traits", measured_recompute), \
         patch.object(trait_engine, "backfill_trait_snapshots", measured_backfill), \
         patch.object(rollups, "refresh_dashboard_rollups", measured_rollups):
        timed("full_production_path_including_commit",
              lambda: handlers.run("traits.recompute", {"mode": "full"}))
    end_clock = datetime.now(timezone.utc)
    event.remove(engine, "before_cursor_execute", before)
    event.remove(engine, "after_cursor_execute", after)
    report["sql_seconds"] = dict(sql_times)
    with Session(engine) as db:
        ids = list(db.execute(text("SELECT id FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted ORDER BY id")).scalars())
        sample = sorted(random.Random(20250308).sample(ids, min(1000, len(ids))))
        report["sample_ids"] = sample
        expected = timed("independent_python_reference", lambda: reference(db, sample, pinned))
        assert set(next(iter(expected.values()))) | {"computed_at"} == set(TRAITS_BY_KEY)
        observed = list(db.execute(text("SELECT * FROM profile_traits WHERE profile_id=ANY(:ids)"), {"ids": sample}).mappings())
        assert len(observed) == len(sample), "Missing computed sample profiles"
        for row in observed:
            for key in TRAITS_BY_KEY:
                ok = (start_clock <= row[key] <= end_clock if key == "computed_at"
                      else row[key] == expected[row["profile_id"]][key])
                if not ok:
                    report["discrepancies"].append(dict(profile_id=row["profile_id"], key=key,
                        actual=row[key], expected=expected[row["profile_id"]].get(key, "within full job wall-clock interval")))
        report["snapshot_months"] = db.execute(text("SELECT count(DISTINCT month) FROM trait_snapshots")).scalar_one()
        report["database_bytes"] = db.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
        report["compared_profiles"] = len(expected)
        report["compared_traits"] = list(TRAITS_BY_KEY)
        # Incremental recompute must retain global, not sample-only, ranks.
        recompute_traits(db, as_of=pinned, profile_ids=sample[:17], write_snapshot=False)
        for row in db.execute(text("SELECT * FROM profile_traits WHERE profile_id=ANY(:ids)"), {"ids": sample[:17]}).mappings():
            for key in ("rfm_recency", "rfm_frequency", "rfm_monetary", "rfm_score"):
                if row[key] != expected[row["profile_id"]][key]:
                    report["discrepancies"].append(dict(profile_id=row["profile_id"], key="incremental_" + key))
        db.rollback()
    report["status"] = "passed" if not report["discrepancies"] and not report["unresolved_records"] else "failed"
    checkpoint("finished")
    if report["status"] != "passed":
        raise AssertionError("M4 discrepancies: see traits.json")