"""Exact rollup arithmetic and SQL guard across all public dashboard requests."""
import re
from datetime import date
from types import SimpleNamespace

from sqlalchemy import event, text

from test_consent_ingestion_pg import db
from app.dashboards.api import _endpoint, _card_endpoint, _build_dashboard
from app.dashboards.rollups import refresh_dashboard_rollups


def seed(db):
    db.execute(text("INSERT INTO sources(id,key,name,kind) OVERRIDING SYSTEM VALUE VALUES(1,'test','Test','crm')"))
    db.execute(text("INSERT INTO profiles(id) OVERRIDING SYSTEM VALUE VALUES(1),(2),(3)"))
    db.execute(text("""
      INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date)
      VALUES(1,'a',1,10,'2023-01-15'),(1,'b',3,10,'2023-01-15'),
        (1,'c',1,20,'2024-01-10'),(1,'d',1,30,'2024-01-20'),
        (1,'e',1,40,'2024-01-20'),(1,'f',2,50,'2024-01-15')
    """))
    db.execute(text("""
      INSERT INTO consents(profile_id,channel,status,source_id)
      VALUES(1,'email','opted_in',1),(2,'email','opted_out',1)"""))
    # Source-only consent must never count.
    db.execute(text("""
      INSERT INTO source_records(source_id,external_id,profile_id,email_norm,attributes,raw_hash)
      VALUES(1,'source3',3,'third@example.org','{"email_consent":"opted_in"}','hash')"""))
    refresh_dashboard_rollups(db, as_of=date(2024, 1, 31))
    db.commit()


def test_arbitrary_range_deduplicates_donors_and_retention(db):
    seed(db)
    r = _build_dashboard(db, "overview", date(2024, 1, 10), date(2024, 1, 20),
                         date(2023, 12, 30), date(2024, 1, 9))
    k = r["overview"]["kpis"]
    assert k["giving"]["value"] == 140
    assert k["average_gift"]["value"] == 35
    assert k["active_partners"]["value"] == 2
    assert k["retention_yoy"]["denominator"] == 2
    assert k["retention_yoy"]["retained"] == 1
    assert k["retention_yoy"]["value"] == 50
    assert r["overview"]["stats"]["email_opted_in"]["value"] == 1
    assert r["overview"]["monthly_giving"][0]["gifts"] == 4
    single = _build_dashboard(db, "overview", date(2024, 1, 20), date(2024, 1, 20),
                              date(2024, 1, 19), date(2024, 1, 19))
    assert single["overview"]["kpis"]["active_partners"]["value"] == 1
    assert single["overview"]["kpis"]["average_gift"]["value"] == 35


def test_every_dashboard_and_card_forbids_raw_tables(db):
    seed(db)
    engine = db.get_bind()
    statements = []
    def guard(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        assert not re.search(r"\b(?:FROM|JOIN)\s+(?:public\.)?(?:gifts|events|source_records)\b",
                             statement, re.I), statement
    event.listen(engine, "before_cursor_execute", guard)
    try:
        for name in ("overview", "overview-primary", "giving", "retention", "engagement", "sources", "data-health"):
            _endpoint(name)(date(2024, 1, 10), date(2024, 1, 20), SimpleNamespace(role="admin"), db)
        for name in ("kpis", "giving-by-month", "needs-attention", "campaigns", "partner-status", "email", "attention"):
            _card_endpoint(name)(date(2024, 1, 10), date(2024, 1, 20), SimpleNamespace(role="admin"), db)
    finally:
        event.remove(engine, "before_cursor_execute", guard)
    assert statements


def test_raw_changes_invisible_until_atomic_refresh_and_rollback_preserves_generation(db):
    seed(db)
    old = db.scalar(text("SELECT value FROM dashboard_kpis WHERE key='generation'"))
    db.execute(text("UPDATE gifts SET amount=amount+1"))
    before = db.scalar(text("SELECT sum(gift_amount) FROM dashboard_daily"))
    refresh_dashboard_rollups(db)
    assert db.scalar(text("SELECT sum(gift_amount) FROM dashboard_daily")) == before+6
    assert db.scalar(text("SELECT value FROM dashboard_kpis WHERE key='generation'")) != old
    db.rollback()
    assert db.scalar(text("SELECT sum(gift_amount) FROM dashboard_daily")) == before
    assert db.scalar(text("SELECT value FROM dashboard_kpis WHERE key='generation'")) == old


def test_traits_recompute_publishes_rollups(db):
    from app.traits.engine import recompute_traits
    db.execute(text("INSERT INTO profiles DEFAULT VALUES"))
    recompute_traits(db, as_of=date(2025, 1, 31))
    assert db.scalar(text("SELECT value FROM dashboard_kpis WHERE key='profiles'")) == 1
    assert db.scalar(text("SELECT value FROM dashboard_kpis WHERE key='generation'"))