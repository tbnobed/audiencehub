"""Production-server regression coverage; do not run against live data."""
from datetime import date
import json

import pytest
from sqlalchemy import text
from test_consent_ingestion_pg import db
from app.segments.compiler import compile_definition
from app.segments.projection import refresh_projections

PIN = date(2026, 10, 18)


def event(db, profile, message, occurred, name="donation", source=1, **properties):
    db.execute(text("""
        INSERT INTO events(profile_id,source_id,message_id,occurred_at,type,name,properties)
        VALUES(:pid,:source,:message,:at,'track',:name,CAST(:props AS jsonb))
    """), {"pid": profile, "source": source, "message": message,
           "at": occurred, "name": name, "props": json.dumps(properties)})


def fixture(db):
    db.execute(text("""
        INSERT INTO sources(id,key,name,kind) OVERRIDING SYSTEM VALUE
          VALUES(1,'donation_events','Donations','csv'),(2,'other_feed','Other','csv');
        INSERT INTO profiles(id) OVERRIDING SYSTEM VALUE
          VALUES(1),(2),(3),(4),(5),(6),(7),(8),(9);
        INSERT INTO profile_traits(profile_id) SELECT id FROM profiles;
    """))
    event(db, 1, "recent", "2026-10-17T12:00:00+00:00", fund="Mission", amount=50)
    event(db, 2, "old", "2023-01-01T00:00:00+00:00")
    event(db, 4, "yesterday", "2026-10-17T00:00:00+00:00")
    event(db, 4, "today", "2026-10-18T00:00:00+00:00")
    event(db, 5, "earlier", "2026-10-16T00:00:00+00:00")
    event(db, 5, "future", "2027-01-01T00:00:00+00:00")
    event(db, 6, "returned", "2026-10-17T00:00:00+00:00")
    event(db, 6, "prior", "2023-01-01T00:00:00+00:00")
    event(db, 6, "return", "2026-10-17T12:00:00+00:00",
          name="donation_return", original_donation_id="gift:returned")
    event(db, 7, "same-id", "2026-10-17T00:00:00+00:00")
    event(db, 7, "wrong-source-return", "2026-10-17T12:00:00+00:00",
          name="donation_return", source=2, original_donation_id="same-id")
    event(db, 8, "ledger-returned", "2026-10-17T00:00:00+00:00")
    event(db, 8, "remaining", "2026-10-16T00:00:00+00:00")
    db.execute(text("""
        INSERT INTO gifts(profile_id,source_id,external_id,amount,gift_date,attributes)
        VALUES(8,1,'refund',-50,'2026-10-17',CAST(:attrs AS jsonb))
    """), {"attrs": json.dumps({
        "is_refund": True, "original_gift_external_id": "donation:ledger-returned",
    })})
    event(db, 9, "boundary", "2026-09-18T00:00:00+00:00")
    refresh_projections(db)


def definition(operator="is_true", where=None, window=None):
    rule = {"type": "event", "name": "donation", "aggregate": "exists",
            "operator": operator,
            "window": window or {"kind": "previous_days", "days": 30}}
    if where is not None:
        rule["where"] = where
    return {"version": 1, "root": {"op": "and", "rules": [rule]}}


def test_latest_nonreturned_and_incremental_clear(db):
    fixture(db)
    values = dict(db.execute(text("SELECT id,last_donation_at FROM profiles")).all())
    assert values[3] is None
    assert values[6].date() == date(2023, 1, 1)
    assert values[8].date() == date(2026, 10, 16)
    event(db, 1, "new-return", "2026-10-18T12:00:00+00:00",
          name="donation_return", original_donation_id="recent")
    refresh_projections(db, [1])
    assert db.scalar(text("SELECT last_donation_at FROM profiles WHERE id=1")) is None
    assert db.scalar(text("SELECT last_donation_at FROM profiles WHERE id=7")) is not None


@pytest.mark.parametrize("operator,expected", [
    ("is_true", [1, 4, 5, 7, 8, 9]),
    ("is_false", [2, 3, 6]),
])
def test_recent_donation_range_nulls_returns_and_boundaries(db, operator, expected):
    fixture(db)
    query = compile_definition(db, definition(operator), PIN)
    assert sorted(db.scalars(query)) == expected
    assert "active_profiles.last_donation_at" in str(query)


def test_today_and_historical_windows_keep_earlier_matching_donations(db):
    fixture(db)
    including_today = definition(window={"kind": "last_days", "days": 1})
    assert sorted(db.scalars(compile_definition(db, including_today, PIN))) == [4]
    historical = definition(window={"kind": "previous_days", "days": 30})
    assert sorted(db.scalars(compile_definition(
        db, historical, date(2026, 9, 19)))) == [9]


@pytest.mark.parametrize("field,value", [
    ("properties.fund", "Mission"), ("properties.amount", "50"),
    ("properties.source_code", "CAMPAIGN"),
])
def test_filtered_donation_rules_retain_event_path(db, field, value):
    fixture(db)
    query = compile_definition(db, definition(where={
        field: {"operator": "eq", "value": value},
    }), PIN)
    sql = str(query)
    assert "last_donation_at" not in sql
    assert "events" in sql


def test_batched_migration_and_runtime_share_frozen_definition(db):
    # Read/compare the frozen SQL without executing a migration in the test.
    import importlib.util
    from pathlib import Path
    from app.segments.donations import UPDATE_LAST_DONATION
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0037_last_donation_at.py"
    spec = importlib.util.spec_from_file_location("last_donation_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert " ".join(migration.BACKFILL.split()) == " ".join(UPDATE_LAST_DONATION.split())
