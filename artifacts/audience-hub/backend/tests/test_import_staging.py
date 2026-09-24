import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.imports.service import (
    CSV_BATCH_SIZE, _json_value, _restore_converted, _upsert_consents,
    _register_enrichments, _insert_events, _row_payload, _insert_gifts, _upsert_source_records,
)
from app.imports.staging import copy_upsert
from app.imports.validation import map_and_validate_row
from test_imports import _ExecuteRecorder


def test_copy_upsert_has_constant_round_trips_and_isolated_unlogged_tables():
    db = _ExecuteRecorder()
    statement = """INSERT INTO example (value)
        SELECT :value FROM {stage} WHERE true
        ON CONFLICT (value) DO UPDATE SET value=EXCLUDED.value"""
    rows = [{"value": str(n)} for n in range(CSV_BATCH_SIZE)]
    copy_upsert(db, statement, rows, "value text")
    assert len(db.calls) == 3  # CREATE, one INSERT SELECT, DROP
    assert len(db.copies) == 1
    assert db.params == rows
    assert "CREATE UNLOGGED TABLE" in db.calls[0]
    assert 'SELECT staged."value"' in db.statement
    assert ":value" not in db.statement and "{stage}" not in db.statement
    copy_upsert(db, statement, rows[:1], "value text")
    assert db.copies[0] != db.copies[1]
    assert CSV_BATCH_SIZE == 10_000


def test_empty_batch_does_not_touch_database():
    db = _ExecuteRecorder()
    copy_upsert(db, "", [], "value text")
    assert db.calls == []


def test_copy_upsert_preserves_complete_identifiers_with_digits():
    db = _ExecuteRecorder()
    copy_upsert(db, """INSERT INTO example (phone_e164, address1, address2, Field3)
        SELECT :phone_e164, :address1, :address2, :Field3 FROM {stage} WHERE true""",
        [{"phone_e164": "+12025551234", "address1": "Main", "address2": "Unit", "Field3": "x"}],
        'phone_e164 text, address1 text, address2 text, "Field3" text')
    assert 'staged."phone_e164"' in db.statement
    assert 'staged."address1"' in db.statement
    assert 'staged."address2"' in db.statement
    assert 'staged."Field3"' in db.statement
    assert 'staged."phone_e"164' not in db.statement


def test_failed_upsert_leaves_cleanup_to_transaction_rollback():
    class FailingDb(_ExecuteRecorder):
        def execute(self, statement, params=None):
            super().execute(statement, params)
            if "INSERT INTO" in str(statement):
                raise RuntimeError("aborted transaction")
    db = FailingDb()
    with pytest.raises(RuntimeError, match="aborted"):
        copy_upsert(db, "INSERT INTO example SELECT :value FROM {stage}",
                    [{"value": "one"}], "value text")
    assert len(db.calls) == 2
    assert not any("DROP TABLE" in sql for sql in db.calls)


def test_validation_staging_round_trip_preserves_types_and_warnings():
    converted = {"values": {
        "amount": Decimal("123.45"), "gift_date": date(2025, 1, 2),
        "occurred_at": datetime(2025, 1, 2, tzinfo=timezone.utc),
        "captured_at": datetime(2025, 1, 3, tzinfo=timezone.utc),
        "enrichments": {
            "score": ("number", Decimal("0.73")),
            "birthday": ("date", date(2000, 1, 1)),
            "flag": ("boolean", True),
        },
        "attributes": {"nested": {"data": ["value"]}},
    }, "errors": [], "warnings": ["Date: date_ambiguous"]}
    staged = json.loads(json.dumps(converted, default=_json_value))
    assert _restore_converted(staged) == converted


def test_consents_collapse_newest_with_opt_out_winning_ties():
    db = _ExecuteRecorder()
    captured = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = [{"record_type": "consent", "profile_id": 42, "source_record_id": i,
             "values": {"channel": "email", "status": status, "captured_at": captured}}
            for i, status in enumerate(["opted_in", "opted_out", "opted_in"], 1)]
    _upsert_consents(db, 1, rows)
    assert len(db.params) == 1
    assert db.params[0]["status"] == "opted_out"
    assert db.params[0]["evidence"] == '{"source_record_id": 2}'
    assert "consents.captured_at < EXCLUDED.captured_at" in db.statement
    assert len(db.copies) == 1


def test_enrichment_registration_and_values_are_each_one_copy_and_upsert():
    db = _ExecuteRecorder()
    rows = [{"profile_id": 42, "values": {
        "enrichments": {"score": ("number", Decimal("0.73")), "flag": ("boolean", True)}
    }}]
    _register_enrichments(db, 1, rows)
    assert len(db.copies) == 2
    assert len([sql for sql in db.calls if "INSERT INTO" in sql]) == 2
    assert db.params[0]["value"] == "0.73"
    assert db.params[1]["value"] == "True"


def test_event_duplicates_are_collapsed_before_set_upsert():
    db = _ExecuteRecorder()
    timestamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = [{"record_type": "event", "event_message_id": "message",
             "values": {"occurred_at": timestamp, "name": name}}
            for name in ("first", "last")]
    _insert_events(db, 1, rows)
    assert len(db.params) == 1
    assert db.params[0]["name"] == "last"


def test_gifts_without_external_ids_use_distinct_stable_auto_ids():
    db = _ExecuteRecorder()
    rows = []
    for amount in ("10.00", "20.00"):
        raw = {"amount": amount, "gift_date": "2025-01-01"}
        converted = map_and_validate_row(
            raw, {"amount": "amount", "gift_date": "gift_date"}, "gift")
        assert not converted["errors"]
        values = converted["values"]
        payload = _row_payload("gift", values)
        assert payload["gift_external_id"] == "auto:" + values["raw_hash"]
        assert payload["gift_external_id"] == _row_payload("gift", values)["gift_external_id"]
        rows.append({**payload, "values": values, "record_type": "gift", "source_record_id": 42})
    _insert_gifts(db, 1, rows)
    assert len(db.params) == 2
    assert len({row["external_id"] for row in db.params}) == 2


