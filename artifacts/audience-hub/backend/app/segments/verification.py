"""Isolated segment benchmark and independent raw-data plausibility checks."""
from datetime import date, datetime, time as daytime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import time

# Hand-written population queries, intentionally independent of the DSL/compiler.
# Each statement derives membership from raw gifts/events/consents and independently
# checks the appropriate profile_traits predicate. Exact ranges are [count,count].
REFERENCE_SQL = {
    "lapsed-high-value": """
SELECT count(*) FROM active_profiles p JOIN profile_traits t ON t.profile_id=p.id
WHERE t.donor_status='lapsed' AND t.ltv_total>=500
AND p.id IN (SELECT profile_id FROM gifts WHERE gift_date<=:as_of
 GROUP BY profile_id HAVING max(gift_date)<CAST(:as_of AS date)-730 AND sum(amount)>=500)
""",
    "new-partners": """
SELECT count(*) FROM active_profiles p JOIN profile_traits t ON t.profile_id=p.id
WHERE t.first_gift_date BETWEEN CAST(:as_of AS date)-89 AND :as_of
AND p.id IN (SELECT profile_id FROM gifts WHERE gift_date<=:as_of
 GROUP BY profile_id HAVING min(gift_date)>=CAST(:as_of AS date)-89)
""",
    "recurring-partners": """
SELECT count(*) FROM active_profiles p JOIN profile_traits t ON t.profile_id=p.id
WHERE t.is_recurring_active AND EXISTS (SELECT 1 FROM gifts g WHERE g.profile_id=p.id
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
SELECT count(*) FROM ranked r JOIN profile_traits t ON t.profile_id=r.id
WHERE r.position<=ceil(r.population*0.05) AND t.gift_amount_12m=r.amount
""",
    "engaged-not-giving": """
SELECT count(*) FROM active_profiles p JOIN profile_traits t ON t.profile_id=p.id
WHERE t.video_views_30d>=3 AND t.gift_count_total=0
AND NOT EXISTS (SELECT 1 FROM gifts g WHERE g.profile_id=p.id AND g.gift_date<=:as_of)
AND (SELECT count(*) FROM events e WHERE e.profile_id=p.id AND e.name='Video Watched'
 AND e.occurred_at>=CAST(:as_of AS date)-29
 AND e.occurred_at<CAST(:as_of AS date)+1)>=3
""",
    "lapsing-opted-in": """
SELECT count(*) FROM active_profiles p JOIN profile_traits t ON t.profile_id=p.id
WHERE t.gift_count_12m=0 AND t.gift_count_total>=2
AND EXISTS (SELECT 1 FROM consents c WHERE c.profile_id=p.id
 AND c.channel='email' AND c.status='opted_in')
AND p.id IN (SELECT profile_id FROM gifts WHERE gift_date<=:as_of
 GROUP BY profile_id HAVING count(*)>=2 AND max(gift_date)<CAST(:as_of AS date)-364)
""",
}


def measure(directory: Path, profiles: int, *, scale="medium", seed_dir=None):
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
    pinned = date(2024, 12, 31)
    instant = datetime.combine(pinned, daytime(), timezone.utc)
    report = {"requested_people": profiles, "scale": scale, "random_seed": 20250308,
              "as_of": str(pinned), "status": "running", "setup_seconds": {},
              "templates": [], "seed_sha256": {}, "reference_sql": REFERENCE_SQL,
              "cold_definition": "First preview in a fresh connection pool after fixture preparation; PostgreSQL/OS caches are NOT flushed.",
              "warm_definition": "Median of three further previews in the same session.",
              "scope": "Preview includes validation/compiler/count and timeout guard; sample includes 25 rows and breakdowns; materialize includes membership/history/audit and commit. Setup excluded."}

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
            generated = {"files": {name: seed_path / name for name in CSV_FILES}}
        else:
            generated = setup("generate_seed", lambda: generate_seed(
                profiles=profiles, scale=scale, random_seed=20250308, output_dir=directory / "seed"))
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
            ensure_templates(db)
            db.execute(text("UPDATE segments SET status='active'"))
            db.commit()
            report["population"] = {table: db.scalar(text(f"SELECT count(*) FROM {table}"))
                                    for table in ("active_profiles", "gifts", "events", "source_records")}
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
                passed = cold["count"] == materialized == actual == expected
                entry = {"key": key, "name": name, "count": cold["count"],
                         "preview_cold_ms": cold_ms, "preview_warm_ms": warm,
                         "preview_warm_median_ms": statistics.median(warm),
                         "sample_ms": sample_ms, "sample_rows": len(sample_result["items"]),
                         "materialize_ms": materialize_ms, "materialized_count": materialized,
                         "expected_range": [expected, expected], "pass": passed}
                report["templates"].append(entry)
                checkpoint()
                print(f"{key} | {actual} | {cold_ms:.2f} | {statistics.median(warm):.2f} | {sample_ms:.2f} | {materialize_ms:.2f} | {'PASS' if passed else 'FAIL'}", flush=True)
        report["status"] = "passed" if len(report["templates"]) == 6 and all(r["pass"] for r in report["templates"]) else "failed"
        checkpoint()
        if report["status"] != "passed":
            raise RuntimeError("Segment template plausibility failed; see segments.json")
    except BaseException:
        report["status"] = "failed"
        checkpoint()
        raise
