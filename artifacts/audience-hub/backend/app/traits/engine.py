"""Set-based computation and persistence of profile traits."""

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

_RECOMPUTE_SQL = text("""
WITH target_profiles AS MATERIALIZED (
    SELECT p.id AS profile_id
    FROM profiles p
    WHERE p.merged_into_id IS NULL AND p.is_deleted=false
      AND (CAST(:profile_ids AS bigint[]) IS NULL OR p.id=ANY(CAST(:profile_ids AS bigint[])))
),
all_active_profiles AS MATERIALIZED (
    SELECT p.id AS profile_id FROM profiles p
    WHERE p.merged_into_id IS NULL AND p.is_deleted=false
),
gift_base AS MATERIALIZED (
    SELECT g.profile_id, g.id, g.amount, g.gift_date, g.is_recurring
    FROM gifts g
    JOIN target_profiles p ON p.profile_id=g.profile_id
    WHERE g.gift_date IS NULL OR g.gift_date <= CAST(:as_of AS date)
),
gift_rollup AS (
    SELECT p.profile_id,
           count(g.id)::integer AS gift_count_total,
           COALESCE(sum(g.amount), 0)::numeric(14,2) AS ltv_total,
           COALESCE(sum(g.amount) FILTER (
               WHERE g.gift_date >= CAST(:as_of AS date) - 364
                 AND g.gift_date <= CAST(:as_of AS date)
           ), 0)::numeric(14,2) AS gift_amount_12m,
           count(g.id) FILTER (
               WHERE g.gift_date >= CAST(:as_of AS date) - 364
                 AND g.gift_date <= CAST(:as_of AS date)
           )::integer AS gift_count_12m,
           min(g.gift_date) AS first_gift_date,
           max(g.gift_date) AS last_gift_date,
           max(g.amount)::numeric(14,2) AS largest_gift_amount,
           avg(g.amount)::numeric(14,2) AS avg_gift_amount,
           COALESCE(bool_or(g.is_recurring AND g.gift_date >= CAST(:as_of AS date) - 44
                            AND g.gift_date <= CAST(:as_of AS date)), false) AS is_recurring_active
    FROM target_profiles p
    LEFT JOIN gift_base g ON g.profile_id=p.profile_id
    GROUP BY p.profile_id
),
dated_gifts AS (
    SELECT g.profile_id, g.gift_date,
           lag(g.gift_date) OVER (PARTITION BY g.profile_id ORDER BY g.gift_date, g.id) AS previous_gift_date,
           row_number() OVER (PARTITION BY g.profile_id ORDER BY g.gift_date DESC, g.id DESC) AS newest_rank
    FROM gift_base g
    WHERE g.gift_date IS NOT NULL
),
latest_gap AS (
    SELECT profile_id, previous_gift_date
    FROM dated_gifts
    WHERE newest_rank=1
),
event_rollup AS (
    SELECT p.profile_id,
           count(e.id) FILTER (
               WHERE e.occurred_at >= CAST(:as_of AS date) - 29
                 AND e.occurred_at < CAST(:as_of AS date) + 1
           )::integer AS event_count_30d,
           max(e.occurred_at) AS last_event_at,
           count(e.id) FILTER (
               WHERE e.name='Video Watched'
                 AND e.occurred_at >= CAST(:as_of AS date) - 29
                 AND e.occurred_at < CAST(:as_of AS date) + 1
           )::integer AS video_views_30d
    FROM target_profiles p
    LEFT JOIN events e ON e.profile_id=p.profile_id
                      AND e.occurred_at < CAST(:as_of AS date) + 1
    GROUP BY p.profile_id
),
source_rollup AS (
    SELECT p.profile_id,
           COALESCE(array_agg(DISTINCT s.key ORDER BY s.key)
                    FILTER (WHERE s.key IS NOT NULL), ARRAY[]::text[]) AS source_keys
    FROM target_profiles p
    LEFT JOIN (
        SELECT profile_id, source_id FROM source_records WHERE profile_id IS NOT NULL
        UNION ALL
        SELECT profile_id, source_id FROM gifts WHERE profile_id IS NOT NULL
        UNION ALL
        SELECT profile_id, source_id FROM events WHERE profile_id IS NOT NULL
    ) appearances ON appearances.profile_id=p.profile_id
    LEFT JOIN sources s ON s.id=appearances.source_id
    GROUP BY p.profile_id
),
engagement_candidates AS (
    SELECT g.profile_id, g.gift_date::timestamp AT TIME ZONE 'UTC' AS engaged_at,
           s.key AS source_key, g.id AS tie_id
    FROM gift_base g JOIN gifts source_gift ON source_gift.id=g.id
    JOIN sources s ON s.id=source_gift.source_id
    WHERE g.gift_date IS NOT NULL
    UNION ALL
    SELECT e.profile_id, e.occurred_at AS engaged_at, s.key AS source_key, e.id AS tie_id
    FROM events e JOIN target_profiles p ON p.profile_id=e.profile_id
    JOIN sources s ON s.id=e.source_id
    WHERE e.occurred_at < CAST(:as_of AS date) + 1
),
last_engagement AS (
    SELECT DISTINCT ON (profile_id) profile_id, source_key
    FROM engagement_candidates
    ORDER BY profile_id, engaged_at DESC, tie_id DESC
),
raw AS (
    SELECT p.profile_id, gr.gift_count_total, gr.ltv_total, gr.gift_amount_12m,
           gr.gift_count_12m, gr.first_gift_date, gr.last_gift_date,
           gr.largest_gift_amount, gr.avg_gift_amount, gr.is_recurring_active,
           (CAST(:as_of AS date) - gr.last_gift_date)::integer AS days_since_last_gift,
           lg.previous_gift_date
    FROM target_profiles p
    JOIN gift_rollup gr ON gr.profile_id=p.profile_id
    LEFT JOIN latest_gap lg ON lg.profile_id=p.profile_id
),
status_rows AS (
    SELECT raw.*,
           CASE
             WHEN last_gift_date IS NULL THEN 'prospect'
             WHEN days_since_last_gift <= 365 AND first_gift_date >= CAST(:as_of AS date) - 365 THEN 'new'
             WHEN days_since_last_gift <= 365
                  AND previous_gift_date IS NOT NULL
                  AND last_gift_date - previous_gift_date > 730 THEN 'reactivated'
             WHEN days_since_last_gift <= 365 THEN 'active'
             WHEN days_since_last_gift <= 730 THEN 'lapsing'
             ELSE 'lapsed'
           END AS donor_status
    FROM raw
),
ranked_donors AS (
    SELECT profile_id,
           (6 - ntile(5) OVER (ORDER BY days_since_last_gift ASC, profile_id))::smallint AS rfm_recency,
           (6 - ntile(5) OVER (ORDER BY gift_count_total DESC, profile_id))::smallint AS rfm_frequency,
           (6 - ntile(5) OVER (ORDER BY ltv_total DESC, profile_id))::smallint AS rfm_monetary
    FROM (
        SELECT p.profile_id, count(g.id)::integer AS gift_count_total,
               COALESCE(sum(g.amount), 0)::numeric(14,2) AS ltv_total,
               (CAST(:as_of AS date) - max(g.gift_date))::integer AS days_since_last_gift
        FROM all_active_profiles p
        JOIN gifts g ON g.profile_id=p.profile_id
        WHERE g.gift_date IS NULL OR g.gift_date <= CAST(:as_of AS date)
        GROUP BY p.profile_id
        HAVING count(g.id) > 0
    ) donor_population
),
trait_rows AS (
    SELECT sr.*, rd.rfm_recency, rd.rfm_frequency, rd.rfm_monetary,
           er.event_count_30d, er.last_event_at, er.video_views_30d,
           le.source_key AS last_engagement_channel, so.source_keys
    FROM status_rows sr
    LEFT JOIN ranked_donors rd ON rd.profile_id=sr.profile_id
    JOIN event_rollup er ON er.profile_id=sr.profile_id
    JOIN source_rollup so ON so.profile_id=sr.profile_id
    LEFT JOIN last_engagement le ON le.profile_id=sr.profile_id
    WHERE CAST(:profile_ids AS bigint[]) IS NULL
       OR sr.profile_id=ANY(CAST(:profile_ids AS bigint[]))
)
INSERT INTO profile_traits (
    profile_id, gift_count_total, ltv_total, gift_amount_12m, gift_count_12m,
    first_gift_date, last_gift_date, largest_gift_amount, avg_gift_amount,
    is_recurring_active, days_since_last_gift, donor_status,
    rfm_recency, rfm_frequency, rfm_monetary, rfm_score,
    event_count_30d, last_event_at, video_views_30d, last_engagement_channel,
    source_keys, computed_at
)
SELECT profile_id, gift_count_total, ltv_total, gift_amount_12m, gift_count_12m,
       first_gift_date, last_gift_date, largest_gift_amount, avg_gift_amount,
       is_recurring_active, days_since_last_gift, donor_status,
       rfm_recency, rfm_frequency, rfm_monetary,
       CASE WHEN rfm_recency IS NULL THEN NULL
            ELSE rfm_recency::text || rfm_frequency::text || rfm_monetary::text END,
       event_count_30d, last_event_at, video_views_30d, last_engagement_channel,
       source_keys, clock_timestamp()
FROM trait_rows
ON CONFLICT (profile_id) DO UPDATE SET
    gift_count_total=EXCLUDED.gift_count_total, ltv_total=EXCLUDED.ltv_total,
    gift_amount_12m=EXCLUDED.gift_amount_12m, gift_count_12m=EXCLUDED.gift_count_12m,
    first_gift_date=EXCLUDED.first_gift_date, last_gift_date=EXCLUDED.last_gift_date,
    largest_gift_amount=EXCLUDED.largest_gift_amount, avg_gift_amount=EXCLUDED.avg_gift_amount,
    is_recurring_active=EXCLUDED.is_recurring_active,
    days_since_last_gift=EXCLUDED.days_since_last_gift, donor_status=EXCLUDED.donor_status,
    rfm_recency=EXCLUDED.rfm_recency, rfm_frequency=EXCLUDED.rfm_frequency,
    rfm_monetary=EXCLUDED.rfm_monetary, rfm_score=EXCLUDED.rfm_score,
    event_count_30d=EXCLUDED.event_count_30d, last_event_at=EXCLUDED.last_event_at,
    video_views_30d=EXCLUDED.video_views_30d,
    last_engagement_channel=EXCLUDED.last_engagement_channel,
    source_keys=EXCLUDED.source_keys, computed_at=EXCLUDED.computed_at
""")

