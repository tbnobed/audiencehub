"""Server-side regression coverage; never run automatically by the agent."""
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import text
from test_consent_ingestion_pg import db
from app.segments.compiler import compile_definition
from app.segments.projection import refresh_projections


def setup_properties(db):
    db.execute(text("""
        INSERT INTO sources(id,key,name,kind,priority) OVERRIDING SYSTEM VALUE
        VALUES(1,'primary_feed','Primary','csv',1),(2,'fallback_feed','Fallback','csv',2);
        INSERT INTO profiles(id) OVERRIDING SYSTEM VALUE VALUES(1),(2),(3);
        INSERT INTO profile_traits(profile_id) VALUES(1),(2),(3);
    """))
    for source, external, profile, updated, attrs in [
        (1, "a", 1, "2026-01-01", {"tbn_plus_subscription": "basic", "has_active_email": False}),
        (2, "b", 1, "2026-02-01", {"tbn_plus_subscription": "premium_monthly", "has_active_email": True}),
        (1, "c", 2, "2026-01-01", {"rfm_date": "", "last_seen": "2026-10-01T00:00:00+00:00",
                                  "last_contact": "2027-01-01T00:00:00+00:00"}),
        (2, "d", 2, "2026-02-01", {"rfm_date": "2026-01-01"}),
        (1, "e", 3, "2026-01-01", {"tbn_plus_subscription": "basic"}),
        (1, "f", 3, "2026-02-01", {"tbn_plus_subscription": "premium_annual"}),
    ]:
        db.execute(text("""
            INSERT INTO source_records(source_id,external_id,profile_id,updated_at,attributes,raw_hash)
            VALUES(:s,:ext,:p,:at,CAST(:attrs AS jsonb),:ext)
        """), {"s": source, "ext": external, "p": profile, "at": updated, "attrs": json.dumps(attrs)})
    refresh_projections(db)


def members(db, root):
    return sorted(db.scalars(compile_definition(
        db, {"version": 1, "root": root}, date(2026, 10, 18))))


def rule(field, operator, **kwargs):
    return {"type": "attribute", "field": field, "operator": operator, **kwargs}


def test_projection_precedence_nulls_and_removal(db):
    setup_properties(db)
    assert members(db, rule("tbn_plus_subscription", "eq", value="basic")) == [1]
    assert members(db, rule("tbn_plus_subscription", "eq", value="premium_annual")) == [3]
    assert members(db, rule("has_active_email", "is_false")) == [1]
    assert members(db, rule("rfm_date", "is_null")) == [1, 2, 3]
    assert members(db, {"op": "and", "negate": True,
                        "rules": [rule("has_active_email", "is_true")]}) == [1, 2, 3]
    db.execute(text("DELETE FROM source_records WHERE profile_id=1"))
    refresh_projections(db, [1])
    assert members(db, rule("tbn_plus_subscription", "eq", value="basic")) == []


def test_activity_index_prefilter_preserves_future_date_or_semantics(db):
    setup_properties(db)
    root = {"op": "or", "rules": [
        rule("last_seen", "within_previous_days", value=120),
        rule("last_contact", "within_previous_days", value=120),
    ]}
    assert members(db, root) == [2]
    statement = str(compile_definition(db, {"version": 1, "root": root}, date(2026, 10, 18)))
    assert "last_activity_at >=" in statement
    assert "source_records" not in statement


def test_sample_does_not_aggregate_membership(db):
    setup_properties(db)
    from app.segments.api import Preview, sample
    from sqlalchemy import event
    sql = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement.lower())
    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        with patch("app.segments.api.sample_rows", return_value=[{"id": 1}]):
            result = sample(Preview(definition={"version": 1, "root":
                rule("tbn_plus_subscription", "eq", value="basic")}),
                user=SimpleNamespace(role="admin"), db=db)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
    assert result["items"] == [{"id": 1}]
    assert any("order by" in s and "limit" in s for s in sql)
    assert not any("count(" in s or "unnest(" in s or "group by" in s for s in sql)
