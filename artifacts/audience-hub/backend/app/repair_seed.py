"""Opt-in, fail-closed repair of the existing audit seed, never a reseed.

No application write-engine import: the default connection is read-only from
startup, including dialect initialization. All apply changes and their before
images share one transaction. See DEPLOYMENT.md before production use.
"""
import json
import os
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.readonly_benchmark import readonly_session


# Explicit generator vocabulary, not "anything that isn't a donation".
_NAMES = (
    "text_signup", "text_web_signup", "text_reply_confirmation",
    "text_call_center_signup", "text_reply_stop", "email_signup",
    "user_web_signup", "preference_update", "order_shipped",
)
_CONTACTS = """
contacts AS MATERIALIZED (
    SELECT r.* FROM source_records r
    JOIN sources s ON s.id=r.source_id
    JOIN imports i ON i.id=r.last_import_id AND i.source_id=r.source_id
    JOIN active_profiles p ON p.id=r.profile_id
    WHERE s.key='donor_direct' AND r.external_id ~ '^crm-[0-9]{8,}$'
      AND i.filename='donor_direct.csv' AND i.record_type='contact'
      AND i.status='completed' AND i.file_sha256 ~ '^[a-f0-9]{64}$'
      AND i.file_path ~ '/seed-[a-f0-9]{16}-donor_direct[.]csv$'
      AND (r.attributes->>'zeta_cdp_id'='cdp-' || (substring(r.external_id FROM 5)::bigint)::text
        OR r.attributes->>'zeta_uid'='uid-' || (substring(r.external_id FROM 5)::bigint)::text)
      AND (COALESCE(r.email_norm::text,r.attributes->>'email_raw','') ~*
                 '@([a-z0-9.-]+[.])?example[.](org|com|net)$'
        OR (COALESCE(r.email_norm::text,r.attributes->>'email_raw','')=''
            AND r.attributes->>'email_sha256' ~ '^[a-f0-9]{64}$'))
)
"""
_PLAN = "WITH " + _CONTACTS + """,
move_events AS MATERIALIZED (
    SELECT e.* FROM events e WHERE e.source_id=:donation
      AND e.name NOT IN ('donation','donation_return')
      AND (e.context @> jsonb_build_object('synthetic',true)
           OR e.name=ANY(CAST(:names AS text[])))
),
move_records AS MATERIALIZED (
    SELECT DISTINCT r.* FROM source_records r JOIN move_events e
      ON r.source_id=e.source_id
     AND r.attributes->>'_event_message_id'=e.message_id
),
refunds AS MATERIALIZED (
    SELECT g.*, substring(g.external_id FROM
        '(donation_return-[0-9]+-recent-[0-9]+)$') AS message
    FROM gifts g WHERE g.source_id=:donation
      AND (g.attributes->>'is_refund'='true' OR g.amount<0
           OR g.external_id LIKE '%donation_return%')
),
refund_records AS MATERIALIZED (
    SELECT r.id, f.id AS refund_id FROM source_records r JOIN refunds f
      ON r.source_id=f.source_id
     AND (r.id=f.source_record_id OR r.attributes->>'_event_message_id'=f.message)
),
pairs AS MATERIALIZED (
    SELECT r.id AS refund_id, g.id AS original_id, g.external_id AS original_external_id,
           g.amount AS original_amount, r.message,
           r.profile_id, r.source_record_id,
           g.gift_date AS original_date, r.gift_date AS refund_date,
           g.currency AS original_currency, r.currency AS refund_currency
    FROM refunds r JOIN gifts g ON g.source_id=r.source_id
      AND g.profile_id=r.profile_id
      AND g.external_id=ANY(ARRAY[
        'gift:' || replace(r.message,'donation_return-','donation-'),
        'donation:_event:' || replace(r.message,'donation_return-','donation-'),
        'donation:' || replace(r.message,'donation_return-','donation-'),
        replace(r.message,'donation_return-','donation-')])
      AND COALESCE(g.attributes->>'is_refund','false')<>'true' AND g.amount>0
),
affected AS MATERIALIZED (
    SELECT profile_id FROM move_events
    UNION SELECT profile_id FROM refunds
    UNION SELECT g.profile_id FROM gifts g JOIN contacts c ON c.profile_id=g.profile_id
),
contact_counts AS MATERIALIZED (
    SELECT profile_id,count(*) AS n FROM contacts GROUP BY profile_id
),
move_evidence AS MATERIALIZED (
    SELECT e.id,e.occurred_at,count(r.id) AS records,
      count(c.id) FILTER (WHERE r.profile_id=e.profile_id
        AND r.external_id=ANY(ARRAY[e.message_id,'_event:' || e.message_id])
        AND r.attributes #>> '{_identity_reference,source_key}'='donor_direct'
        AND r.attributes #>> '{_identity_reference,contact_external_id}'=c.external_id
        AND substring(e.message_id FROM ('^' || e.name || '-([0-9]+)-'))::bigint
            =substring(c.external_id FROM 5)::bigint) AS proven
    FROM move_events e LEFT JOIN move_records r
      ON r.attributes->>'_event_message_id'=e.message_id
    LEFT JOIN contacts c ON c.profile_id=r.profile_id
    GROUP BY e.id,e.occurred_at
),
gift_record_counts AS MATERIALIZED (
    SELECT source_record_id,count(*) AS n FROM gifts GROUP BY source_record_id
),
gift_evidence AS MATERIALIZED (
    SELECT g.id,count(c.id) FILTER (WHERE r.profile_id=g.profile_id AND r.source_id=g.source_id
      AND r.attributes #>> '{_identity_reference,source_key}'='donor_direct'
      AND r.attributes #>> '{_identity_reference,contact_external_id}'=c.external_id
      AND substring(g.external_id FROM 'donation(?:_return)?-([0-9]+)-')::bigint
          =substring(c.external_id FROM 5)::bigint) AS proven
    FROM gifts g JOIN affected a ON a.profile_id=g.profile_id
    LEFT JOIN source_records r ON r.id=g.source_record_id
    LEFT JOIN contacts c ON c.profile_id=r.profile_id GROUP BY g.id
),
refund_evidence AS MATERIALIZED (
    SELECT f.id,
      count(rr.id) FILTER (WHERE r.attributes->>'_event_message_id'=f.message) AS event_records,
      count(rr.id) AS records,
      count(c.id) FILTER (WHERE c.profile_id=f.profile_id
        AND r.attributes #>> '{_identity_reference,source_key}'='donor_direct'
        AND r.attributes #>> '{_identity_reference,contact_external_id}'=c.external_id
        AND (r.id=f.source_record_id OR
             r.external_id=ANY(ARRAY[f.message,'_event:' || f.message]))) AS proven
    FROM refunds f LEFT JOIN refund_records rr ON rr.refund_id=f.id
    LEFT JOIN source_records r ON r.id=rr.id
    LEFT JOIN contacts c ON c.profile_id=r.profile_id GROUP BY f.id
),
ledger AS MATERIALIZED (
    SELECT g.*, COALESCE(-p.original_amount,g.amount) AS net_amount,
           COALESCE(g.attributes->>'is_refund','false')='true' AS is_refund
    FROM gifts g JOIN affected a ON a.profile_id=g.profile_id
    LEFT JOIN pairs p ON p.refund_id=g.id
    WHERE g.gift_date IS NULL OR g.gift_date<=CURRENT_DATE
),
unreturned AS MATERIALIZED (
    SELECT g.* FROM ledger g WHERE NOT g.is_refund
      AND NOT EXISTS (SELECT 1 FROM pairs p JOIN ledger r ON r.id=p.refund_id
                      WHERE p.original_id=g.id)
),
net_totals AS MATERIALIZED (
    SELECT profile_id,sum(net_amount) AS total FROM ledger GROUP BY profile_id
),
unreturned_totals AS MATERIALIZED (
    SELECT profile_id,count(*) AS n,round(avg(amount),2) AS average
    FROM unreturned GROUP BY profile_id
),
latest AS MATERIALIZED (
    SELECT DISTINCT ON (profile_id) profile_id,gift_date,amount,source_code
    FROM unreturned ORDER BY profile_id,gift_date DESC NULLS LAST,id DESC
),
contact_values AS MATERIALIZED (
    SELECT c.id, c.profile_id, jsonb_build_object(
      'lifetime_amount', COALESCE(net.total,0),
      'number_of_donations', COALESCE(u.n,0),
      'lifetime_avg_donation', COALESCE(u.average,0),
      'last_donation_date', last_gift.gift_date,
      'last_donation_amount', last_gift.amount,
      'last_donation_source', last_gift.source_code
    ) AS patch
    FROM contacts c JOIN affected a ON a.profile_id=c.profile_id
    LEFT JOIN net_totals net ON net.profile_id=c.profile_id
    LEFT JOIN unreturned_totals u ON u.profile_id=c.profile_id
    LEFT JOIN latest last_gift ON last_gift.profile_id=c.profile_id
),
contact_changes AS MATERIALIZED (
    SELECT v.* FROM contact_values v JOIN source_records r ON r.id=v.id
    WHERE r.attributes IS DISTINCT FROM r.attributes || v.patch
),
refund_changes AS MATERIALIZED (
    SELECT p.*, jsonb_build_object('is_refund',true,
        'original_donation_id',p.original_external_id,
        'original_gift_external_id',p.original_external_id) AS patch
    FROM pairs p JOIN gifts r ON r.id=p.refund_id
    WHERE r.amount IS DISTINCT FROM -p.original_amount
       OR r.attributes->>'original_donation_id' IS DISTINCT FROM p.original_external_id
       OR r.attributes->>'original_gift_external_id' IS DISTINCT FROM p.original_external_id
       OR EXISTS (SELECT 1 FROM source_records sr WHERE sr.id=r.source_record_id
          AND (sr.attributes->>'original_donation_id' IS DISTINCT FROM p.original_external_id
            OR sr.attributes->>'original_gift_external_id' IS DISTINCT FROM p.original_external_id))
       OR EXISTS (SELECT 1 FROM events e WHERE e.source_id=r.source_id AND e.message_id=p.message
          AND (e.properties->'amount' IS DISTINCT FROM to_jsonb(p.original_amount)
            OR e.properties->>'original_gift_id' IS DISTINCT FROM p.original_external_id
            OR e.properties->>'original_donation_id' IS DISTINCT FROM p.original_external_id))
       OR EXISTS (SELECT 1 FROM refund_records rr JOIN source_records sr ON sr.id=rr.id
          WHERE rr.refund_id=r.id AND sr.attributes ? '_event_properties'
          AND (sr.attributes #> '{_event_properties,amount}' IS DISTINCT FROM to_jsonb(p.original_amount)
            OR sr.attributes #>> '{_event_properties,original_gift_id}' IS DISTINCT FROM p.original_external_id
            OR sr.attributes #>> '{_event_properties,original_donation_id}' IS DISTINCT FROM p.original_external_id))
)
"""


