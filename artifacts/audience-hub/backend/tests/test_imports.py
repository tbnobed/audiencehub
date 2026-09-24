from decimal import Decimal

from app.importer import _mapping
from app.imports.mapping import suggest_mapping, validate_mapping
from app.imports.service import (
    _external_id,
    _identity_hash,
    _identifiers_blocked_batch,
    _insert_events,
    _is_hard_bounce,
    _register_hard_bounce_suppressions,
    _row_payload,
)
from app.imports.validation import map_and_validate_row, normalize_email


def test_header_auto_suggest_and_mapping_validation():
    headers = ["Email Address", "Gift Amt", "ZIP Code", "Unrelated"]
    suggested = suggest_mapping(headers, "gift")
    assert suggested == {"Email Address": "email", "Gift Amt": "amount"}
    assert validate_mapping(suggested, "gift", headers) == suggested


def test_validation_reports_required_and_bad_date_by_record_type():
    result = map_and_validate_row(
        {"Amount": "not money", "Date": "not a date"},
        {"Amount": "amount", "Date": "gift_date"},
        "gift",
    )
    assert len(result["errors"]) == 4
    assert any("Amount" in error for error in result["errors"])
    assert any("gift_date" in error for error in result["errors"])


def test_non_iso_gift_date_is_coerced_and_reported_as_warning():
    result = map_and_validate_row(
        {"Amount": "25.00", "Date": "12/25/2024"},
        {"Amount": "amount", "Date": "gift_date"},
        "gift",
    )
    assert result["errors"] == []
    assert result["values"]["gift_date"].isoformat() == "2024-12-25"
    assert result["warnings"] == ["Date: date format coerced to ISO"]


def test_enrichment_requires_declared_typed_attribute_and_identifier():
    mapping = validate_mapping(
        {
            "Email Address": "email",
            "HH Income": {"enrichment": "hh_income_band", "data_type": "enum"},
            "Score": {"enrichment": "donor_propensity_score", "data_type": "number"},
        },
        "enrichment",
        ["Email Address", "HH Income", "Score"],
    )
    result = map_and_validate_row(
        {"Email Address": "donor@example.org", "HH Income": "high", "Score": "0.73"},
        mapping,
        "enrichment",
    )
    assert result["errors"] == []
    assert result["values"]["enrichments"] == {
        "hh_income_band": ("enum", "high"),
        "donor_propensity_score": ("number", Decimal("0.73")),
    }


def test_seed_gifts_and_enrichment_use_explicit_donor_crm_reference():
    gift_headers = ["external_id", "contact_external_id", "email", "phone", "amount", "gift_date"]
    gift_mapping = _mapping(gift_headers, "gift")
    assert gift_mapping["options"] == {"reference_source": "donor_crm"}
    assert gift_mapping["columns"]["contact_external_id"] == "contact_external_id"
    gift_values = map_and_validate_row({
        "external_id": "gift-1", "contact_external_id": "crm-1",
        "email": "", "phone": "", "amount": "25.00", "gift_date": "2025-01-01",
    }, gift_mapping["columns"], "gift", gift_mapping["options"])["values"]
    gift_payload = _row_payload("gift", gift_values, gift_mapping["options"]["reference_source"])
    assert gift_payload["external_id"] == "gift-1"
    assert gift_payload["attributes"]["_identity_reference"] == {
        "contact_external_id": "crm-1", "source_key": "donor_crm",
    }

    enrichment_headers = [
        "external_id", "contact_external_id", "hh_income_band", "donor_propensity_score",
    ]
    enrichment_mapping = _mapping(enrichment_headers, "enrichment")
    assert enrichment_mapping["options"] == {"reference_source": "donor_crm"}
    assert enrichment_mapping["columns"]["contact_external_id"] == "contact_external_id"
    enrichment_values = map_and_validate_row({
        "external_id": "zeta-1", "contact_external_id": "crm-1",
        "hh_income_band": "75k_149k", "donor_propensity_score": "0.75",
    }, enrichment_mapping["columns"], "enrichment", enrichment_mapping["options"])["values"]
    enrichment_payload = _row_payload(
        "enrichment", enrichment_values, enrichment_mapping["options"]["reference_source"]
    )
    assert enrichment_payload["external_id"] == "zeta-1"
    assert enrichment_payload["attributes"]["_identity_reference"] == {
        "contact_external_id": "crm-1", "source_key": "donor_crm",
    }


