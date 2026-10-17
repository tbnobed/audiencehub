"""Production-review arithmetic, hand-computed fixtures, and independent raw SQL.

The imported fixture migrates a fresh random schema for EACH test. Only run via
CONSENT_TEST_DATABASE_URL or the local benchmark --test-suite harness.
"""
import hashlib
import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from test_consent_ingestion_pg import db
from app.dashboards import api, cache
from app.dashboards.health import health_counts
from app.dashboards.overview import retention_window
from app.traits.engine import recompute_traits


def fixture(db):
    db.execute(text("""
      INSERT INTO sources(id,key,name,kind) OVERRIDING SYSTEM VALUE
      VALUES(1,'crm','CRM','crm'),(2,'web','Web','web');
      INSERT INTO profiles(id) OVERRIDING SYSTEM VALUE VALUES(1),(2),(3),(4),(5);
      INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,campaign,channel,fund,appeal_code)
      VALUES
        (1,'a',1,10,'2022-12-31','General','mail','A','Y'),
        (1,'b',1,20,'2023-01-01','General','mail','A','Y'),
        (1,'c',2,30,'2023-12-31','Winter','online','B','Z'),
        (1,'d',1,40,'2024-01-01','General','mail','A','Y'),
        (1,'e',3,50,'2024-11-01','Winter','online','B','Z'),
        (1,'f',3,60,'2024-12-31','Winter','online','B','Z'),
        (1,'g',3,70,'2024-12-31','Winter','online','B','Z'),
        (1,'h',4,80,'2023-11-01','Winter','mail','B','Z'),
        (1,'outside',2,999,'2025-01-01','Future','online','B','Z');
      INSERT INTO source_records(source_id,external_id,profile_id,email_norm,phone_e164,address1,city,raw_hash)
      VALUES(1,'p1',1,'a@example.org',NULL,'','Rome','a'),
        (1,'p2',2,NULL,'+12025550123',NULL,NULL,'b'),
        (2,'p1',1,'a@example.org',NULL,NULL,NULL,'c'),
        (2,'p3',3,NULL,NULL,NULL,NULL,'d');
      INSERT INTO events(source_id,profile_id,type,name,occurred_at,message_id)
      VALUES(2,1,'track','View','2022-12-01','a'),
        (2,3,'track','View','2024-10-01','b'),
        (2,3,'track','View','2024-10-01','c'),
        (2,NULL,'track','Click','2024-10-02','d');
    """))
    recompute_traits(db, as_of=date(2025, 12, 31))
    db.commit()


def build(db, name, start=date(2020, 1, 1), end=date(2024, 12, 31)):
    return api._build_dashboard(db, name, *api._date_range(start, end))


def sql(db, query):
    return [dict(r) for r in db.execute(text(query)).mappings()]


def test_review_retention_is_adjacent_trailing_years_not_overlapping_range(db):
    fixture(db)
    k = build(db, "overview")["overview"]["kpis"]["retention_yoy"]
    # Prior 2023 = {1,2,4}; trailing 2024 = {1,3}; intersection = {1}.
    assert (k["denominator"], k["retained"], k["value"]) == (3, 1, 100/3)
    assert k["window"] == {"from": "2024-01-01", "to": "2024-12-31",
                           "prior_from": "2023-01-01", "prior_to": "2023-12-31"}
    direct = sql(db, """
      WITH prior AS (SELECT DISTINCT profile_id FROM gifts WHERE gift_date BETWEEN '2023-01-01' AND '2023-12-31'),
      recent AS (SELECT DISTINCT profile_id FROM gifts WHERE gift_date BETWEEN '2024-01-01' AND '2024-12-31')
      SELECT count(*) AS base,count(recent.profile_id) AS retained FROM prior LEFT JOIN recent USING(profile_id)
    """)[0]
    assert (k["denominator"], k["retained"]) == (direct["base"], direct["retained"])
    # A shorter selection with the same end MUST NOT change retention.
    short = build(db, "overview", date(2024, 12, 31))["overview"]["kpis"]["retention_yoy"]
    assert short["value"] == k["value"]
    assert short["window"] == k["window"]


