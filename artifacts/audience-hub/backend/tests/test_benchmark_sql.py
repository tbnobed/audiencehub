"""Real PostgreSQL target smoke; enabled only by isolated benchmark test runner."""
import os
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest


@pytest.mark.skipif(os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
                    reason="requires benchmark-owned disposable PostgreSQL")
def test_all_bulk_sql_targets_insert_and_conflict():
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.imports.service import (
        _upsert_source_records, _insert_gifts, _insert_events,
        _register_enrichments, _upsert_consents,
    )

    with Session(engine) as db:
        source = db.execute(text("""
            INSERT INTO sources (key,name,kind,record_types,priority,is_active)
            VALUES ('sql_smoke','SQL smoke','csv',
                    ARRAY['contact','gift','event','enrichment','consent'],10,true)
            RETURNING id
        """)).scalar_one()
        profile = db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
        record = dict(external_id="smoke-contact", attributes={"test": True},
                      raw_hash="one", email_norm="smoke@example.org",
                      phone_e164="+12025550123", address1="1 Main", address2="Unit 2")
        ids = _upsert_source_records(db, source, None, [record])
        source_record = ids["smoke-contact"]
        now = datetime(2025, 1, 15, tzinfo=timezone.utc)
        gift = dict(record_type="gift", gift_external_id="smoke-gift",
                    source_record_id=source_record,
                    values={"amount": Decimal("25.50"), "gift_date": date(2025, 1, 15)})
        event = dict(record_type="event", event_message_id="smoke-event",
                     values={"occurred_at": now, "name": "Smoke", "properties": {"test": True}})
        enrichment = dict(profile_id=profile, values={"enrichments": {
            "smoke_number": ("number", Decimal("1.25")),
            "smoke_bool": ("boolean", True),
            "smoke_date": ("date", date(2025, 1, 15)),
            "smoke_text": ("text", "hello"),
            "smoke_enum": ("enum", "high"),
        }})
        consent = dict(record_type="consent", profile_id=profile,
                       source_record_id=source_record,
                       values={"channel": "email", "status": "opted_in", "captured_at": now})
        for _ in range(2):
            _upsert_source_records(db, source, None, [record])
            _insert_gifts(db, source, [gift])
            _insert_events(db, source, [event])
            _register_enrichments(db, source, [enrichment])
            _upsert_consents(db, source, [consent])
        consent["values"]["status"] = "opted_out"
        _upsert_consents(db, source, [consent])
        for table, count in (("source_records", 1), ("gifts", 1), ("events", 1),
                             ("enrichment_attributes", 5), ("enrichment_values", 5),
                             ("consents", 1)):
            assert db.execute(text(f"SELECT count(*) FROM {table} WHERE source_id=:id"),
                              {"id": source}).scalar_one() == count
        assert db.execute(text("SELECT amount FROM gifts WHERE source_id=:id"),
                          {"id": source}).scalar_one() == Decimal("25.50")
        assert db.execute(text("SELECT status FROM consents WHERE source_id=:id"),
                          {"id": source}).scalar_one() == "opted_out"
        actual = db.execute(text(
            "SELECT phone_e164,address1,address2 FROM source_records WHERE id=:id"
        ), {"id": source_record}).one()
        assert tuple(actual) == ("+12025550123", "1 Main", "Unit 2")
        # This test never commits, even in its already-disposable database.
        db.rollback()