def _scalar(db, query, params):
    return db.scalar(text(_PLAN + query), params)


def _preflight(db, params):
    """No DDL, locks, temp tables, or audit writes in this shared dry-run path."""
    checks = {
        "busy jobs/imports": """
          SELECT (SELECT count(*) FROM jobs WHERE status IN ('queued','running'))
               + (SELECT count(*) FROM imports WHERE status NOT IN ('completed','failed','cancelled'))
               + (SELECT count(*) FROM source_records WHERE resolved_at IS NULL)
        """,
        "missing or ambiguous contact provenance": """
          SELECT count(*) FROM affected a LEFT JOIN contact_counts c ON c.profile_id=a.profile_id
          WHERE a.profile_id IS NULL OR COALESCE(c.n,0)<>1
        """,
        "mixed or non-seed contact/profile evidence": """
          SELECT count(*) FROM source_records r JOIN affected a ON a.profile_id=r.profile_id
          LEFT JOIN imports i ON i.id=r.last_import_id
          WHERE i.id IS NULL OR (i.file_path ~ '/seed-[a-f0-9]{16}-') IS DISTINCT FROM true
             OR (i.file_sha256 ~ '^[a-f0-9]{64}$') IS DISTINCT FROM true
             OR i.filename NOT IN ('donor_direct.csv','people_feed.csv','hubspot.csv',
                 'okta_tbn_plus.csv','donation_events.csv','donation_events_gifts.csv',
                 'people_feed_events.csv','vod_viewed_events.csv','model_scores.csv')
        """,
        "unguarded/missing/multiply attached nonfinancial event records": """
          SELECT count(*) FROM move_events e JOIN move_evidence v
            ON v.id=e.id AND v.occurred_at=e.occurred_at WHERE
            NOT e.context @> jsonb_build_object('synthetic',true)
            OR NOT e.name=ANY(CAST(:names AS text[]))
            OR e.message_id !~ ('^' || e.name || '-[0-9]+-[0-9]*$')
            OR e.message_id IS NULL
            OR (SELECT count(*) FROM events other WHERE other.source_id=e.source_id
                AND other.message_id=e.message_id)<>1
            OR v.records<>1 OR v.proven<>1
        """,
        "orphaned deterministic nonfinancial source records": """
          SELECT count(*) FROM source_records r WHERE r.source_id=:donation
            AND split_part(r.attributes->>'_event_message_id','-',1)=ANY(CAST(:names AS text[]))
            AND NOT EXISTS (SELECT 1 FROM move_events e
                            WHERE e.message_id=r.attributes->>'_event_message_id')
        """,
        "destination event/source-record collisions or financially shared records": """
          SELECT (SELECT count(*) FROM move_events e JOIN events other
                  ON other.source_id=:people AND other.message_id=e.message_id
                     AND other.occurred_at=e.occurred_at)
               + (SELECT count(*) FROM move_records r JOIN source_records other
                  ON other.source_id=:people AND other.external_id=r.external_id)
               + (SELECT count(*) FROM move_records r JOIN gifts g ON g.source_record_id=r.id)
               + (SELECT count(*) FROM move_records r JOIN events e
                  ON e.source_id=r.source_id AND e.message_id=r.attributes->>'_event_message_id'
                  WHERE NOT EXISTS (SELECT 1 FROM move_events m
                                    WHERE m.id=e.id AND m.occurred_at=e.occurred_at))
        """,
        "unrecognized, real, or ambiguous gift provenance": """
          SELECT count(*) FROM gifts g JOIN affected a ON a.profile_id=g.profile_id
          JOIN gift_evidence v ON v.id=g.id
          LEFT JOIN gift_record_counts rc ON rc.source_record_id=g.source_record_id
          WHERE g.source_id<>:donation OR g.source_record_id IS NULL
            OR g.external_id !~ '^(gift:|donation:(_event:)?|donation_return:(_event:)?)?donation(_return)?-[0-9]+-(history|gt|recent-[0-9]+)$'
            OR v.proven<>1 OR COALESCE(rc.n,0)<>1
            OR (SELECT count(*) FROM events e WHERE e.source_id=g.source_id
                AND e.message_id=substring(g.external_id FROM '(donation(?:_return)?-[0-9]+-(?:history|gt|recent-[0-9]+))$'))<>1
            OR (SELECT count(*) FROM events e WHERE e.source_id=g.source_id
                AND e.profile_id=g.profile_id AND e.context @> jsonb_build_object('synthetic',true)
                AND e.name=CASE WHEN g.attributes->>'is_refund'='true'
                               THEN 'donation_return' ELSE 'donation' END
                AND e.message_id=substring(g.external_id FROM '(donation(?:_return)?-[0-9]+-(?:history|gt|recent-[0-9]+))$'))<>1
            OR (g.amount<=0 AND COALESCE(g.attributes->>'is_refund','false')<>'true')
        """,
        "unguarded refund event source-record mirrors": """
          SELECT count(*) FROM refund_evidence
          WHERE event_records<>1 OR records<>proven
        """,
        "missing/ambiguous originals or unrecognized refunds": """
          SELECT count(*) FROM refunds r WHERE r.message IS NULL
            OR r.external_id<>ALL(ARRAY['gift:' || r.message,
                'donation_return:_event:' || r.message,'donation_return:' || r.message,r.message])
            OR r.attributes->>'is_refund' IS DISTINCT FROM 'true'
            OR (SELECT count(*) FROM pairs p WHERE p.refund_id=r.id)<>1
        """,
        "duplicate/other returns, incompatible currencies or chronology": """
          SELECT count(*) FROM pairs p WHERE
            (SELECT count(*) FROM pairs other WHERE other.original_id=p.original_id)<>1
            OR p.original_currency<>p.refund_currency
            OR (p.original_date IS NOT NULL AND p.refund_date IS NOT NULL
                AND p.original_date>p.refund_date)
            OR EXISTS (SELECT 1 FROM gifts other WHERE other.id<>p.refund_id
              AND other.source_id=:donation
              AND (other.attributes->>'original_donation_id'=p.original_external_id
                   OR other.attributes->>'original_gift_external_id'=p.original_external_id))
        """,
    }
    failed = []
    for label, query in checks.items():
        count = _scalar(db, query, params)
        print(f"Precondition {label}: {count:,} blocking rows")
        if count:
            failed.append(label)
    if failed:
        raise RuntimeError("Repair refused; no changes made. Failed: " + "; ".join(failed))
    counts = {}
    for key, table in (("events_to_move", "move_events"), ("source_records_to_move", "move_records"),
                       ("refunds_to_repair", "refund_changes"),
                       ("canonical_contacts_to_update", "contact_changes"),
                       ("synthetic_profiles_to_refresh", "affected")):
        counts[key] = _scalar(db, f"SELECT count(*) FROM {table}", params)
    counts["refunds_with_unknown_dates"] = _scalar(db, """
        SELECT count(*) FROM pairs WHERE original_date IS NULL OR refund_date IS NULL
    """, params)
    counts["future_ledger_rows_excluded"] = _scalar(db, """
        SELECT count(*) FROM gifts g JOIN affected a ON a.profile_id=g.profile_id
        WHERE g.gift_date>CURRENT_DATE
    """, params)
    counts["undated_ledger_rows_included"] = _scalar(db, """
        SELECT count(*) FROM ledger WHERE gift_date IS NULL
    """, params)
    print(json.dumps(counts, sort_keys=True))
    print("Amounts are currency units. Positive gifts are unchanged; no rescaling.")
    return counts


