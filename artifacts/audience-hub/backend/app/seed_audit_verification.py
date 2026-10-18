"""Isolated real-import Step 4 acceptance; no segment/template dependencies."""
import json
import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.orm import Session


def prepare_audience_oracle(db, as_of):
    """Independent PostgreSQL predicates over resolved source evidence and events."""
    # Per-field priority matches canonical access, but never calls its resolver
    # or the segment compiler. Include master profiles even if evidence is absent.
    db.execute(text("""
        CREATE TEMP TABLE audit_people ON COMMIT DROP AS
        SELECT p.id,p.master_id,p.phone,COALESCE(a.attrs,'{}'::jsonb) attrs
        FROM active_profiles p LEFT JOIN (
          SELECT profile_id,jsonb_object_agg(key,value) attrs FROM (
            SELECT DISTINCT ON (sr.profile_id,j.key) sr.profile_id,j.key,j.value
            FROM source_records sr JOIN sources s ON s.id=sr.source_id
            CROSS JOIN LATERAL jsonb_each(sr.attributes) j
            WHERE sr.profile_id IS NOT NULL AND j.value <> 'null'::jsonb
            ORDER BY sr.profile_id,j.key,s.priority,sr.updated_at DESC,sr.id
          ) ranked GROUP BY profile_id
        ) a ON a.profile_id=p.id
    """))
    db.execute(text("CREATE INDEX ON audit_people(id)"))
    db.execute(text("ANALYZE audit_people"))
    params = {"as_of": as_of, "recent": as_of-timedelta(days=30),
              "active": as_of-timedelta(days=120), "app": as_of-timedelta(days=720)}
    from app.seed_dates import seed_window
    params["start"] = seed_window(as_of).start
    attr = lambda key: f"attrs->>'{key}'"
    exists = lambda key: f"NULLIF(btrim({attr(key)}),'') IS NOT NULL"
    yes = lambda key: f"{attr(key)}='true'"
    number = lambda key: f"NULLIF({attr(key)},'')::numeric"
    plan = f"{attr('tbn_plus_subscription')} IN ('basic','premium_monthly','premium_annual')"
    basic = f"{attr('tbn_plus_subscription')}='basic'"
    account = exists("donor_account_number")
    def donation(cutoff):
        # Independent raw-evidence oracle: do not read last_donation_at or use
        # the compiler/projection helper to prove its own result.
        eligible = ""
        if cutoff in ("app", "recent"):
            eligible = """
                AND COALESCE(e.properties->>'is_refund','false')<>'true'
                AND NOT EXISTS (
                  SELECT 1 FROM (
                    SELECT r.profile_id,r.source_id,r.properties AS evidence
                    FROM events r WHERE r.name='donation_return'
                    UNION ALL
                    SELECT g.profile_id,g.source_id,g.attributes AS evidence
                    FROM gifts g WHERE g.amount<0 OR g.attributes->>'is_refund'='true'
                  ) returned
                  WHERE returned.profile_id=e.profile_id AND returned.source_id=e.source_id
                    AND (returned.evidence->>'original_donation_id' IN (
                          e.message_id,'gift:'||e.message_id,'donation:'||e.message_id,
                          'donation:_event:'||e.message_id)
                      OR returned.evidence->>'original_gift_external_id' IN (
                          e.message_id,'gift:'||e.message_id,'donation:'||e.message_id,
                          'donation:_event:'||e.message_id))
                )
            """
        return ("p.id IN (SELECT e.profile_id FROM events e WHERE e.name='donation' "
                f"AND e.occurred_at>=:{cutoff} AND e.occurred_at<:as_of {eligible})")
    predicates = {
        "All TBN Reference": "master_id IS NOT NULL",
        "Has Active Email": yes("has_active_email"),
        "Active (120 days)": "(NULLIF(attrs->>'last_seen','')::timestamptz>=:active AND NULLIF(attrs->>'last_seen','')::timestamptz<:as_of) OR (NULLIF(attrs->>'last_contact','')::timestamptz>=:active AND NULLIF(attrs->>'last_contact','')::timestamptz<:as_of)",
        "TBN Plus Subscription": plan,
        "TBN Plus Free Users": basic,
        "TBN Plus Premium": f"{attr('tbn_plus_subscription')} IN ('premium_monthly','premium_annual')",
        "Premium Upgraders": f"{attr('tbn_plus_subscription_update')} LIKE 'Upgrade%'",
        "TBN App Non-Donors": f"{plan} AND NOT ({account}) AND NOT ({donation('app')})",
        "TBN App Donors": f"{plan} AND {donation('app')}",
        "Basic Donors": f"{basic} AND {account} AND {number('lifetime_amount')}>0",
        "Basic Recurring Donors": f"{basic} AND {yes('has_recurring_donation')}",
        "Basic One-Time Donors": f"{basic} AND {number('rfm_raw_frequency')}=1",
        "RFM TBN All Donor Reference": f"{exists('donor_account_status')} AND {exists('rfm_date')}",
        "RFM Lapsed Donors": f"{number('rfm_recency_score')}=0",
        "Partner Names Non-Donors": f"{account} AND NOT ({exists('rfm_date')})",
        "Donors Past 30 Days": donation("recent"),
        "Living Legacy Prospects V2": f"{number('donation_likelihood_prob')}>.75 AND {number('wealth_score')}>4 "
        f"AND {number('amt_suggested')}>500 AND {donation('start')}",
        "Giving Tuesday 2024": "p.id IN (SELECT profile_id FROM events WHERE name='donation' AND "
        "occurred_at >= TIMESTAMPTZ '2024-12-03 00:00:00+00' AND occurred_at < TIMESTAMPTZ '2024-12-04 00:00:00+00' AND "
        "properties->>'source_code' IN ('T4XWDGT','T4XPCGT','T4YWDGT','T4YPCGT'))",
        "Mobile Subscribed": f"{yes('mobile_mktg_optin_tbn')} AND phone IS NOT NULL",
        "Call Center SMS Opt-ins": f"{yes('tbn_call_center_sms_optin')} AND phone IS NOT NULL",
        "SMS Opt-Out Date Exists": exists("mobile_mktg_optout_date_tbn"),
        "VOD Optouts": f"{attr('tbn_video_on_demand_updates_opt_status')}='false'",
        "Customers with VOD Viewed": "p.id IN (SELECT profile_id FROM events WHERE name='vod_viewed')",
    }
    return predicates, params


