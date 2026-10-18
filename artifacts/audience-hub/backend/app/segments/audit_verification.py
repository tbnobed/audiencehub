"""Step 5: independent identity-set parity and measured public segment operations."""
import json
import os
import statistics
import time
from datetime import datetime, time as midnight, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.segments.verification import reference_date


def assert_isolated(engine):
    if os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        raise RuntimeError("Use --audit-segments in the isolated benchmark runner")
    schema = os.environ.get("KINSHIP_BENCHMARK_SCHEMA")
    if schema:
        from app.schema_benchmark import assert_private
        with engine.connect() as connection:
            assert_private(connection, schema)
    elif engine.url.host or not str(engine.url.query.get("host", "")).startswith("/tmp/kinship-bench-"):
        raise RuntimeError("Refusing non-private benchmark socket")


def benchmark_loaded(engine, directory, *, as_of=None):
    """Reuse an already imported PRIVATE database, never a production population.

    Identity comparisons use independent source-record SQL, not traits, generated
    cohort tags or compiler counts. Peak identity storage is one template at a time.
    """
    from app.models import Segment, SegmentMembership, User
    from app.seed_audit import TARGETS, DENOMINATOR
    from app.seed_audit_verification import prepare_audience_oracle
    from app.segments.api import Preview, preview, sample
    from app.segments.compiler import compile_definition
    from app.segments.service import ensure_templates, materialize
    from app.segments.templates import template_definitions

    assert_isolated(engine)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    pinned = reference_date(as_of)
    instant = datetime.combine(pinned, midnight(), timezone.utc)
    report = {
        "as_of": str(pinned), "denominator": DENOMINATOR, "status": "running", "templates": [],
        "cold_definition": "Application first execution of each operation for this template in a fresh session, not OS/PostgreSQL cold. Count precedes preview; underlying database pages may already be warm. No application result cache is used.",
        "warm_definition": "Three repetitions in the same session.",
        "scope": "Private imported database; compiler/count, API preview/sample and committed materialization. No exports.",
        "reference_parameters": {"as_of": instant.isoformat(), "timezone": "UTC",
                                 "relative_window": "[as_of - interval, as_of)"},
    }

    def checkpoint():
        (directory / "audience-acceptance.json").write_text(json.dumps(report, indent=2, default=str) + "\n")

    def timed(fn):
        start = time.perf_counter()
        result = fn()
        return result, (time.perf_counter() - start) * 1000

    with Session(engine) as db:
        ensure_templates(db)
        keys = [t["key"] for t in template_definitions()]
        segments = list(db.scalars(select(Segment).where(Segment.template_key.in_(keys)).order_by(Segment.id)))
        for segment in segments:
            segment.status = "active"
        db.commit()
        templates = [(s.id, s.template_key, s.name, s.definition) for s in segments]
        report["profiles"] = db.scalar(text("SELECT count(*) FROM active_profiles"))
    checkpoint()
    user = User(id=1, subject="audit-benchmark", email="benchmark@example.org", name="Benchmark", role="admin")
    try:
        # This transaction owns its temporary canonical table until all comparisons
        # finish; materialization commits happen in separate sessions.
        with Session(engine) as oracle:
            predicates, params = prepare_audience_oracle(oracle, pinned)
            for ident, key, name, definition in templates:
                sql = f"SELECT id FROM audit_people p WHERE {predicates[name]}"
                expected_ids, oracle_ms = timed(lambda: set(oracle.scalars(text(sql), params)))
                body = Preview(definition=definition, as_of=instant)
                with Session(engine) as db:
                    def count():
                        return db.scalar(select(func.count()).select_from(compile_definition(db, definition, instant).subquery()))
                    cold_count, count_ms = timed(count)
                    count_warm = [timed(count) for _ in range(3)]
                    cold, preview_ms = timed(lambda: preview(body, user=user, db=db))
                    warm = [timed(lambda: preview(body, user=user, db=db)) for _ in range(3)]
                    sample_result, sample_ms = timed(lambda: sample(body, user=user, db=db))
                    actual_ids = set(db.scalars(compile_definition(db, definition, instant)))
                    db.rollback()
                    def refresh():
                        result = materialize(db, ident, instant)
                        db.commit()
                        return result
                    materialized, materialize_ms = timed(refresh)
                    stored_ids = set(db.scalars(select(SegmentMembership.profile_id).where(SegmentMembership.segment_id == ident)))
                expected = report["profiles"] * TARGETS[name] / DENOMINATOR
                tolerance = max(.2 * expected, 25)
                missing, unexpected = expected_ids - actual_ids, actual_ids - expected_ids
                passed = (
                    not missing and not unexpected and stored_ids == actual_ids
                    and cold_count == cold["count"] == materialized == len(expected_ids)
                    and all(value == cold_count for value, _ in count_warm)
                    and all(value["count"] == cold_count for value, _ in warm)
                    and all(item["id"] in expected_ids for item in sample_result["items"])
                    and abs(cold_count - expected) <= tolerance
                )
                report["templates"].append({
                    "key": key, "name": name, "definition": definition, "count": cold_count,
                    "oracle_count": len(expected_ids), "oracle_sql": sql, "oracle_ms": oracle_ms,
                    "missing_count": len(missing), "unexpected_count": len(unexpected),
                    "missing_sample": sorted(missing)[:25], "unexpected_sample": sorted(unexpected)[:25],
                    "expected": expected, "tolerance": tolerance, "pass": passed,
                    "count_cold_ms": count_ms, "count_warm_ms": [ms for _, ms in count_warm],
                    "preview_cold_ms": preview_ms, "preview_warm_ms": [ms for _, ms in warm],
                    "preview_warm_median_ms": statistics.median(ms for _, ms in warm),
                    "sample_ms": sample_ms, "sample_rows": len(sample_result["items"]),
                    "materialize_ms": materialize_ms, "materialized_count": materialized,
                    "materialized_identity_match": stored_ids == actual_ids,
                })
                checkpoint()
                print(f"{key} | {cold_count} | preview cold ms {preview_ms:.2f} | materialize ms {materialize_ms:.2f} | {'PASS' if passed else 'FAIL'}", flush=True)
        report["status"] = "passed" if len(report["templates"]) == 23 and all(t["pass"] for t in report["templates"]) else "failed"
        checkpoint()
        if report["status"] != "passed":
            raise RuntimeError("Audience acceptance failed; see audience-acceptance.json")
        return report
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        checkpoint()
        raise


def measure(directory, profiles=50_000, *, scale="small", seed_dir=None, as_of=None):
    """Fresh audit import followed by parity; outer benchmark owns DB cleanup."""
    from app.db import engine
    from app.seed_audit_verification import measure as import_audit
    assert_isolated(engine)
    pinned = reference_date(as_of)
    seed_report = import_audit(directory, profiles, scale=scale, seed_dir=seed_dir, as_of=pinned)
    if not seed_report["passed"]:
        raise RuntimeError("Audit seed acceptance failed; see audit-seed.json")
    return benchmark_loaded(engine, directory, as_of=pinned)
