"""Read-only production seed diagnostics; never initializes the app write engine."""
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.readonly_benchmark import readonly_session


def verify_seed(timeout_seconds=600):
    if not 1 <= timeout_seconds <= 14400:
        raise ValueError("--timeout-seconds must be between 1 and 14400")
    url = make_url(get_settings().database_url.replace(
        "postgres://", "postgresql+psycopg://", 1).replace(
        "postgresql://", "postgresql+psycopg://", 1))
    if url.get_backend_name() != "postgresql":
        raise ValueError("verify-seed requires PostgreSQL")
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True, connect_args={
        "application_name": "kinship-verify-seed-readonly", "connect_timeout": 10,
        "options": (url.query.get("options", "") +
                    f" -cdefault_transaction_read_only=on -cstatement_timeout={timeout_seconds*1000}"
                    " -clock_timeout=5000"),
    })
    try:
        with readonly_session(engine) as db:
            db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            stats = db.execute(text("""
                SELECT count(*) AS ledger_rows,
                  count(*) FILTER (WHERE COALESCE(attributes->>'is_refund','false')<>'true') AS gift_count,
                  min(amount) FILTER (WHERE COALESCE(attributes->>'is_refund','false')<>'true') AS minimum,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY amount)
                    FILTER (WHERE COALESCE(attributes->>'is_refund','false')<>'true') AS median,
                  max(amount) FILTER (WHERE COALESCE(attributes->>'is_refund','false')<>'true') AS maximum,
                  count(*) FILTER (WHERE amount<0) AS negative_amounts,
                  count(*) FILTER (WHERE amount<0 AND COALESCE(attributes->>'is_refund','false')<>'true')
                    AS unexplained_negatives,
                  count(*) FILTER (WHERE profile_id IS NULL) AS unattached_rows
                FROM gifts
            """)).mappings().one()
            mismatch = db.scalar(text("""
                WITH totals AS (
                  SELECT profile_id, sum(amount) AS net FROM gifts
                  WHERE gift_date IS NULL OR gift_date<=CURRENT_DATE GROUP BY profile_id
                )
                SELECT count(*) FROM active_profiles p
                LEFT JOIN totals g ON g.profile_id=p.id
                LEFT JOIN profile_traits t ON t.profile_id=p.id
                WHERE t.profile_id IS NULL OR t.ltv_total IS DISTINCT FROM COALESCE(g.net,0)
            """))
            empty_links = db.scalar(text("""
                SELECT count(*) FROM active_profiles p
                WHERE EXISTS (SELECT 1 FROM source_records r JOIN sources s ON s.id=r.source_id
                              WHERE r.profile_id=p.id AND s.key='donation_events')
                  AND NOT EXISTS (SELECT 1 FROM gifts g WHERE g.profile_id=p.id
                                  AND COALESCE(g.attributes->>'is_refund','false')<>'true')
            """))
            invalid_returns = db.scalar(text("""
                WITH refunds AS (
                  SELECT r.*, coalesce(r.attributes->>'original_gift_external_id',
                                       r.attributes->>'original_donation_id') AS original_id
                  FROM gifts r WHERE r.attributes->>'is_refund'='true'
                ), grouped AS (
                  SELECT source_id,profile_id,original_id,sum(abs(amount)) AS returned,
                         min(gift_date) AS earliest, bool_or(amount>=0) AS invalid_sign
                  FROM refunds GROUP BY source_id,profile_id,original_id
                )
                SELECT count(*) FROM grouped r
                WHERE r.original_id IS NULL OR r.invalid_sign OR NOT EXISTS (
                  SELECT 1 FROM gifts g WHERE g.source_id=r.source_id
                    AND g.external_id=r.original_id AND g.profile_id=r.profile_id
                    AND COALESCE(g.attributes->>'is_refund','false')<>'true'
                    AND g.amount>=r.returned AND g.gift_date<=r.earliest
                )
            """))
            print("Read-only repeatable-read snapshot; amounts are currency units, not cents.")
            print("Lifetime reconciliation includes undated gifts and gifts through today UTC.")
            print(f"Gift count: {stats['gift_count']:,}; ledger rows (including returns): {stats['ledger_rows']:,}")
            print(f"Gift amount min/median/max (excluding return rows): {stats['minimum']} / {stats['median']} / {stats['maximum']}")
            print(f"Negative amounts: {stats['negative_amounts']:,}; without refund evidence: {stats['unexplained_negatives']:,}")
            print(f"Invalid/unlinked/over-refunded original gifts: {invalid_returns:,}")
            print(f"Profiles with lifetime giving mismatch or missing traits: {mismatch:,}")
            print(f"Profiles with donation_events source but zero gifts: {empty_links:,}")
            print(f"Unattached gift/return rows: {stats['unattached_rows']:,}")
            if mismatch:
                examples = db.execute(text("""
                    WITH totals AS (
                      SELECT profile_id,sum(amount) AS net FROM gifts
                      WHERE gift_date IS NULL OR gift_date<=CURRENT_DATE GROUP BY profile_id
                    )
                    SELECT p.master_id,p.id,t.ltv_total,COALESCE(g.net,0) AS gift_sum
                    FROM active_profiles p LEFT JOIN totals g ON g.profile_id=p.id
                    LEFT JOIN profile_traits t ON t.profile_id=p.id
                    WHERE t.profile_id IS NULL OR t.ltv_total IS DISTINCT FROM COALESCE(g.net,0)
                    ORDER BY p.id LIMIT 20
                """)).mappings()
                print("Lifetime mismatches (first 20):")
                for row in examples:
                    print(f"  {row['master_id']} profile={row['id']} lifetime={row['ltv_total']} gift_sum={row['gift_sum']}")
            if empty_links:
                examples = db.execute(text("""
                    SELECT p.id,p.master_id FROM active_profiles p
                    WHERE EXISTS (SELECT 1 FROM source_records r JOIN sources s ON s.id=r.source_id
                                  WHERE r.profile_id=p.id AND s.key='donation_events')
                      AND NOT EXISTS (SELECT 1 FROM gifts g WHERE g.profile_id=p.id
                                      AND COALESCE(g.attributes->>'is_refund','false')<>'true')
                    ORDER BY p.id LIMIT 20
                """)).mappings()
                print("Donation-source profiles without gifts (first 20):")
                for row in examples:
                    print(f"  {row['master_id']} profile={row['id']}")
            failed = bool(mismatch or empty_links or invalid_returns or
                          stats["unexplained_negatives"] or stats["unattached_rows"])
            print("FAIL: discrepancies found; no changes made." if failed else "PASS: ledger reconciliation checks passed.")
            return int(failed)
    finally:
        engine.dispose()