_SNAPSHOT_SQL = text("""
INSERT INTO trait_snapshots (month, donor_status, profile_count, ltv_sum, giving_12m_sum)
SELECT date_trunc('month', CAST(:as_of AS date))::date, pt.donor_status,
       count(*)::integer, COALESCE(sum(pt.ltv_total), 0), COALESCE(sum(pt.gift_amount_12m), 0)
FROM profile_traits pt
JOIN profiles p ON p.id=pt.profile_id AND p.merged_into_id IS NULL AND p.is_deleted=false
WHERE NOT EXISTS (
    SELECT 1 FROM jobs j
    WHERE j.type='import.run' AND j.status IN ('queued', 'running')
)
AND NOT EXISTS (SELECT 1 FROM source_records sr WHERE sr.resolved_at IS NULL)
GROUP BY pt.donor_status
ON CONFLICT (month, donor_status) DO NOTHING
""")


def recompute_traits(db: Session, *, as_of: date | None = None,
                     profile_ids: list[int] | None = None,
                     write_snapshot: bool = True) -> int:
    """Recompute all active profiles or an incremental subset in one SQL statement.

    RFM ranks are always computed against the complete donor population, even for
    incremental writes, so selected profiles retain globally meaningful quintiles.
    """
    if profile_ids is not None and not profile_ids:
        return 0
    # The lock lasts through the caller's transaction (including its dirty-row
    # cleanup), preventing a full pass and a dirty pass from deleting each
    # other's selected rows while updating the same profile traits.
    db.execute(text(
        "SELECT pg_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"
    ))
    pinned_date = as_of or date.today()
    if profile_ids is not None and write_snapshot:
        month = pinned_date.replace(day=1)
        snapshot_exists = db.execute(text("""
            SELECT EXISTS (SELECT 1 FROM trait_snapshots WHERE month=:month)
        """), {"month": month}).scalar_one()
        if not snapshot_exists:
            # The first computation for a month must cover the whole active
            # population before recording its immutable monthly snapshot.
            profile_ids = None
    result = db.execute(_RECOMPUTE_SQL, {
        "as_of": pinned_date,
        "profile_ids": profile_ids,
    })
    updated = result.rowcount
    if profile_ids is None:
        db.execute(text("""
            DELETE FROM profile_traits pt
            USING profiles p
            WHERE pt.profile_id=p.id
              AND (p.merged_into_id IS NOT NULL OR p.is_deleted=true)
        """))
    if write_snapshot:
        db.execute(_SNAPSHOT_SQL, {"as_of": pinned_date})
    from app.dashboards.rollups import refresh_dashboard_rollups
    refresh_dashboard_rollups(db, as_of=pinned_date)
    return updated