def test_consent_missing_capture_time_is_rejected_before_writes():
    converted = map_and_validate_row(
        {"external_id": "contact", "channel": "email", "status": "opted_in"},
        {"external_id": "external_id", "channel": "channel", "status": "status"},
        "consent",
    )
    assert any("captured_at" in error for error in converted["errors"])
    # Bounce-derived consent metadata belongs to a contact, not a direct
    # consent row; the direct consent upsert must ignore it.
    db = _ExecuteRecorder()
    _upsert_consents(db, 1, [{
        "record_type": "contact", "profile_id": 42,
        "values": {"consent": {"channel": "email", "status": "opted_out", "captured_at": None}},
    }])
    assert db.calls == []


def test_source_returning_avoids_fresh_import_lookups(monkeypatch):
    def upsert(db, statement, rows, columns, *, returning=False):
        assert returning
        assert "RETURNING external_id, id, profile_id" in statement
        assert "raw_hash IS DISTINCT FROM" in statement
        return [("new", 1, None), ("changed", 2, 42)]
    monkeypatch.setattr("app.imports.service.copy_upsert", upsert)
    db = _ExecuteRecorder()
    profiles = {}
    rows = [{"external_id": key, "attributes": {}} for key in ("new", "changed")]
    assert _upsert_source_records(db, 1, 1, rows, profiles) == {"new": 1, "changed": 2}
    assert profiles == {1: None, 2: 42}
    assert db.calls == []


def test_source_unchanged_rows_still_resolve_existing_ids_and_profiles(monkeypatch):
    def upsert(db, statement, rows, columns, *, returning=False):
        assert returning
        if "INSERT INTO" in statement:
            return [("new", 1, None)]
        assert rows == [{"source_id": 7, "external_id": "unchanged"}]
        assert "JOIN source_records AS sr" in statement
        assert "ANY" not in statement
        assert "sr.profile_id" in statement
        assert "sr.source_id = :source_id" in statement
        assert "sr.external_id = :external_id" in statement
        return [("unchanged", 2, 42)]
    monkeypatch.setattr("app.imports.service.copy_upsert", upsert)

    profiles = {}
    rows = [{"external_id": key, "attributes": {}} for key in ("new", "unchanged")]
    assert _upsert_source_records(_ExecuteRecorder(), 7, 1, rows, profiles) == {"new": 1, "unchanged": 2}
    assert profiles == {1: None, 2: 42}


def test_statistics_analyze_all_touched_targets_at_import_50k_and_at_end():
    from app.imports.staging import ImportStatistics

    db = _ExecuteRecorder()
    statistics = ImportStatistics()
    for _ in range(4):
        statistics.record(db, "source_records", 10_000)
    statistics.record(db, "gifts", 3)
    statistics.record(db, "consents", 1)
    statistics.advance(db, 49_999)
    assert db.calls == []
    statistics.record(db, "source_records", 10_000)
    statistics.advance(db, 50_000)
    assert db.calls == ["ANALYZE consents", "ANALYZE gifts", "ANALYZE source_records"]
    statistics.record(db, "source_records", 60_000)
    statistics.record(db, "gifts", 1)
    statistics.advance(db, 110_000)
    # A newly touched target is refreshed immediately after its first write
    # once the import-wide milestone has passed, even with few modified rows.
    statistics.record(db, "suppressions", 5)
    statistics.record(db, "suppressions", 2)
    assert db.calls == [
        "ANALYZE consents", "ANALYZE gifts", "ANALYZE source_records",
        "ANALYZE suppressions",
    ]
    statistics.finish(db)
    assert db.calls[4:] == [
        "ANALYZE consents", "ANALYZE gifts", "ANALYZE source_records",
        "ANALYZE suppressions",
    ]


def test_statistics_hook_counts_actual_writes_and_not_selects():
    from types import SimpleNamespace
    from app.imports.staging import ImportStatistics, import_statistics

    class Db(_ExecuteRecorder):
        def execute(self, statement, params=None):
            super().execute(statement, params)
            return SimpleNamespace(rowcount=3, all=lambda: [])

    db = Db()
    stats = ImportStatistics()
    token = import_statistics.set(stats)
    try:
        copy_upsert(db, "INSERT INTO suppressions SELECT :value FROM {stage}",
                    [{"value": n} for n in range(10)], "value text")
        copy_upsert(db, "SELECT :value FROM {stage}",
                    [{"value": 1}], "value text", returning=True)
    finally:
        import_statistics.reset(token)
    assert stats.written == {"suppressions": 3}
    stats.finish(db)
    assert db.calls[-1] == "ANALYZE suppressions"


@pytest.mark.parametrize(("number", "expected"), [
    (0, "0"), (1, "1"), (10_000, "10k"), (50_000, "50k"),
    (1_000_000, "1M"), (1_500_000, "1.5M"),
])
def test_import_progress_compact_numbers(number, expected):
    from app.imports.service import _compact_number
    assert _compact_number(number) == expected


def test_validation_and_writing_progress_are_explicit():
    from app.imports.service import _report_progress
    updates = []
    _report_progress(updates.append, "Validating", 10_000, 250_000)
    _report_progress(updates.append, "Writing", 10_000, 250_000)
    assert [update["message"] for update in updates] == [
        "Validating 10k / 250k", "Writing 10k / 250k",
    ]
    assert [update["phase"] for update in updates] == ["validating", "writing"]