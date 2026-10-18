"""Isolated segment benchmark and independent raw-data plausibility checks."""
from datetime import date, datetime, time as daytime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import time

# Hand-written population queries, intentionally independent of the DSL/compiler.
# Each statement derives membership from raw gifts/events/consents without reading
# profile_traits or compiler output. Exact ranges are [count,count].
REFERENCE_SQL = {
    "lapsed-high-value": """
SELECT count(*) FROM active_profiles p
WHERE p.id IN (SELECT profile_id FROM gifts WHERE gift_date IS NULL OR gift_date<=:as_of
 GROUP BY profile_id HAVING max(gift_date)<CAST(:as_of AS date)-730 AND sum(amount)>=500)
""",
    "new-partners": """
SELECT count(*) FROM active_profiles p
WHERE p.id IN (SELECT profile_id FROM gifts WHERE gift_date<=:as_of
 GROUP BY profile_id HAVING min(gift_date) BETWEEN CAST(:as_of AS date)-89 AND :as_of)
""",
    "recurring-partners": """
SELECT count(*) FROM active_profiles p
WHERE EXISTS (SELECT 1 FROM gifts g WHERE g.profile_id=p.id
 AND g.is_recurring AND g.gift_date BETWEEN CAST(:as_of AS date)-44 AND :as_of)
""",
    "top-giving": """
WITH totals AS (
 SELECT p.id, sum(g.amount) AS amount FROM active_profiles p
 JOIN gifts g ON g.profile_id=p.id
 WHERE g.gift_date BETWEEN CAST(:as_of AS date)-364 AND :as_of
 GROUP BY p.id HAVING sum(g.amount)>0
), ranked AS (
 SELECT id, amount, rank() OVER(ORDER BY amount DESC) AS position,
 count(*) OVER() AS population FROM totals
)
SELECT count(*) FROM ranked r
WHERE r.position<=ceil(r.population*0.05)
""",
    "engaged-not-giving": """
SELECT count(*) FROM active_profiles p
WHERE NOT EXISTS (SELECT 1 FROM gifts g WHERE g.profile_id=p.id
 AND (g.gift_date IS NULL OR g.gift_date<=:as_of))
AND (SELECT count(*) FROM events e WHERE e.profile_id=p.id AND e.name='Video Watched'
  AND e.occurred_at>=((CAST(:as_of AS date)-29)::timestamp AT TIME ZONE 'UTC')
  AND e.occurred_at<((CAST(:as_of AS date)+1)::timestamp AT TIME ZONE 'UTC'))>=3
""",
    "lapsing-opted-in": """
SELECT count(*) FROM active_profiles p
WHERE EXISTS (SELECT 1 FROM consents c WHERE c.profile_id=p.id
 AND c.channel='email' AND c.status='opted_in')
AND p.id IN (SELECT profile_id FROM gifts WHERE gift_date IS NULL OR gift_date<=:as_of
 GROUP BY profile_id HAVING count(*)>=2
 AND count(*) FILTER(WHERE gift_date BETWEEN CAST(:as_of AS date)-364 AND :as_of)=0)
""",
}


def reference_date(as_of: date | str | None = None) -> date:
    """One explicit UTC calendar day for generation, traits and segment windows."""
    if as_of is None:
        return datetime.now(timezone.utc).date()
    if isinstance(as_of, str):
        return date.fromisoformat(as_of)
    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        raise ValueError("as_of must be a date or an ISO YYYY-MM-DD string")
    return as_of


