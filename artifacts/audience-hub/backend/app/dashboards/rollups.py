"""Offline refresh only. Never imported as a request-time refill fallback.

All deletes/inserts, the readiness marker, and cache invalidation commit together
with traits. MVCC readers continue to see the previous complete generation.
"""
import json
import uuid
from datetime import date

from sqlalchemy import text


def refresh_dashboard_rollups(db, *, as_of=None):
    as_of = as_of or date.today()
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('dashboard-rollups'))"))
    for table in ("dashboard_daily", "dashboard_donor_daily", "dashboard_donor_patterns", "dashboard_giving_daily",
                  "dashboard_event_daily", "dashboard_conversion_daily", "dashboard_kpis"):
        db.execute(text(f"DELETE FROM {table}"))
    db.execute(text("""
      INSERT INTO dashboard_donor_daily
      SELECT g.profile_id,g.gift_date,count(*),sum(g.amount),bool_or(g.is_recurring)
      FROM gifts g JOIN active_profiles p ON p.id=g.profile_id
      WHERE g.gift_date IS NOT NULL GROUP BY g.profile_id,g.gift_date
    """))
    db.execute(text("""
      INSERT INTO dashboard_donor_patterns(days,gift_counts,recurring_flags,donors)
      WITH histories AS (
        SELECT array_agg(day ORDER BY day) AS days,
          array_agg(gift_count ORDER BY day) AS gift_counts,
          array_agg(recurring ORDER BY day) AS recurring_flags
        FROM dashboard_donor_daily GROUP BY profile_id)
      SELECT days,gift_counts,recurring_flags,count(*) FROM histories GROUP BY 1,2,3
    """))
    db.execute(text("""
      INSERT INTO dashboard_daily(day,source_id,fund,campaign,channel,
        gift_count,gift_amount,donor_count,new_donor_count,active)
      WITH firsts AS (SELECT profile_id,min(gift_date) AS day FROM gifts GROUP BY profile_id)
      SELECT g.gift_date,g.source_id,g.fund,g.campaign,g.channel,count(*),sum(g.amount),
        count(DISTINCT g.profile_id),
        count(DISTINCT g.profile_id) FILTER(WHERE f.day=g.gift_date),p.id IS NOT NULL
      FROM gifts g LEFT JOIN active_profiles p ON p.id=g.profile_id
      LEFT JOIN firsts f ON f.profile_id=g.profile_id WHERE g.gift_date IS NOT NULL
      GROUP BY g.gift_date,g.source_id,g.fund,g.campaign,g.channel,p.id IS NOT NULL
    """))
    db.execute(text("""
      INSERT INTO dashboard_giving_daily
      SELECT gift_date,appeal_code,
        CASE WHEN amount<25 THEN '$1–24' WHEN amount<50 THEN '$25–49'
          WHEN amount<100 THEN '$50–99' WHEN amount<250 THEN '$100–249'
          WHEN amount<500 THEN '$250–499' WHEN amount<1000 THEN '$500–999'
          WHEN amount<5000 THEN '$1k–4.9k' ELSE '$5k+' END,
        CASE WHEN amount<25 THEN 1 WHEN amount<50 THEN 2 WHEN amount<100 THEN 3
          WHEN amount<250 THEN 4 WHEN amount<500 THEN 5 WHEN amount<1000 THEN 6
          WHEN amount<5000 THEN 7 ELSE 8 END,count(*),sum(amount)
      FROM gifts WHERE gift_date IS NOT NULL GROUP BY 1,2,3,4
    """))
    db.execute(text("""
      INSERT INTO dashboard_event_daily
      SELECT e.occurred_at::date,s.key,e.name,e.type,count(*)
      FROM events e JOIN sources s ON s.id=e.source_id GROUP BY 1,2,3,4
    """))
    db.execute(text("""
      INSERT INTO dashboard_conversion_daily
      WITH e AS (SELECT profile_id,min(occurred_at::date) AS day FROM events GROUP BY profile_id),
           g AS (SELECT profile_id,min(gift_date) AS day FROM gifts GROUP BY profile_id)
      SELECT g.day,count(*) FROM g JOIN e USING(profile_id) WHERE e.day<g.day GROUP BY g.day
    """))
    def put(key, value):
        db.execute(text("INSERT INTO dashboard_kpis VALUES (:day,:key,CAST(:value AS jsonb))"),
                   {"day": as_of, "key": key, "value": json.dumps(value, default=str)})

    counts = db.execute(text("""
      SELECT (SELECT count(*) FROM active_profiles) AS profiles,
        (SELECT count(*) FROM (
          SELECT c.profile_id FROM consents c JOIN active_profiles p ON p.id=c.profile_id
          WHERE c.channel='email' GROUP BY c.profile_id
          HAVING bool_or(c.status IN ('opted_in','subscribed','granted'))
             AND NOT bool_or(c.status IN ('opted_out','unsubscribed','denied','hard_bounce'))
        ) eligible) AS email_opted_in,
        (SELECT count(*) FROM profile_traits WHERE is_recurring_active) AS recurring_partners,
        (SELECT count(*) FROM profile_traits WHERE donor_status='lapsing') AS lapsing
    """)).mappings().one()
    for key, value in counts.items():
        put(key, value)
    put("partner_status", [dict(r) for r in db.execute(text(
        "SELECT donor_status AS status,count(*) AS count FROM profile_traits GROUP BY 1")).mappings()])
    # Non-date source diagnostics are precomputed once, including expensive overlap
    # and blocklist matching; requests only deserialize these small chart payloads.
    from app.dashboards.offline_charts import static_charts
    for key, value in static_charts(db).items():
        put(key, value)
    put("generation", str(uuid.uuid4()))
    # Retain stale entries for immediate SWR; generation mismatch invalidates them.
    # Publishing the new generation is atomic with all preceding replacements.
    for table in ("dashboard_daily", "dashboard_donor_daily", "dashboard_donor_patterns", "dashboard_giving_daily",
                  "dashboard_event_daily", "dashboard_conversion_daily", "dashboard_kpis"):
        db.execute(text(f"ANALYZE {table}"))