@pytest.mark.parametrize("end,expected", [
    (date(2024, 2, 29), ("2023-03-01", "2022-03-01", "2023-02-28")),
    (date(2025, 2, 28), ("2024-02-29", "2023-03-01", "2024-02-28")),
    (date(2024, 6, 17), ("2023-06-18", "2022-06-18", "2023-06-17")),
])
def test_review_retention_calendar_boundaries(end, expected):
    w = retention_window(end)
    assert (w["from"], w["prior_from"], w["prior_to"]) == expected


def test_review_distinct_calendar_month_takeaway_uses_chart_totals(db):
    fixture(db)
    o = build(db, "overview")["overview"]
    # All giving=360: Dec=10+30+60+70=170, Nov=50+80=130.
    assert [(m["name"], m["amount"]) for m in o["top_two_months"]] == [
        ("December", 170), ("November", 130)]
    assert sum(x["amount"] for x in o["monthly_giving"]) == 360
    assert o["top_two_month_share"] == pytest.approx(300/360*100)
    direct = sql(db, """
      SELECT extract(month FROM gift_date)::int AS month,sum(amount) AS amount FROM gifts
      WHERE gift_date BETWEEN '2020-01-01' AND '2024-12-31'
      GROUP BY 1 ORDER BY amount DESC,month LIMIT 2
    """)
    assert [(x["month"], x["amount"]) for x in o["top_two_months"]] == [
        (r["month"], float(r["amount"])) for r in direct]


def test_review_lapsing_is_current_traits_on_every_card_and_attention(db):
    fixture(db)
    # On 2025-12-31, only p1 (2024-01-01) lapses; historical status differs.
    direct = db.scalar(text("SELECT count(*) FROM profile_traits WHERE donor_status='lapsing'"))
    assert direct == 1
    user = SimpleNamespace(role="admin")
    full = api._endpoint("overview")(date(2020, 1, 1), date(2024, 12, 31), user, db)["overview"]
    status = api._card_endpoint("partner-status")(date(2020, 1, 1), date(2024, 12, 31), user, db)["partner_status"]
    assert next(r["count"] for r in status["statuses"] if r["status"] == "lapsing") == direct
    assert full["stats"]["lapsing"]["value"] == direct
    assert full["stats"]["lapsing"]["label"] == status["label"] == "as of today"
    assert status["as_of"] == "2025-12-31"
    attention = api._card_endpoint("needs-attention")(None, None, user, db)["attention"]
    assert next(r for r in attention if r["title"] == "Partners are lapsing")["explanation"].startswith("1 partners")
    for day in (date(2021, 1, 1), date(2024, 12, 31)):
        assert build(db, "overview", day, day)["overview"]["stats"]["lapsing"]["value"] == direct


def test_review_giving_charts_match_hand_totals_and_raw_sql(db):
    fixture(db)
    charts = build(db, "giving")["charts"]
    assert sum(r["amount"] for r in charts["monthly_giving"]) == 360
    assert sum(r["gifts"] for r in charts["monthly_giving"]) == 8
    comparisons = {
        "monthly_giving": ("date_trunc('month',gift_date)::date", "month", "gifts"),
        "by_channel": ("channel", "channel", "gifts"),
        "by_fund": ("fund", "fund", "gifts"),
        "top_campaigns": ("campaign", "campaign", "gifts"),
        "appeal_codes": ("appeal_code", "appeal_code", "responses"),
        "gift_size_distribution": ("""CASE WHEN amount<25 THEN '$1–24' WHEN amount<50 THEN '$25–49'
          WHEN amount<100 THEN '$50–99' WHEN amount<250 THEN '$100–249' WHEN amount<500 THEN '$250–499'
          WHEN amount<1000 THEN '$500–999' WHEN amount<5000 THEN '$1k–4.9k' ELSE '$5k+' END""", "bucket", "gifts"),
    }
    for chart, (expr, label, count) in comparisons.items():
        raw = sql(db, f"""SELECT {expr} AS label,count(*) AS n,sum(amount) AS amount FROM gifts
          WHERE gift_date BETWEEN '2020-01-01' AND '2024-12-31' GROUP BY 1""")
        assert {str(r[label]): (r[count], r["amount"]) for r in charts[chart]} == {
            str(r["label"]): (r["n"], float(r["amount"])) for r in raw}, chart
    raw = sql(db, """
      WITH firsts AS (SELECT profile_id,min(gift_date) AS first_date FROM gifts GROUP BY profile_id),
      months AS (SELECT DISTINCT g.profile_id,date_trunc('month',gift_date)::date AS month,f.first_date
        FROM gifts g JOIN firsts f USING(profile_id) WHERE gift_date BETWEEN '2020-01-01' AND '2024-12-31')
      SELECT month,count(*) FILTER(WHERE first_date>=month) AS new_donors,
        count(*) FILTER(WHERE first_date<month) AS returning_donors FROM months GROUP BY month
    """)
    assert {r["month"]: (r["new_donors"], r["returning_donors"]) for r in charts["new_returning"]} == {
        r["month"].isoformat(): (r["new_donors"], r["returning_donors"]) for r in raw}


