"""Audited canonical contract: no production database or invented fallback values."""
import pytest
from app.canonical import FIELDS, EVENT_NAMES, normalize_attributes
from app.imports.mapping import suggest_mapping
from app.imports.validation import map_and_validate_row


def test_every_catalog_field_maps_and_converts():
    row = {"external_id": "audit"}
    for key, meta in FIELDS.items():
        row[key] = {"text": "evidence", "number": "12.5", "boolean": "false",
                    "date": "2026-01-02", "timestamp": "2026-01-02T03:04:05+00:00"}[meta["data_type"]]
    row["tbn_plus_subscription"] = "premium_annual"
    result = map_and_validate_row(row, suggest_mapping(list(row), "contact"), "contact")
    assert result["errors"] == []
    stored = result["values"]["attributes"]
    assert len(stored) == len(FIELDS) - 6
    assert stored["rfm_raw_recency"] == 12.5
    assert stored["mobile_mktg_optin_tbn"] is False
    assert stored["rfm_date"] == "2026-01-02"
    assert stored["inmate"] is False and FIELDS["inmate"]["sensitive"]
    assert result["values"]["city"] == "evidence"
    assert len(EVENT_NAMES) == 12


def test_legacy_alias_duplicates_normalize_or_fail_explicitly():
    assert normalize_attributes({"lastdonationdate": "2026-01-02",
                                 "last_donation_date": "2026-01-02"}) == {"last_donation_date": "2026-01-02"}
    row = {"external_id": "a", "lastDonationDate": "2026-01-01", "last_donation_date": "2026-01-02"}
    result = map_and_validate_row(row, suggest_mapping(list(row), "contact"), "contact")
    assert any("Conflicting aliases" in error for error in result["errors"])
    with pytest.raises(ValueError, match="finite"):
        normalize_attributes({"wealth_score": "NaN"})


@pytest.mark.parametrize("field,date_field", [
    ("rfm_raw_frequency", "rfm_date"),
    ("donation_likelihood_prob", "donation_likelihood_model_date"),
    ("amt_suggested", "donation_ask_model_date"),
    ("wealth_score", "wealth_score_model_date"),
])
def test_models_require_preserved_dates(field, date_field):
    row = {"external_id": "a", field: "1"}
    result = map_and_validate_row(row, suggest_mapping(list(row), "contact"), "contact")
    assert any(date_field in error for error in result["errors"])


@pytest.mark.parametrize("event", EVENT_NAMES)
def test_all_audited_event_names_are_importable(event):
    row = {"external_id": event, "name": event, "occurred_at": "2026-01-02T03:04:05Z",
           "amount": "25", "fund": "Mission", "source_code": "T4XWDGT"}
    result = map_and_validate_row(row, suggest_mapping(list(row), "event"), "event")
    assert result["errors"] == []
    assert result["values"]["name"] == event
    assert result["values"]["properties"]["source_code"] == "T4XWDGT"


def test_enrichment_requires_license_evidence_and_is_separate():
    row = {"email": "audit@example.org", "religion": "test"}
    columns = {"email": "email", "religion": {"enrichment": "religion", "data_type": "text"}}
    result = map_and_validate_row(row, columns, "enrichment")
    assert len(result["errors"]) == 3
    row.update(vendor="Licensed provider", license_id="L1", append_date="2026-01-02")
    columns.update({key: key for key in ("vendor", "license_id", "append_date")})
    result = map_and_validate_row(row, columns, "enrichment")
    assert result["errors"] == []
    assert result["values"]["enrichments"]["religion"] == ("text", "test")
    assert "religion" not in result["values"].get("attributes", {})