def imported_report(db, as_of, requested_profiles, *, split_activity=False):
    from app.seed_audit import TARGETS, DENOMINATOR
    predicates, params = prepare_audience_oracle(db, as_of)
    exists = lambda key: f"NULLIF(attrs->>'{key}','') IS NOT NULL"
    yes = lambda key: f"attrs->>'{key}'='true'"
    account = exists("donor_account_number")
    count = lambda sql: db.scalar(text(sql), params)
    actual_n = count("SELECT count(*) FROM audit_people")
    results = []
    for name, predicate in predicates.items():
        sql = f"SELECT count(*) FROM audit_people p WHERE {predicate}"
        start = time.perf_counter()
        actual = count(sql)
        expected = TARGETS[name]*actual_n/DENOMINATOR
        if name == "Giving Tuesday 2024" and not params["start"] <= date(2024,12,3) < as_of:
            expected = 0
        tolerance = max(25, .2*expected)
        results.append(dict(metric=name, actual=actual, expected=expected,
                            tolerance=tolerance, passed=abs(actual-expected)<=tolerance,
                            seconds=time.perf_counter()-start, sql=sql))
    for name, predicate, fraction in (
        ("CDP identity", exists("zeta_cdp_id"), .93),
        ("UID only", f"{exists('zeta_uid')} AND NOT ({exists('zeta_cdp_id')})", .07),
        ("Known to Zeta", yes("known_to_zeta"), .49),
        ("No active phone", "phone IS NULL", .92),
        ("Donor accounts", account, .301),
    ):
        actual, expected = count(f"SELECT count(*) FROM audit_people p WHERE {predicate}"), actual_n*fraction
        results.append(dict(metric=name, actual=actual, expected=expected, tolerance=.2*expected,
                            passed=abs(actual-expected)<=.2*expected))
    for name, daily in (("donation",3000), ("donation_return",27), ("order_shipped",171)):
        actual = count(f"SELECT count(*) FROM events WHERE name='{name}' AND occurred_at>=:recent AND occurred_at<:as_of")
        expected = actual_n*daily*30/7830000
        results.append(dict(metric=f"30 day {name}", actual=actual, expected=expected,
                            tolerance=.2*expected, passed=abs(actual-expected)<=.2*expected))
    pending = count("SELECT count(*) FROM source_records WHERE resolved_at IS NULL")
    unresolved = count("SELECT count(*) FROM gifts WHERE profile_id IS NULL")
    unresolved += count("SELECT count(*) FROM events WHERE profile_id IS NULL")
    enrichment_profiles = count("SELECT count(DISTINCT profile_id) FROM enrichment_values")
    wrong_enrichment = count("""SELECT count(*) FROM enrichment_values e LEFT JOIN audit_people p ON p.id=e.profile_id
        WHERE p.id IS NULL OR p.attrs->>'known_to_zeta' IS DISTINCT FROM 'true'
        OR e.vendor IS DISTINCT FROM 'kinship-synthetic-demo'
        OR e.license_id IS DISTINCT FROM 'synthetic-demo-only'""")
    expected_enrichment = actual_n*.49
    results.append(dict(metric="Synthetic enrichment", actual=enrichment_profiles,
                        expected=expected_enrichment, tolerance=.2*expected_enrichment,
                        passed=abs(enrichment_profiles-expected_enrichment)<=.2*expected_enrichment))
    invalid_refund_references = 0
    event_feed_violations = 0
    if split_activity:
        invalid_refund_references = count("""
            SELECT count(*) FROM gifts r LEFT JOIN gifts original
              ON original.source_id=r.source_id
              AND original.external_id=r.attributes->>'original_gift_external_id'
            WHERE (r.amount<0 OR r.attributes->>'is_refund'='true') AND (
              original.id IS NULL OR original.amount<=0 OR r.amount>=0
              OR original.profile_id IS DISTINCT FROM r.profile_id
              OR r.gift_date<original.gift_date OR -r.amount>original.amount
            )
        """)
        invalid_refund_references += count("""
            SELECT count(*) FROM (
              SELECT r.source_id,r.attributes->>'original_gift_external_id'
              FROM gifts r JOIN gifts original ON original.source_id=r.source_id
                AND original.external_id=r.attributes->>'original_gift_external_id'
              WHERE r.amount<0
              GROUP BY r.source_id,r.attributes->>'original_gift_external_id'
              HAVING -sum(r.amount)>max(original.amount)
            ) over_refunded
        """)
        event_feed_violations = count("""
            SELECT count(*) FROM events e JOIN sources s ON s.id=e.source_id
            WHERE s.key IN ('donation_events','vod_viewed_events','people_feed')
              AND s.key<>CASE WHEN e.name IN ('donation','donation_return') THEN 'donation_events'
                         WHEN e.name='vod_viewed' THEN 'vod_viewed_events'
                         ELSE 'people_feed' END
        """)
    return {"as_of": str(as_of), "profiles": actual_n, "requested_profiles": requested_profiles,
            "unresolved_source_records": pending, "unresolved_facts": unresolved,
            "wrong_enrichment": wrong_enrichment, "metrics": results,
            "invalid_refund_references": invalid_refund_references,
            "event_feed_violations": event_feed_violations,
            "passed": all(r["passed"] for r in results) and not (pending or unresolved or wrong_enrichment)
            and not (invalid_refund_references or event_feed_violations)
            and abs(actual_n-requested_profiles) <= max(25,requested_profiles*.005)}