def _journal(db, run_id, table, selection, params):
    # table and selection are code-owned constants, never CLI input.
    db.execute(text(_PLAN + f"""
        INSERT INTO audit_log(actor_type,action,entity_type,entity_id,details)
        SELECT 'system','seed.repair.before',:table_name,t.id::text,
          jsonb_build_object('run_id',:run_id,'version',1,'before',to_jsonb(t))
        FROM {table} t WHERE {selection}
    """), {**params, "run_id": run_id, "table_name": table})


def _apply(db, params, counts):
    run_id = str(uuid4())
    # Materialize a validated plan before mutation; never recalculate a partial
    # plan after moving evidence out of donation_events.
    for name, query in (
        ("seed_repair_moves", "SELECT id,occurred_at FROM move_events"),
        ("seed_repair_records", "SELECT id FROM move_records"),
        ("seed_repair_refunds", "SELECT * FROM refund_changes"),
        ("seed_repair_refund_records", """
            SELECT rr.* FROM refund_records rr JOIN refund_changes rc ON rc.refund_id=rr.refund_id
        """),
        ("seed_repair_contacts", "SELECT * FROM contact_changes"),
        ("seed_repair_profiles", "SELECT profile_id FROM affected"),
    ):
        db.execute(text(f"CREATE TEMP TABLE {name} ON COMMIT DROP AS " + _PLAN + query), params)
    _journal(db, run_id, "events",
             "(t.id,t.occurred_at) IN (SELECT id,occurred_at FROM seed_repair_moves)", params)
    _journal(db, run_id, "gifts", "t.id IN (SELECT refund_id FROM seed_repair_refunds)", params)
    _journal(db, run_id, "source_records", """
        t.id IN (SELECT id FROM seed_repair_records UNION SELECT id FROM seed_repair_contacts
                 UNION SELECT id FROM seed_repair_refund_records)
    """, params)
    # Refund event/source-record mirrors must not retain contradictory amounts.
    _journal(db, run_id, "events", """
        t.source_id=:donation AND t.message_id IN (SELECT message FROM seed_repair_refunds)
    """, params)
    db.execute(text("""
        UPDATE events e SET source_id=:people FROM seed_repair_moves m
        WHERE e.id=m.id AND e.occurred_at=m.occurred_at
    """), params)
    db.execute(text("""
        UPDATE source_records r SET source_id=:people FROM seed_repair_records m WHERE r.id=m.id
    """), params)
    db.execute(text("""
        UPDATE gifts g SET amount=-p.original_amount,attributes=g.attributes || p.patch
        FROM seed_repair_refunds p WHERE g.id=p.refund_id
    """))
    db.execute(text("""
        UPDATE events e SET properties=e.properties || jsonb_build_object(
          'amount',p.original_amount,'original_gift_id',p.original_external_id,
          'original_donation_id',p.original_external_id)
        FROM seed_repair_refunds p WHERE e.source_id=:donation AND e.message_id=p.message
    """), params)
    db.execute(text("""
        UPDATE source_records r SET attributes=r.attributes ||
          CASE WHEN r.id=p.source_record_id THEN p.patch ELSE '{}'::jsonb END ||
          CASE WHEN r.attributes ? '_event_properties' THEN jsonb_build_object(
            '_event_properties',r.attributes->'_event_properties' || jsonb_build_object(
              'amount',p.original_amount,'original_gift_id',p.original_external_id,
              'original_donation_id',p.original_external_id)) ELSE '{}'::jsonb END
        FROM seed_repair_refunds p JOIN seed_repair_refund_records rr ON rr.refund_id=p.refund_id
        WHERE r.id=rr.id
    """))
    db.execute(text("""
        UPDATE source_records r SET attributes=r.attributes || c.patch
        FROM seed_repair_contacts c WHERE r.id=c.id
    """))
    # Keep existing external identifiers/resolver keys, timestamps, raw import
    # hashes, profile attachments, and consent byte-for-byte unchanged.
    if counts["events_to_move"]:
        _journal(db, run_id, "sources", "t.id=:people", params)
        db.execute(text("""
            UPDATE sources SET record_types=array_append(COALESCE(record_types,ARRAY[]::varchar[]),'event')
            WHERE id=:people AND NOT COALESCE('event'=ANY(record_types),false)
        """), params)
    from app.traits.engine import recompute_traits
    profile_ids = list(db.scalars(text("SELECT profile_id FROM seed_repair_profiles ORDER BY profile_id")))
    # Existing pipeline also refreshes dashboard rollups/cache in this same
    # transaction. No identity resolution, historical snapshots, or dirty-queue deletion.
    if profile_ids:
        recompute_traits(db, profile_ids=profile_ids, write_snapshot=False,
                         as_of=db.scalar(text("SELECT CURRENT_DATE")))
    invalid = db.scalar(text("""
        SELECT
          (SELECT count(*) FROM seed_repair_moves m LEFT JOIN events e
             ON e.id=m.id AND e.occurred_at=m.occurred_at
           WHERE e.id IS NULL OR e.source_id<>:people)
        + (SELECT count(*) FROM seed_repair_records m LEFT JOIN source_records r ON r.id=m.id
           WHERE r.id IS NULL OR r.source_id<>:people)
        + (SELECT count(*) FROM seed_repair_refunds p LEFT JOIN gifts g ON g.id=p.refund_id
           WHERE g.id IS NULL OR g.amount IS DISTINCT FROM -p.original_amount
             OR g.attributes->>'original_donation_id' IS DISTINCT FROM p.original_external_id
             OR g.attributes->>'original_gift_external_id' IS DISTINCT FROM p.original_external_id)
        + (SELECT count(*) FROM seed_repair_contacts c JOIN source_records r ON r.id=c.id
           WHERE NOT r.attributes @> c.patch)
        + (SELECT count(*) FROM seed_repair_profiles p
           LEFT JOIN profile_traits t ON t.profile_id=p.profile_id
           WHERE t.profile_id IS NULL OR t.ltv_total IS DISTINCT FROM COALESCE(
             (SELECT sum(g.amount) FROM gifts g WHERE g.profile_id=p.profile_id
                AND (g.gift_date IS NULL OR g.gift_date<=CURRENT_DATE)),0))
    """), params)
    if invalid:
        raise RuntimeError("Repair postconditions failed; entire transaction will be rolled back.")
    db.execute(text("""
        INSERT INTO audit_log(actor_type,action,entity_type,entity_id,details)
        VALUES ('system','seed.repair.complete','seed_repair',:run_id,CAST(:details AS jsonb))
    """), {"run_id": run_id, "details": json.dumps({"run_id": run_id, "version": 1, **counts})})
    return run_id


