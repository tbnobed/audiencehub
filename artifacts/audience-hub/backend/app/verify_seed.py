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
                  count(*) FILTER (WHERE amount>=1000 AND COALESCE(attributes->>'is_refund','false')<>'true') AS gifts_1000,
                  count(*) FILTER (WHERE amount>=10000 AND COALESCE(attributes->>'is_refund','false')<>'true') AS gifts_10000,
                  count(*) FILTER (WHERE COALESCE(attributes->>'is_refund','false')='true') AS returns,
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
            distribution = db.execute(text("""
                WITH positive AS (
                  SELECT g.* FROM gifts g WHERE g.profile_id IS NOT NULL AND g.amount>0
                    AND COALESCE(g.attributes->>'is_refund','false')<>'true'
                ), retained AS (
                  SELECT g.* FROM positive g WHERE NOT EXISTS (
                    SELECT 1 FROM gifts r WHERE r.source_id=g.source_id AND r.profile_id=g.profile_id
                      AND (r.amount<0 OR r.attributes->>'is_refund'='true')
                      AND (r.attributes->>'original_gift_external_id'=g.external_id
                           OR r.attributes->>'original_donation_id'=g.external_id)
                  )
                ), gross_counts AS (
                  SELECT profile_id,count(*) AS n FROM positive GROUP BY profile_id
                ), retained_counts AS (
                  SELECT profile_id,count(*) AS n FROM retained GROUP BY profile_id
                )
                SELECT
                  (SELECT percentile_cont(.5) WITHIN GROUP(ORDER BY n) FROM gross_counts) AS gross_median,
                  (SELECT percentile_cont(.95) WITHIN GROUP(ORDER BY n) FROM gross_counts) AS gross_p95,
                  count(*) AS donors,
                  percentile_cont(.5) WITHIN GROUP(ORDER BY n) AS median,
                  percentile_cont(.95) WITHIN GROUP(ORDER BY n) AS p95,
                  count(*) FILTER(WHERE n=1) AS single_givers,
                  count(*) FILTER(WHERE n>=20) AS frequent_givers
                FROM retained_counts
            """)).mappings().one()
            seasonal = db.execute(text("""
                SELECT extract(year FROM g.gift_date)::int AS year,
                  count(*) AS gifts,sum(g.amount) AS dollars,
                  sum(g.amount) FILTER(WHERE extract(month FROM g.gift_date) IN (11,12)) AS year_end,
                  min(g.gift_date) AS first_date,max(g.gift_date) AS last_date
                FROM gifts g WHERE g.amount>0 AND NOT g.is_recurring
                  AND COALESCE(g.attributes->>'is_refund','false')<>'true'
                  AND NOT EXISTS (
                    SELECT 1 FROM gifts r WHERE r.source_id=g.source_id AND r.profile_id=g.profile_id
                      AND (r.amount<0 OR r.attributes->>'is_refund'='true')
                      AND (r.attributes->>'original_gift_external_id'=g.external_id
                           OR r.attributes->>'original_donation_id'=g.external_id))
                  AND g.gift_date IS NOT NULL
                GROUP BY extract(year FROM g.gift_date) ORDER BY year
            """)).mappings().all()
            pct = lambda numerator, denominator: 100 * float(numerator or 0) / denominator if denominator else 0
            print(f"Gifts per donor (unreturned): median={distribution['median']}; p95={distribution['p95']}; donors={distribution['donors']:,}")
            print(f"Gifts per donor (all positive originals): median={distribution['gross_median']}; p95={distribution['gross_p95']}")
            print(f"One-time givers (one unreturned gift): {distribution['single_givers']:,} "
                  f"({pct(distribution['single_givers'], distribution['donors']):.2f}%); "
                  f"20+ unreturned gifts: {distribution['frequent_givers']:,}")
            print(f"Gifts at 1,000+: {stats['gifts_1000']:,} ({pct(stats['gifts_1000'],stats['gift_count']):.3f}%); "
                  f"at 10,000+: {stats['gifts_10000']:,} ({pct(stats['gifts_10000'],stats['gift_count']):.3f}%)")
            print(f"Returns: {stats['returns']:,} ({pct(stats['returns'],stats['gift_count']):.3f}% of donations)")
            print("Nov-Dec share of one-time dollars by year (unreturned gifts; boundary years may be partial):")
            for year in seasonal:
                print(f"  {year['year']}: {pct(year['year_end'],float(year['dollars'])):.2f}%; "
                      f"one-time gifts={year['gifts']:,}; dollars={year['dollars']}; "
                      f"observed={year['first_date']}..{year['last_date']}")
            print(f"Nov-Dec share of one-time dollars, all years: "
                  f"{pct(sum(float(y['year_end'] or 0) for y in seasonal), sum(float(y['dollars']) for y in seasonal)):.2f}%")
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