def test_stable_row_hash_generates_deterministic_natural_keys_for_reimport():
    row = {"external_id": "crm-42", "raw_hash": "f" * 64}
    assert _external_id("contact", row) == "crm-42"
    assert _external_id("gift", {"raw_hash": "a" * 64}) == _external_id(
        "gift", {"raw_hash": "a" * 64}
    )
    assert _external_id("event", {"raw_hash": "a" * 64}) == "auto:" + "a" * 64


def test_gmail_email_normalization_collapses_dots_and_plus_tags():
    assert normalize_email(" First.Last+monthly@Gmail.com ") == "firstlast@gmail.com"
    assert normalize_email(" First.Last+monthly@GMAIL.TEST ") == "firstlast@gmail.test"
    assert normalize_email("first.last@other.test") is None


def test_five9_event_fields_and_json_are_preserved():
    headers = ["external_id", "message_id", "type", "name", "occurred_at",
               "received_at", "properties", "context"]
    suggested = suggest_mapping(headers, "event")
    assert {key: suggested[key] for key in (
        "external_id", "message_id", "type", "name", "occurred_at", "received_at", "context",
        "properties"
    )} == {
        "external_id": "external_id",
        "message_id": "message_id",
        "type": "type",
        "name": "name",
        "occurred_at": "occurred_at",
        "received_at": "received_at",
        "context": "context",
        "properties": "properties",
    }
    mapping = {
        "external_id": "external_id",
        "message_id": "message_id",
        "type": "type",
        "name": "name",
        "occurred_at": "occurred_at",
        "received_at": "received_at",
        "properties": "properties",
        "context": "context",
    }
    validated_mapping = validate_mapping(mapping, "event", headers)
    result = map_and_validate_row({
        "external_id": "call-42",
        "message_id": "message-42",
        "type": "identify",
        "name": "Inbound Call",
        "occurred_at": "2025-01-02T03:04:05Z",
        "received_at": "2025-01-02T03:05:05Z",
        "properties": '{"duration_seconds":42,"direction":"inbound"}',
        "context": '{"channel":"phone","vendor":"five9"}',
    }, validated_mapping, "event")

    assert result["errors"] == []
    values = result["values"]
    assert values["message_id"] == "message-42"
    assert values["type"] == "identify"
    assert values["received_at"].isoformat() == "2025-01-02T03:05:05+00:00"
    assert values["properties"] == {"duration_seconds": 42, "direction": "inbound"}
    assert values["context"] == {"channel": "phone", "vendor": "five9"}
    payload = _row_payload("event", values)
    assert payload["external_id"] == "call-42"
    assert payload["event_message_id"] == "message-42"


class _ExecuteRecorder:
    def __init__(self):
        self.statement = ""
        self.params = None

    def execute(self, statement, params):
        self.statement = str(statement)
        self.params = params


def test_event_upsert_includes_mapped_fields_and_remains_idempotent():
    db = _ExecuteRecorder()
    values = {
        "type": "track",
        "name": "Inbound Call",
        "occurred_at": map_and_validate_row(
            {"name": "Inbound Call", "occurred_at": "2025-01-02T03:04:05Z"},
            {"name": "name", "occurred_at": "occurred_at"}, "event"
        )["values"]["occurred_at"],
        "received_at": map_and_validate_row(
            {"received_at": "2025-01-02T03:05:05Z"},
            {"received_at": "received_at"}, "event"
        )["values"]["received_at"],
        "properties": {"duration_seconds": 42},
        "context": {"vendor": "five9"},
    }
    _insert_events(db, 9, [{
        "record_type": "event",
        "event_message_id": "message-42",
        "source_record_id": 15,
        "values": values,
    }])

    params = db.params[0]
    assert params["message_id"] == "message-42"
    assert params["type"] == "track"
    assert params["properties"] == '{"duration_seconds": 42}'
    assert params["context"] == '{"vendor": "five9"}'
    assert "received_at" in db.statement
    assert "ON CONFLICT (source_id, message_id, occurred_at)" in db.statement
    assert "IS DISTINCT FROM" in db.statement