def test_review_giving_and_overview_use_same_resolved_population(db):
    fixture(db)
    db.execute(text("""
      UPDATE profiles SET merged_into_id=1 WHERE id=4;
      UPDATE profiles SET is_deleted=true WHERE id=5;
      INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date)
      VALUES(1,'deleted',5,10000,'2024-01-01'),(1,'unresolved',NULL,20000,'2024-01-01');
    """))
    recompute_traits(db, as_of=date(2025, 12, 31))
    db.commit()
    giving = build(db, "giving")["charts"]
    overview = build(db, "overview")["overview"]
    direct = db.scalar(text("""SELECT sum(g.amount) FROM gifts g JOIN profiles p ON p.id=g.profile_id
      WHERE NOT p.is_deleted AND p.merged_into_id IS NULL
        AND gift_date BETWEEN '2020-01-01' AND '2024-12-31'"""))
    assert direct == 280  # 360 less merged profile 4's $80; exclude deleted/unresolved.
    assert overview["kpis"]["giving"]["value"] == direct
    for chart in ("monthly_giving", "by_channel", "by_fund", "top_campaigns",
                  "appeal_codes", "gift_size_distribution"):
        assert sum(r["amount"] for r in giving[chart]) == direct, chart


def test_review_current_cards_stay_consistent_during_stale_series_refresh(db, monkeypatch):
    fixture(db)
    user = SimpleNamespace(role="admin")
    start, end = date(2020, 1, 1), date(2024, 12, 31)
    assert api._endpoint("overview")(start, end, user, db)["overview"]["stats"]["lapsing"]["value"] == 1
    # Keep the old date-series cache deliberately stale while a new generation publishes.
    monkeypatch.setattr(cache._executor, "submit", lambda *args: None)
    recompute_traits(db, as_of=date(2025, 6, 30))
    db.commit()
    assert db.scalar(text("SELECT count(*) FROM profile_traits WHERE donor_status='lapsing'")) == 2
    full = api._endpoint("overview")(start, end, user, db)["overview"]
    assert full["stats"]["lapsing"]["value"] == 2
    assert next(r for r in full["partner_status"]["statuses"] if r["status"] == "lapsing")["count"] == 2
    attention = api._card_endpoint("needs-attention")(start, end, user, db)["attention"]
    assert next(r for r in attention if r["title"] == "Partners are lapsing")["explanation"].startswith("2 partners")


