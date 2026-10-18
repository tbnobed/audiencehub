"""Code-only regressions; execute against the production verification environment."""
import io
import json
from unittest.mock import MagicMock

import pytest

from app.source_connectors import SourceError, snowflake_rows, validate_config, window
from app.source_rows import mapped_row, identity_aliases, matched_columns
from app.zeta_files import detect_type, records
from app.zeta_migrations import catalog, validate_counts


@pytest.mark.parametrize("feed", ["people", "models", "lookups"])
def test_exit_contact_rows_cannot_invent_an_identity_from_arbitrary_id(feed):
    with pytest.raises(SourceError, match="require"):
        mapped_row({"id": "an-unrelated-source-record", "first_name": "Alice"}, {"feed": feed}, require_zeta=True)


def test_people_and_lookup_evidence_do_not_overwrite_each_other():
    original = {"zeta_cdp_id": "cdp-one", "zeta_uid": "UID-ONE", "first_name": "Alice", "UnmappedFlag": "yes"}
    people = mapped_row(original, {"feed": "people"}, require_zeta=True)
    lookup = mapped_row(original, {"feed": "lookups"}, require_zeta=True)
    model = mapped_row(original, {"feed": "models"}, require_zeta=True)
    assert len({people["external_id"], lookup["external_id"], model["external_id"]}) == 3
    assert people["UnmappedFlag"] == "yes"
    assert [r["external_id"] for r in identity_aliases(people, "people")] == ["cdp-one", "uid-one"]
    second = mapped_row({**original, "hubspot_record_id": "456"}, {"feed": "lookups"}, require_zeta=True)
    assert lookup["external_id"] != second["external_id"]


def test_nested_zeta_event_fields_and_unknown_properties_survive():
    incoming = {"message_id": "evt-one", "timestamp": "2026-09-01T12:00:00Z",
                "properties": {"user_id": "UID-ONE", "amount": 45, "raw_vendor_field": "retained"}}
    row = mapped_row(incoming, {"feed": "events", "target_type": "donation"}, require_zeta=True)
    assert row["name"] == "donation" and row["contact_external_id"] == "uid-one"
    assert row["properties"]["raw_vendor_field"] == "retained"
    assert row["occurred_at"] == incoming["timestamp"]


def test_column_mapping_merges_properties_and_preserves_blank_unknowns():
    mapping = {"feed": "events", "target_type": "donation",
               "columns": {"GiftDollars": "properties.amount", "KeepMe": ""}}
    row = mapped_row({"GiftDollars": 50, "properties": '{"source_code":"A"}', "KeepMe": "raw"}, mapping)
    assert row["properties"] == {"amount": 50, "source_code": "A"}
    assert row["KeepMe"] == "raw"


@pytest.mark.parametrize(("name", "first", "expected"), [
    ("exit.csv", {"Event Name": "donation_return", "user_id": "u"}, "donation_return"),
    ("products.csv", {"id": "1", "name": "A"}, "products"),
    ("exit.jsonl", {"user_id": "u", "properties": {"wealth_score": 5}}, "model_scores"),
    ("okta_lookup.csv", {"zeta_uid": "u", "okta_customer_id": "k"}, "lookups"),
])
def test_exit_type_detection(name, first, expected):
    assert detect_type(name, first) == expected


def test_jsonl_is_streamed_past_copy_chunk_size(tmp_path, monkeypatch):
    from app import source_connectors
    monkeypatch.setattr(source_connectors, "MAX_LINES", 2)
    path = tmp_path / "people.jsonl"
    path.write_text('\ufeff{"zeta_uid":"a"}\n{"zeta_uid":"b"}\n{"zeta_uid":"c"}\n')
    assert [r["zeta_uid"] for r in records(path)] == ["a", "b", "c"]


def test_parity_requires_real_counts_for_all_twenty_three_audiences():
    counts = catalog()
    assert len(counts) == 23 and validate_counts(counts) == counts
    with pytest.raises(SourceError):
        validate_counts({name: value for name, value in counts.items() if name != "All TBN Reference"})
    with pytest.raises(SourceError):
        validate_counts({**counts, "All TBN Reference": True})


def test_snowflake_result_batches_are_not_fetchall():
    result = MagicMock()
    result.__enter__.return_value = result
    result.description = [("ZETA_UID",)]
    batch = MagicMock()
    batch.create_iter.return_value = iter([("u1",), ("u2",)])
    result.get_result_batches.return_value = [batch]
    connection = MagicMock()
    connection.cursor.return_value = result
    assert list(snowflake_rows(connection, {"table": "DB.SCHEMA.PEOPLE"})) == [
        {"zeta_uid": "u1"}, {"zeta_uid": "u2"}]
    result.fetchall.assert_not_called()
    result.fetchmany.assert_not_called()


def test_mapping_schedule_and_empty_connection_configuration():
    validate_config("snowflake", {"mappings": [], "schedules": {}})
    validate_config("snowflake", {"mappings": [{"id": "p", "feed": "people", "target_type": "people",
        "table": "DB.SCHEMA.PEOPLE", "columns": {"CUSTOM_UID": "zeta_uid"},
        "schedule": {"frequency": "daily", "hour": 3, "minute": 15, "timezone": "America/New_York"}}],
        "schedules": {}})
    assert matched_columns(["user_id", "Zeta CDP ID"])["user_id"] == "zeta_uid"