def assert_isolated(engine):
    """Budget overrides must never attach to an application engine."""
    if os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        raise RuntimeError("An isolated benchmark runner is required; refusing application database")
    schema = os.environ.get("KINSHIP_BENCHMARK_SCHEMA")
    if schema:
        from app.schema_benchmark import assert_private
        with engine.connect() as connection:
            assert_private(connection, schema)
    elif engine.url.host or not str(engine.url.query.get("host","")).startswith("/tmp/kinship-bench-"):
        raise RuntimeError("Refusing non-private benchmark socket")


@contextmanager
def bulk_phase_budget(engine):
    """Mirror worker bulk budget on EVERY transaction in one private phase.

    Direct handlers.run does not enter the worker lifecycle, so without this
    listener identity/traits/rollups inherit the API connection's 60s default.
    SET LOCAL resets at commit/rollback; removing the listener prevents it from
    affecting later oracle, dashboard or segment preview transactions.
    """
    assert_isolated(engine)
    from app.config import get_settings
    milliseconds = str(get_settings().bulk_statement_timeout_seconds * 1000)

    def begin(connection):
        connection.execute(text("SELECT set_config('statement_timeout', :budget, true)"),
                           {"budget": milliseconds})

    event.listen(engine, "begin", begin)
    try:
        yield
    finally:
        event.remove(engine, "begin", begin)


class PhaseTimings:
    """Checkpoint successful phases and retain the failing phase's elapsed time."""
    def __init__(self, directory):
        self.path = Path(directory) / "phase-timings.json"
        self.timings = {}
        self.write(status="running")

    def write(self, **state):
        document = {"completed_timings": dict(self.timings), **state}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2, default=str) + "\n")
        temporary.replace(self.path)

    def run(self, label, fn):
        started = time.perf_counter()
        self.write(status="running", phase=label)
        try:
            result = fn()
        except BaseException as exc:
            self.write(status="failed", failing_phase=label,
                       failing_phase_elapsed_seconds=time.perf_counter() - started,
                       error_type=type(exc).__name__, error=str(exc))
            raise
        self.timings[label] = time.perf_counter() - started
        self.write(status="running", last_completed_phase=label)
        return result