def test_hard_bounce_registers_email_hmac_and_has_privacy_safe_detection():
    pepper = "test-pepper"
    email = normalize_email("Bounced.User+esp@example.org")
    assert email == "bounced.user@example.org"
    assert _is_hard_bounce({"attributes": {"hard_bounce": " true "}})
    assert not _is_hard_bounce({"attributes": {"hard_bounce": "false"}})

    db = _ExecuteRecorder()
    _register_hard_bounce_suppressions(db, {_identity_hash(pepper, email)})
    assert "'hard_bounce'" in db.statement
    assert "ON CONFLICT (type, value_hash)" in db.statement
    assert db.params == [{"value_hash": _identity_hash(pepper, email)}]
    assert email not in str(db.params)

    contact = map_and_validate_row(
        {"external_id": "esp-1", "email": "Bounced.User+esp@example.org",
         "hard_bounce": "true"},
        {"external_id": "external_id", "email": "email",
         "hard_bounce": "attributes.hard_bounce"},
        "contact",
    )
    assert contact["errors"] == []
    assert _is_hard_bounce(contact["values"])


class _BatchLookupDb:
    def __init__(self, suppression_hashes, emails, phones):
        self.suppression_hashes = set(suppression_hashes)
        self.emails = {value.casefold() for value in emails}
        self.phones = set(phones)
        self.calls = []

    def execute(self, statement, params):
        statement = str(statement)
        self.calls.append(statement)
        if "FROM suppressions" in statement:
            rows = [
                {"type": kind, "value_hash": digest}
                for kind, hashes in (
                    ("email", params["email_hashes"]),
                    ("phone", params["phone_hashes"]),
                )
                for digest in hashes if digest in self.suppression_hashes
            ]
        else:
            rows = []
            for email in self.emails:
                if email in params["emails"] or email in {"*@test.com", "noemail@*"}:
                    rows.append({"type": "email", "value": email})
            rows.extend({"type": "phone", "value": phone}
                        for phone in self.phones if phone in params["phones"])

        class Result:
            def mappings(self):
                return self

            def __iter__(self):
                return iter(rows)

        return Result()


def test_identifier_checks_batch_queries_suppressions_and_preserve_blocklist_rules():
    pepper = "test-pepper"
    bounced_email = "bounced@example.org"
    bounced_phone = "+12147483647"
    identities = {
        (bounced_email, None),
        (None, bounced_phone),
        ("block@example.org", None),
        ("donor@test.com", None),
        ("noemail@example.org", None),
        (None, "+15555555555"),
        (None, "+11111111111"),
        ("good@example.org", "+12145551234"),
    }
    identities.update(
        (f"person-{index}@example.org", f"+120255{index:06d}")
        for index in range(1000)
    )
    db = _BatchLookupDb(
        {_identity_hash(pepper, bounced_email), _identity_hash(pepper, bounced_phone)},
        {"block@example.org", "*@test.com", "noemail@*"},
        {"+15555555555"},
    )

    result = _identifiers_blocked_batch(db, identities, pepper)

    assert result[(bounced_email, None)] == (True, False)
    assert result[(None, bounced_phone)] == (True, False)
    assert result[("block@example.org", None)] == (False, True)
    assert result[("donor@test.com", None)] == (False, True)
    assert result[("noemail@example.org", None)] == (False, True)
    assert result[(None, "+15555555555")] == (False, True)
    assert result[(None, "+11111111111")] == (False, True)
    assert result[("good@example.org", "+12145551234")] == (False, False)
    assert len(result) == len(identities)
    assert len(db.calls) == 2