"""Hand-computed Step 2 PostgreSQL tests; isolated schema fixture only."""
from datetime import date
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_consent_ingestion_pg import db, ingest
from test_master_identity_pg import source, record
from app.canonical import FIELDS, EVENT_NAMES, canonical_attributes, canonical_attributes_many
from app.identity.bulk import resolve_bulk
from app.imports.service import _register_enrichments, _insert_events
from app.imports.mapping import suggest_mapping
from app.imports.validation import map_and_validate_row
from app.resources import import_resources, ResourceBatch
from app.segments.compiler import compile_definition


def ids(db, rule):
    return list(db.scalars(compile_definition(db, {"version": 1, "root": rule}, date(2026, 1, 3))))


def test_import_all_categories_source_priority_models_and_segments(db, tmp_path):
    row = {"external_id": "audit", "email": "audit@example.org"}
    for key, meta in FIELDS.items():
        row[key] = {"text": "evidence", "number": "12.5", "boolean": "false",
                    "date": "2026-01-02", "timestamp": "2026-01-02T03:04:05+00:00"}[meta["data_type"]]
    row["tbn_plus_subscription"] = "basic"
    src, records = ingest(db, tmp_path, ",".join(row) + "\n" + ",".join(row.values()) + "\n")
    resolve_bulk(db)
    pid = db.scalar(text("SELECT profile_id FROM source_records WHERE source_id=:s"), {"s": src.id})
    actual = canonical_attributes(db, pid)
    assert set(actual) == set(FIELDS)
    assert actual["rfm_raw_frequency"] == 12.5
    assert actual["rfm_date"] == "2026-01-02"
    assert actual["mobile_mktg_optin_tbn"] is False
    assert actual["last_seen"] == "2026-01-02T03:04:05+00:00"
    db.execute(text("INSERT INTO profile_traits(profile_id,rfm_score) VALUES(:p,999) ON CONFLICT(profile_id) DO UPDATE SET rfm_score=999"), {"p": pid})
    assert canonical_attributes(db, pid)["rfm_rfm_score"] == 12.5
    assert canonical_attributes_many(db, [pid, 999999])[999999] == {}
    for field, operator, value in (
        ("lifetime_amount", "eq", 12.5), ("rfm_date", "between", ["2026-01-02", "2026-01-02"]),
        ("has_active_email", "is_false", None), ("tbn_plus_subscription", "eq", "basic"),
        ("last_seen", "in_last_days", 2), ("ncoa_return_code", "eq", "evidence"),
    ):
        assert ids(db, {"type": "attribute", "field": field, "operator": operator, "value": value}) == [pid]
    db.execute(text("UPDATE sources SET priority=1 WHERE id=:s"), {"s": src.id})
    low = source(db)
    db.execute(text("UPDATE sources SET priority=99 WHERE id=:s"), {"s": low})
    record(db, low, "lower", "audit@example.org", lifetime_amount=900, carrier="lower")
    resolve_bulk(db)
    assert canonical_attributes(db, pid)["lifetime_amount"] == 12.5
    assert ids(db, {"type": "attribute", "field": "lifetime_amount", "operator": "eq", "value": 12.5}) == [pid]


def test_resources_real_import_references_join_and_no_fallback(db):
    sid = source(db)
    rid = record(db, sid, "a", "resources@example.org")
    resolve_bulk(db)
    pid = db.scalar(text("SELECT profile_id FROM source_records WHERE id=:id"), {"id": rid})
    admin = SimpleNamespace(role="admin")
    for resource, row in (("source_codes", {"key": "T4XWDGT", "name": "Giving Tuesday"}),
                          ("vod_titles", {"key": "v1", "name": "Episode 1", "series": "Series X"}),
                          ("products", {"key": "p1", "name": "Book"})):
        assert import_resources(resource, ResourceBatch(rows=[row]), admin, db)["accepted"] == 1
    db.execute(text("""INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,attributes)
        VALUES(:s,'g',:p,25,'2026-01-02','{"source_code":"T4XWDGT"}')"""), {"s": sid, "p": pid})
    db.execute(text("""INSERT INTO events(source_id,profile_id,type,name,occurred_at,properties)
        VALUES(:s,:p,'track','vod_viewed','2026-01-02','{"vod_title_id":"v1"}')"""), {"s": sid, "p": pid})
    assert ids(db, {"type": "gift", "aggregate": "exists", "operator": "is_true",
                   "where": {"source_codes.key": {"operator": "in", "value": ["T4XWDGT"]}}}) == [pid]
    assert ids(db, {"type": "event", "name": "vod_viewed", "aggregate": "count",
                   "operator": "eq", "value": 1,
                   "where": {"vod_titles.series": {"operator": "eq", "value": "Series X"}}}) == [pid]
    assert canonical_attributes(db, pid) == {}
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(text("""INSERT INTO events(source_id,profile_id,type,name,occurred_at,properties)
                VALUES(:s,:p,'track','vod_viewed','2026-01-02','{"vod_title_id":"missing"}')"""), {"s": sid, "p": pid})


def test_alias_conflict_and_sensitive_guard(db):
    sid = source(db)
    rid = record(db, sid, "legacy", "legacy@example.org", lastdonationdate="2026-01-02")
    assert db.scalar(text("SELECT attributes->>'last_donation_date' FROM source_records WHERE id=:r"), {"r": rid}) == "2026-01-02"
    with pytest.raises(DBAPIError, match="Conflicting canonical aliases"):
        with db.begin_nested():
            record(db, sid, "conflict", lastDonationDate="2026-01-01", last_donation_date="2026-01-02")
    resolve_bulk(db)
    pid = db.scalar(text("SELECT profile_id FROM source_records WHERE id=:r"), {"r": rid})
    for key in ("religion", "political_party", "military_veteran"):
        _register_enrichments(db, sid, [{"profile_id": pid, "values": {
            "enrichments": {key: ("text", "licensed")},
            "vendor": "Vendor", "license_id": "L1", "append_date": date(2026, 1, 2), "sensitive": False}}])
    rows = db.execute(text("SELECT vendor,license_id,append_date,sensitive FROM enrichment_values")).all()
    assert rows == [("Vendor", "L1", date(2026, 1, 2), True)] * 3


def test_all_twelve_events_copy_import_real_references(db):
    sid = source(db)
    import_resources("source_codes", ResourceBatch(rows=[{"key": "T4XWDGT"}]),
                     SimpleNamespace(role="admin"), db)
    records = []
    for name in EVENT_NAMES:
        row = {"external_id": name, "name": name, "occurred_at": "2026-01-02T03:04:05Z",
               "amount": "25", "fund": "Mission", "source_code": "T4XWDGT"}
        converted = map_and_validate_row(row, suggest_mapping(list(row), "event"), "event")
        assert converted["errors"] == []
        records.append({"record_type": "event", "event_message_id": name, "values": converted["values"]})
    _insert_events(db, sid, records)
    actual = db.execute(text("SELECT name,source_code,properties FROM events ORDER BY name")).all()
    assert {r.name for r in actual} == set(EVENT_NAMES)
    assert len(actual) == 12
    assert all(r.source_code == "T4XWDGT" for r in actual)
    donation = next(r for r in actual if r.name == "donation")
    assert donation.properties["amount"] == 25
    assert donation.properties["fund"] == "Mission"