def acceptance_summary(db, as_of: date) -> dict:
    """Read the actual published generation, not a synthetic/date-shifted fixture."""
    from sqlalchemy import text
    counts = dict(db.execute(text("""
        SELECT t.donor_status, count(*) FROM profile_traits t
        JOIN active_profiles p ON p.id=t.profile_id
        WHERE t.gift_count_total>0 GROUP BY t.donor_status
    """)).all())
    givers = sum(counts.values())
    active_ish = sum(counts.get(key, 0) for key in ("new", "active", "reactivated"))
    groups = {"active_ish": active_ish, "lapsing": counts.get("lapsing", 0),
              "lapsed": counts.get("lapsed", 0)}
    recent = dict(db.execute(text("""
        SELECT count(DISTINCT day) AS days, COALESCE(sum(gift_count),0) AS gifts,
               COALESCE(sum(gift_amount),0) AS amount
        FROM dashboard_daily WHERE active
          AND day BETWEEN CAST(:as_of AS date)-364 AND CAST(:as_of AS date)
    """), {"as_of": as_of}).mappings().one())
    published = {r["key"]: dict(r) for r in db.execute(text("""
        SELECT key,value,as_of FROM dashboard_kpis
        WHERE key IN ('generation','partner_status','recurring_partners','lapsing')
    """)).mappings()}
    status_counts = dict(db.execute(text("""
        SELECT t.donor_status,count(*) FROM profile_traits t
        JOIN active_profiles p ON p.id=t.profile_id GROUP BY t.donor_status
    """)).all())
    recurring = db.scalar(text("""
        SELECT count(*) FROM profile_traits t JOIN active_profiles p ON p.id=t.profile_id
        WHERE t.is_recurring_active
    """))
    published_counts = {r["status"]: r["count"]
                        for r in published.get("partner_status", {}).get("value", [])}
    publication_matches = (
        len(published) == 4 and all(r["as_of"] == as_of for r in published.values())
        and published_counts == status_counts
        and published["recurring_partners"]["value"] == recurring
        and published["lapsing"]["value"] == counts.get("lapsing", 0)
    )
    return {
        "as_of": str(as_of), "givers": givers, "donor_status_counts": counts,
        "giver_groups": {key: {"count": value,
                              "share_pct": 100 * value / givers if givers else 0}
                         for key, value in groups.items()},
        "new": counts.get("new", 0), "reactivated": counts.get("reactivated", 0),
        "recurring_active": recurring,
        "dashboard_last_12m": {**recent, "nonempty": recent["gifts"] > 0 and recent["amount"] > 0},
        "current_status_publication_matches": publication_matches,
    }


def acceptance_passed(summary: dict, templates: list[dict]) -> bool:
    """No empty template may pass simply because both independent counts are zero."""
    return (
        summary["givers"] > 0
        and summary["dashboard_last_12m"]["nonempty"]
        and summary["current_status_publication_matches"]
        and 35 <= summary["giver_groups"]["active_ish"]["share_pct"] <= 45
        and 10 <= summary["giver_groups"]["lapsing"]["share_pct"] <= 15
        and all(summary[key] > 0 for key in ("new", "reactivated", "recurring_active"))
        and all(group["count"] > 0 for group in summary["giver_groups"].values())
        and {row["key"] for row in templates} == set(REFERENCE_SQL)
        and len(templates) == len(REFERENCE_SQL)
        and all(row["pass"] and row["count"] > 0 for row in templates)
    )