def test_review_retention_engagement_and_all_time_sources_match_sql(db):
    fixture(db)
    retention = build(db, "retention")["charts"]
    # 2023 population {1,2,4} -> 2024 retained {1}; 2022 {1}->2023 {1}.
    assert next(r for r in retention["retention_by_year"] if r["year"] == 2023)["retained"] == 1
    raw = sql(db, """
      WITH y AS (SELECT DISTINCT profile_id,extract(year FROM gift_date)::int AS year FROM gifts)
      SELECT a.year,count(*) AS donors,count(b.profile_id) AS retained FROM y a
      LEFT JOIN y b ON a.profile_id=b.profile_id AND b.year=a.year+1
      WHERE a.year BETWEEN 2020 AND 2024 GROUP BY a.year
    """)
    assert {r["year"]: (r["donors"], r["retained"]) for r in retention["retention_by_year"]} == {
        r["year"]: (r["donors"], r["retained"]) for r in raw}
    raw = sql(db, """
      WITH y AS (SELECT DISTINCT profile_id,extract(year FROM gift_date)::int AS year FROM gifts),
      f AS (SELECT profile_id,min(year) AS first_year FROM y GROUP BY profile_id)
      SELECT first_year,year-first_year AS years_after_first,count(*) AS retained FROM f JOIN y USING(profile_id)
      WHERE first_year>=2020 AND year<=2024 GROUP BY 1,2
    """)
    assert {(r["first_year"], r["years_after_first"]): r["retained"] for r in retention["cohorts"]} == {
        (r["first_year"], r["years_after_first"]): r["retained"] for r in raw}
    # Persisted snapshots are independently published by traits; arbitrary historical selections must read them.
    db.execute(text("""INSERT INTO trait_snapshots(month,donor_status,profile_count,ltv_sum,giving_12m_sum)
      VALUES('2024-01-01','active',1,70,40)"""))
    retention = build(db, "retention")["charts"]
    assert retention["status_over_time"] == [{"month": "2024-01-01", "status": "active", "profiles": 1,
                                              "ltv_sum": 70, "giving_12m_sum": 40}]
    raw = sql(db, "SELECT month,donor_status AS status,profile_count AS profiles,ltv_sum,giving_12m_sum FROM trait_snapshots WHERE month BETWEEN '2020-01-01' AND '2024-12-31'")
    assert retention["status_over_time"] == api._safe_rows(raw)
    engagement = build(db, "engagement")["charts"]
    assert sum(r["events"] for r in engagement["events_by_day"]) == 4
    for chart, dimensions in (("events_by_day", "e.occurred_at::date AS day,s.key AS source"),
                               ("top_event_names", "e.name,e.type,s.key AS source")):
        raw = sql(db, f"""SELECT {dimensions},count(*) AS events FROM events e JOIN sources s ON s.id=e.source_id
          WHERE occurred_at>='2020-01-01' AND occurred_at<'2025-01-01' GROUP BY 1,2{',3' if chart=='top_event_names' else ''}""")
        assert sorted(engagement[chart], key=str) == sorted(api._safe_rows(raw), key=str)
    assert sum(r["conversions"] for r in engagement["viewer_to_donor"]) == 2
    raw = sql(db, """
      WITH e AS (SELECT profile_id,min(occurred_at::date) AS day FROM events GROUP BY profile_id),
      g AS (SELECT profile_id,min(gift_date) AS day FROM gifts GROUP BY profile_id)
      SELECT date_trunc('month',g.day)::date AS month,count(*) AS conversions
      FROM g JOIN e USING(profile_id) WHERE e.day<g.day AND g.day BETWEEN '2020-01-01' AND '2024-12-31' GROUP BY 1
    """)
    assert sorted(engagement["viewer_to_donor"], key=str) == sorted(api._safe_rows(raw), key=str)
    sources = build(db, "sources", date(2020, 1, 1), date(2020, 1, 2))["charts"]
    assert sources == build(db, "sources", date(2024, 1, 1))["charts"]
    assert {r["source"]: r["profiles"] for r in sources["profiles_by_source"]} == {"crm": 2, "web": 2}
    assert next(r for r in sources["overlap_matrix"] if r["source_a"] == "crm" and r["source_b"] == "web")["profiles"] == 1
    for chart in ("profiles_by_source", "identifier_coverage"):
        assert sum(r["profiles"] for r in sources[chart]) == db.scalar(text(
            "SELECT count(*) FROM (SELECT DISTINCT source_id,profile_id FROM source_records) x"))
    assert {r["source"]: r["profiles"] for r in sources["profiles_by_source"]} == {
        r["source"]: r["profiles"] for r in sql(db, """SELECT s.key AS source,count(DISTINCT profile_id) AS profiles
          FROM source_records sr JOIN sources s ON s.id=sr.source_id GROUP BY s.key""")}
    coverage = next(r for r in sources["identifier_coverage"] if r["source"] == "crm")
    assert (coverage["email_pct"], coverage["phone_pct"], coverage["address_pct"]) == (50, 50, 50)
    raw = sql(db, """SELECT s.key AS source,count(DISTINCT profile_id) AS profiles,
      count(DISTINCT profile_id) FILTER(WHERE email_norm IS NOT NULL) AS email_profiles,
      count(DISTINCT profile_id) FILTER(WHERE phone_e164 IS NOT NULL) AS phone_profiles,
      count(DISTINCT profile_id) FILTER(WHERE city IS NOT NULL) AS address_profiles
      FROM source_records sr JOIN sources s ON s.id=sr.source_id GROUP BY s.key ORDER BY s.key""")
    for r in raw:
        for field in ("email", "phone", "address"):
            r[field+"_pct"] = 100*r[field+"_profiles"]/r["profiles"]
    assert sorted(sources["identifier_coverage"], key=str) == sorted(raw, key=str)
    raw = sql(db, """WITH p AS (SELECT DISTINCT profile_id,s.key AS source FROM source_records sr
      JOIN sources s ON s.id=sr.source_id)
      SELECT a.source AS source_a,b.source AS source_b,count(*) AS profiles FROM p a JOIN p b USING(profile_id)
      WHERE a.source<=b.source GROUP BY 1,2 ORDER BY 1,2""")
    assert sources["overlap_matrix"] == raw