def backfill_trait_snapshots(db: Session, *, as_of: date | None = None) -> int:
    """Build up to 24 monthly snapshots from historical gift dates once."""
    pinned_date = as_of or date.today()
    missing_months = db.execute(text("""
        WITH months AS (
            SELECT generated.month::date AS month
            FROM generate_series(
                date_trunc('month', CAST(:as_of AS date)) - interval '23 months',
                date_trunc('month', CAST(:as_of AS date)),
                interval '1 month'
            ) AS generated(month)
        )
        SELECT count(*) FROM months m
        WHERE NOT EXISTS (SELECT 1 FROM trait_snapshots ts WHERE ts.month=m.month)
    """), {"as_of": pinned_date}).scalar_one()
    if not missing_months:
        return 0
    db.execute(text("""
        WITH months AS (
            SELECT generated.month::date AS month
            FROM generate_series(
                date_trunc('month', CAST(:as_of AS date)) - interval '23 months',
                date_trunc('month', CAST(:as_of AS date)),
                interval '1 month'
            ) AS generated(month)
            WHERE NOT EXISTS (
                SELECT 1 FROM trait_snapshots ts
                WHERE ts.month=generated.month::date
            )
        ),
        profile_months AS MATERIALIZED (
            SELECT p.id AS profile_id, m.month, (m.month + interval '1 month - 1 day')::date AS month_end
            FROM profiles p CROSS JOIN months m
            WHERE p.merged_into_id IS NULL AND p.is_deleted=false
        ),
        gift_stats AS (
            SELECT pm.profile_id, pm.month, pm.month_end,
                   min(g.gift_date) AS first_gift_date,
                   max(g.gift_date) AS last_gift_date,
                   count(g.id)::integer AS gift_count_total,
                   COALESCE(sum(g.amount), 0)::numeric(14,2) AS ltv_total,
                   COALESCE(sum(g.amount) FILTER (
                       WHERE g.gift_date >= pm.month_end - 364
                   ), 0)::numeric(14,2) AS giving_12m
            FROM profile_months pm
            LEFT JOIN gifts g ON g.profile_id=pm.profile_id AND g.gift_date <= pm.month_end
            GROUP BY pm.profile_id, pm.month, pm.month_end
        ),
        latest_gaps AS (
            SELECT profile_id, month,
                   max(previous_gift_date) FILTER (WHERE newest_rank=1) AS previous_gift_date
            FROM (
                SELECT pm.profile_id, pm.month, g.gift_date,
                       lag(g.gift_date) OVER (PARTITION BY pm.profile_id, pm.month ORDER BY g.gift_date, g.id) AS previous_gift_date,
                       row_number() OVER (PARTITION BY pm.profile_id, pm.month ORDER BY g.gift_date DESC, g.id DESC) AS newest_rank
                FROM profile_months pm
                JOIN gifts g ON g.profile_id=pm.profile_id AND g.gift_date <= pm.month_end
            ) ordered
            GROUP BY profile_id, month
        ),
        statuses AS (
            SELECT gs.month, gs.profile_id, gs.ltv_total, gs.giving_12m,
                   CASE
                     WHEN gs.last_gift_date IS NULL THEN 'prospect'
                     WHEN gs.month_end - gs.last_gift_date <= 365
                          AND gs.first_gift_date >= gs.month_end - 365 THEN 'new'
                     WHEN gs.month_end - gs.last_gift_date <= 365
                          AND lg.previous_gift_date IS NOT NULL
                          AND gs.last_gift_date - lg.previous_gift_date > 730 THEN 'reactivated'
                     WHEN gs.month_end - gs.last_gift_date <= 365 THEN 'active'
                     WHEN gs.month_end - gs.last_gift_date <= 730 THEN 'lapsing'
                     ELSE 'lapsed'
                   END AS donor_status
            FROM gift_stats gs
            LEFT JOIN latest_gaps lg USING (profile_id, month)
        )
        INSERT INTO trait_snapshots (month, donor_status, profile_count, ltv_sum, giving_12m_sum)
        SELECT month, donor_status, count(*)::integer, sum(ltv_total), sum(giving_12m)
        FROM statuses GROUP BY month, donor_status
        ON CONFLICT (month, donor_status) DO NOTHING
    """), {"as_of": pinned_date})
    return missing_months