def measure(directory: Path, profiles: int, *, scale="medium", seed_dir=None,
            as_of: date | str | None = None):
    from sqlalchemy import select, func, text
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.seed import generate_seed, load_generated_files, CSV_FILES
    from app.jobs import handlers
    from app.jobs.queue import succeed
    from app.traits.engine import recompute_traits
    from app.segments.compiler import compile_definition
    from app.segments.service import ensure_templates, materialize
    from app.segments.api import sample, Preview, preview
    from app.models import Segment, User
    import app.load_status

    if os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        raise RuntimeError("Use kinship benchmark --segments; isolated runner required")
    schema = os.environ.get("KINSHIP_BENCHMARK_SCHEMA")
    if schema:
        from app.schema_benchmark import assert_private
        with engine.connect() as connection:
            assert_private(connection, schema)
    elif engine.url.host or not str(engine.url.query.get("host", "")).startswith("/tmp/kinship-bench-"):
        raise RuntimeError("Refusing non-private benchmark socket")
    pinned = reference_date(as_of)
    instant = datetime.combine(pinned, daytime(), timezone.utc)
    report = {"requested_people": profiles, "scale": scale, "random_seed": 20250308,
              "as_of": str(pinned), "status": "running", "setup_seconds": {},
              "reference_parameters": {"as_of": str(pinned), "timezone": "UTC",
                                       "segment_as_of": instant.isoformat()},
              "templates": [], "seed_sha256": {}, "reference_sql": REFERENCE_SQL,
              "cold_definition": "First preview in a fresh connection pool after fixture preparation; PostgreSQL/OS caches are NOT flushed.",
              "warm_definition": "Median of three further previews in the same session.",
              "scope": "Preview includes validation/compiler/count and timeout guard; sample includes ordered LIMIT 25 and hydration, not optional full-audience breakdowns; materialize includes membership/history/audit and commit. Setup excluded."}

    def checkpoint():
        (directory / "segments.json").write_text(json.dumps(report, indent=2, default=str) + "\n")

    def setup(label, fn):
        print(f"Segment benchmark setup: {label}", flush=True)
        start = time.perf_counter()
        value = fn()
        report["setup_seconds"][label] = time.perf_counter() - start
        checkpoint()
        return value

    checkpoint()
    try:
        if seed_dir:
            seed_path = Path(seed_dir).resolve()
            stats = json.loads((seed_path / "generator_stats.json").read_text())
            if (stats["requested_people"], stats["scale"], stats["random_seed"]) != (profiles, scale, 20250308):
                raise ValueError("Reused seed metadata does not match requested fixture")
            if stats.get("as_of") != str(pinned):
                raise ValueError("Reused seed as_of does not match the verification reference date")
            generated = {"files": {name: seed_path / name for name in CSV_FILES}}
        else:
            generated = setup("generate_seed", lambda: generate_seed(
                profiles=profiles, scale=scale, random_seed=20250308,
                output_dir=directory / "seed", as_of=pinned, mode="legacy"))
        for name, path in generated["files"].items():
            with Path(path).open("rb") as stream:
                report["seed_sha256"][name] = hashlib.file_digest(stream, "sha256").hexdigest()

        def drain_imports(*args, **kwargs):
            while True:
                with Session(engine) as db:
                    job = db.execute(text("SELECT id,payload FROM jobs WHERE type='import.run' AND status='queued' ORDER BY id LIMIT 1")).mappings().first()
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
            setup("real_csv_imports", lambda: load_generated_files(generated["files"]))
        finally:
            app.load_status.run_load_status = original_wait
        setup("identity_resolution", lambda: handlers.run("identity.resolve_batch", {}))
        with Session(engine) as db:
            db.execute(text("UPDATE jobs SET status='cancelled' WHERE status='queued'"))
            setup("traits", lambda: recompute_traits(db, as_of=pinned))
            from app.segments.templates import legacy_template_definitions
            ensure_templates(db, legacy_template_definitions())
            db.execute(text("UPDATE segments SET status='active'"))
            db.commit()
            report["population"] = {table: db.scalar(text(f"SELECT count(*) FROM {table}"))
                                    for table in ("active_profiles", "gifts", "events", "source_records")}
            report["acceptance"] = acceptance_summary(db, pinned)
            unresolved = db.scalar(text("SELECT count(*) FROM source_records WHERE resolved_at IS NULL"))
            if unresolved:
                raise RuntimeError("Segment fixture still has unresolved source records")
            db.execute(text("ANALYZE"))
            db.commit()
            templates = [(s.id, s.template_key, s.name, s.definition)
                         for s in db.scalars(select(Segment).order_by(Segment.id)) if s.template_key]
        user = User(id=1, subject="benchmark", email="benchmark@example.org", name="Benchmark", role="admin")
        print("template | count | preview cold ms | preview warm median ms | sample ms | materialize ms | plausibility", flush=True)
        for segment_id, key, name, definition in templates:
            body = Preview(definition=definition, as_of=instant)
            engine.dispose()
            with Session(engine) as db:
                start = time.perf_counter()
                cold = preview(body, user=user, db=db)
                cold_ms = (time.perf_counter() - start) * 1000
                warm = []
                for _ in range(3):
                    start = time.perf_counter()
                    result = preview(body, user=user, db=db)
                    warm.append((time.perf_counter() - start) * 1000)
                    assert result["count"] == cold["count"]
                start = time.perf_counter()
                sample_result = sample(body, user=user, db=db)
                sample_ms = (time.perf_counter() - start) * 1000
                db.rollback()
                start = time.perf_counter()
                materialized = materialize(db, segment_id, instant)
                db.commit()
                materialize_ms = (time.perf_counter() - start) * 1000
                expected = db.scalar(text(REFERENCE_SQL[key]), {"as_of": pinned})
                actual_ids = compile_definition(db, definition, instant)
                # Count recheck after materialization guards against a mismatched saved definition.
                actual = db.scalar(select(func.count()).select_from(actual_ids.subquery()))
                passed = cold["count"] == materialized == actual == expected and actual > 0
                entry = {"key": key, "name": name, "count": cold["count"],
                         "preview_cold_ms": cold_ms, "preview_warm_ms": warm,
                         "preview_warm_median_ms": statistics.median(warm),
                         "sample_ms": sample_ms, "sample_rows": len(sample_result["items"]),
                         "materialize_ms": materialize_ms, "materialized_count": materialized,
                         "expected_range": [expected, expected], "pass": passed}
                report["templates"].append(entry)
                checkpoint()
                print(f"{key} | {actual} | {cold_ms:.2f} | {statistics.median(warm):.2f} | {sample_ms:.2f} | {materialize_ms:.2f} | {'PASS' if passed else 'FAIL'}", flush=True)
        report["status"] = "passed" if acceptance_passed(report["acceptance"], report["templates"]) else "failed"
        checkpoint()
        if report["status"] != "passed":
            raise RuntimeError("Segment template plausibility failed; see segments.json")
    except BaseException:
        report["status"] = "failed"
        checkpoint()
        raise