def test_review_rejected_reports_are_same_live_all_time_source_and_reopen(db):
    fixture(db)
    db.execute(text("""
      INSERT INTO imports(id,source_id,filename,record_type,status,rows_rejected,created_at)
      OVERRIDING SYSTEM VALUE VALUES
        (1,1,'old.csv','gift','completed',3,'2026-01-01'),
        (2,1,'failed.csv','gift','failed',2,'2026-01-02'),
        (3,1,'running.csv','gift','running',9,'2026-01-03'),
        (4,1,'reviewed.csv','gift','completed',4,'2026-01-04');
      INSERT INTO audit_log(actor_type,action,entity_type,entity_id,details)
      VALUES('system','data_health.rejected_report.review','import','4','{"rows_rejected": 4}');
    """))
    db.commit()
    user = SimpleNamespace(role="admin")
    def reports():
        return api._endpoint("data-health")(date(2020, 1, 1), date(2024, 12, 31), user, db)["charts"]["rejected_rows_by_import"]
    assert health_counts(db)["rejected_reports"] == len(reports()) == 2
    assert {r["import_id"] for r in reports()} == {1, 2}
    assert sum(r["rejected"] for r in reports()) == 5
    # Review and changed row totals must be reflected without waiting for cache TTL.
    db.execute(text("""INSERT INTO audit_log(actor_type,action,entity_type,entity_id,details)
      VALUES('system','data_health.rejected_report.review','import','1','{"rows_rejected": 3}')"""))
    db.commit()
    assert health_counts(db)["rejected_reports"] == len(reports()) == 1
    db.execute(text("UPDATE imports SET rows_rejected=4 WHERE id=1"))
    db.commit()
    assert health_counts(db)["rejected_reports"] == len(reports()) == 2


def test_review_old_cache_contract_and_failed_build_are_never_served(db, monkeypatch):
    fixture(db)
    key = ("overview", date(2020, 1, 1), date(2024, 12, 31))
    legacy = hashlib.sha256(json.dumps(key, default=str).encode()).hexdigest()
    db.execute(text("""INSERT INTO dashboard_cache(key,payload,computed_at,generation)
      VALUES(:key,'{"obsolete": true}',now(),:version)"""), {"key": legacy, "version": cache.generation(db)})
    db.commit()
    assert "obsolete" not in api._dashboard(*key, db)
    def fail(*args):
        raise RuntimeError("fixture build failure")
    monkeypatch.setattr(api, "build_cache_key", fail)
    with pytest.raises(RuntimeError, match="fixture build failure"):
        api._dashboard("giving", date(2020, 1, 1), date(2024, 12, 31), db)
    assert not db.scalar(text("SELECT count(*) FROM dashboard_cache WHERE key=:key"),
                         {"key": cache.cache_digest(("giving", key[1], key[2]))})