def repair_seed(*, apply=False, yes=False, timeout_seconds=7200):
    if apply != yes:
        raise ValueError("Writes require BOTH --apply and --yes; omit both for a read-only dry run.")
    if not 1 <= timeout_seconds <= 14400:
        raise ValueError("--timeout-seconds must be between 1 and 14400")
    settings = get_settings()
    if apply and (settings.app_env != "production" or any(os.environ.get(key) for key in
            ("REPL_ID", "REPLIT_DEPLOYMENT", "REPLIT_DEV_DOMAIN", "REPLIT_DOMAINS"))):
        raise ValueError("Apply is permitted only on your production server (APP_ENV=production), not Replit.")
    url = make_url(settings.database_url.replace(
        "postgres://", "postgresql+psycopg://", 1).replace(
        "postgresql://", "postgresql+psycopg://", 1))
    if url.get_backend_name() != "postgresql":
        raise ValueError("repair-seed requires PostgreSQL")
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True, connect_args={
        "application_name": "kinship-repair-seed-apply" if apply else "kinship-repair-seed-readonly",
        "connect_timeout": 10,
        "options": url.query.get("options", "") +
                   f" -cdefault_transaction_read_only={'off' if apply else 'on'}"
                   f" -cstatement_timeout={timeout_seconds * 1000} -clock_timeout=5000",
    })
    try:
        if not apply:
            with readonly_session(engine) as db:
                _run(db, apply=False)
        else:
            with Session(engine) as db, db.begin():
                # Unlike an advisory lock alone, these locks also exclude writers
                # that know nothing about this command (API, imports, worker claims).
                if not db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext('audience-hub:repair-seed:v1'))")):
                    raise RuntimeError("Repair already running; no changes made.")
                db.execute(text("""
                    LOCK TABLE jobs,imports,identity_batches,sources,profiles,identifiers,
                      consents,source_records,gifts,events,trait_dirty_profiles
                    IN SHARE ROW EXCLUSIVE MODE NOWAIT
                """))
                _run(db, apply=True)
            print("COMMITTED: data, before-image journal, traits, source chips and dashboard rollups.")
    finally:
        engine.dispose()
    return 0


def _run(db, *, apply):
    db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    source_ids = dict(db.execute(text(
        "SELECT key,id FROM sources WHERE key IN ('donation_events','people_feed','donor_direct')"
    )).all())
    if len(source_ids) != 3:
        raise RuntimeError("Required existing audit sources missing; no sources will be created.")
    params = {"donation": source_ids["donation_events"], "people": source_ids["people_feed"],
              "names": list(_NAMES)}
    print("APPLY: locked maintenance transaction." if apply else
          "DRY RUN: PostgreSQL-enforced read-only repeatable-read snapshot; no writes.")
    counts = _preflight(db, params)
    if not any(counts[k] for k in ("events_to_move", "refunds_to_repair", "canonical_contacts_to_update")):
        print("Already reconciled: no changes, audit entries, or refreshes needed.")
    elif apply:
        run_id = _apply(db, params, counts)
        print(f"Repair journal run_id={run_id}; commit pending.")
    else:
        print("Preconditions passed. Review backup and counts before --apply --yes on your server.")