def measure(directory, profiles=50_000, *, scale="small", as_of=None, seed_dir=None):
    """Run only in the existing benchmark's private schema/socket. Never starts a worker."""
    from app.db import engine
    assert_isolated(engine)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    reference = date.fromisoformat(as_of) if isinstance(as_of,str) else as_of or datetime.now(timezone.utc).date()
    from app.seed import (
        generate_seed, load_generated_files, CSV_FILES, PREVIOUS_AUDIT_CSV_FILES,
        LEGACY_CSV_FILES,
    )
    from app.seed_audit_report import measure_audit
    from app.imports.service import run_import
    from app.jobs import handlers
    from app.jobs.queue import succeed
    from app.traits.engine import recompute_traits
    from app.dashboards.rollups import refresh_dashboard_rollups
    import app.load_status
    progress = PhaseTimings(directory)
    timed = progress.run
    def bulk(label, fn):
        def run():
            with bulk_phase_budget(engine):
                return fn()
        return timed(label, run)
    if seed_dir:
        def reuse():
            seed_path = Path(seed_dir)
            stats = json.loads((seed_path/"generator_stats.json").read_text())
            if stats.get("mode") != "audit" or stats["requested_people"] != profiles or stats["as_of"] != str(reference):
                raise ValueError("Reused seed must match audit mode, population and as_of")
            filenames = CSV_FILES if stats.get("source_file_mapping") is not None else LEGACY_CSV_FILES
            if (stats.get("source_file_mapping") is not None
                    and "people_feed_events.csv" not in stats["source_file_mapping"]):
                filenames = PREVIOUS_AUDIT_CSV_FILES
            if set(stats.get("files", {})) != set(filenames):
                raise ValueError("Reused seed must contain the complete matching audit file manifest")
            return {"files": {name: seed_path/name for name in filenames}, "output_dir": seed_path}
        generated = timed("reuse_seed", reuse)
    else:
        generated = timed("generate", lambda: generate_seed(profiles,scale,output_dir=directory/"seed",as_of=reference))
    csv_report = timed("csv_sql_oracle", lambda: measure_audit(generated["output_dir"]))
    def drain(*args, **kwargs):
        while True:
            with Session(engine) as db:
                job = db.execute(text("SELECT id,payload FROM jobs WHERE type='import.run' AND status='queued' ORDER BY id LIMIT 1")).mappings().first()
                if not job:
                    break
                db.execute(text("UPDATE jobs SET status='running' WHERE id=:id"), {"id":job["id"]})
                db.commit()
            run_import(job["payload"]["import_id"])
            with Session(engine) as db:
                succeed(db,job["id"])
                db.commit()
        return 0
    original = app.load_status.run_load_status
    app.load_status.run_load_status = drain
    try:
        timed("real_imports", lambda: load_generated_files(generated["files"]))
    finally:
        app.load_status.run_load_status = original
    bulk("identity", lambda: handlers.run("identity.resolve_batch", {}))
    def traits():
        with Session(engine) as db:
            db.execute(text("UPDATE jobs SET status='cancelled' WHERE status='queued'"))
            result = recompute_traits(db, as_of=reference)
            db.commit()
            return result
    bulk("traits", traits)
    def rollups():
        with Session(engine) as db:
            result = refresh_dashboard_rollups(db, as_of=reference)
            db.commit()
            return result
    bulk("rollups", rollups)
    with Session(engine) as db:
        report = timed("postgres_oracle", lambda: imported_report(
            db, reference, profiles, split_activity="people_feed_events.csv" in generated["files"]))
        from app.dashboards.overview import build_overview
        overview = timed("default_dashboard", lambda: build_overview(
            db,reference-timedelta(days=29),reference,
            reference-timedelta(days=59),reference-timedelta(days=30)))
        report["dashboard"] = overview
        report["dashboard_nonempty"] = bool(
            overview["overview"]["stats"]["profiles"]["value"] > 0
            and any(bucket["gifts"] > 0 for bucket in overview["charts"]["monthly_giving"])
            and overview["charts"]["top_campaigns"])
        # The actual overview payload is retained for inspection, while this
        # direct rollup assertion is stable across presentation schema changes.
        report["dashboard_recent_gifts"] = timed("dashboard_recent_gifts", lambda: db.scalar(text(
            "SELECT count(*) FROM gifts WHERE gift_date>=:start AND gift_date<:end AND amount>0"),
            {"start":reference-timedelta(days=30),"end":reference}))
        report["passed"] = (report["passed"] and csv_report["passed"]
                            and report["dashboard_recent_gifts"] > 0 and report["dashboard_nonempty"])
    report["timings"] = progress.timings
    (directory/"audit-seed.json").write_text(json.dumps(report,indent=2,default=str)+"\n")
    progress.write(status="passed" if report["passed"] else "failed",
                   acceptance_passed=report["passed"])
    return report
