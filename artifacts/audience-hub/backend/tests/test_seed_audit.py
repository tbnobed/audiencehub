import csv
import json
from datetime import date, datetime, timezone

import pytest

from app.canonical import EVENT_NAMES
from app.seed import generate_seed
from app.seed_audit_report import measure_audit


@pytest.fixture(scope="module")
def audit(tmp_path_factory):
    directory = tmp_path_factory.mktemp("audit-50k")
    result = generate_seed(50_000, output_dir=directory)
    return result, measure_audit(directory)


def test_all_audit_proportions_from_independent_csv_sql(audit):
    result, report = audit
    assert report["as_of"] == str(datetime.now(timezone.utc).date())
    assert report["profiles"] == 50_000
    assert report["passed"], [r for r in report["metrics"] if not r["passed"]]
    assert set(report["event_names"]) == set(EVENT_NAMES)
    assert 40 <= report["gift_median"] <= 60
    assert report["gift_maximum"] > 1000
    assert report["invalid_enrichment"] == 0
    assert report["orphan_rows"] == report["ledger_mismatches"] == report["duplicate_gifts"] == report["date_violations"] == 0
    assert any(r["actual"] > 0 for r in report["metrics"] if r["metric"] == "30 day donation rows")


def test_audit_constants_match_user_catalog():
    from pathlib import Path
    from app.seed_audit import TARGETS, DENOMINATOR
    catalog = json.loads((Path(__file__).resolve().parents[2]/"docs/ZETA_AUDIENCE_TARGETS.json").read_text())
    assert TARGETS == catalog["audiences"]
    assert DENOMINATOR == catalog["denominator"]


def test_real_import_mapping_accepts_audit_csv(audit):
    from itertools import islice
    from app.importer import _mapping, SEED_SOURCES
    from app.imports.validation import map_and_validate_row
    result, _ = audit
    for filename, path in result["files"].items():
        with path.open() as handle:
            reader = csv.DictReader(handle)
            kind = SEED_SOURCES[filename][2]
            mapping = _mapping(reader.fieldnames, kind)
            for row in islice(reader, 500):
                validated = map_and_validate_row(row, mapping["columns"], kind, mapping["options"])
                assert not validated["errors"], (filename, validated["errors"])


def test_repeatability_and_dates(tmp_path):
    first = generate_seed(1000, output_dir=tmp_path/"one", as_of=date(2026, 10, 17))
    second = generate_seed(1000, output_dir=tmp_path/"two", as_of=date(2026, 10, 17))
    for name in first["files"]:
        assert first["files"][name].read_bytes() == second["files"][name].read_bytes()
    with first["files"]["giving_platform_gifts.csv"].open() as stream:
        assert all("2021-10-17" <= row["gift_date"] < "2026-10-17" for row in csv.DictReader(stream))


def test_audit_is_default_and_legacy_explicit(tmp_path):
    result = generate_seed(3, output_dir=tmp_path)
    assert result["mode"] == "audit"
    with pytest.raises(ValueError, match="mode"):
        generate_seed(3, output_dir=tmp_path, mode="invalid")


def test_fresh_seed_deduplicates_canonical_identity_headers(tmp_path, monkeypatch):
    from app.canonical import FIELDS
    from app.importer import _mapping
    from app.imports.validation import map_and_validate_row

    # Cover current overlap and future additions without depending on which
    # identity properties happen to be canonical in this catalog version.
    for key in ("donor_account_number", "zeta_cdp_id", "email_sha256"):
        monkeypatch.setitem(FIELDS, key, {"data_type": "text", "group": "Identity", "sensitive": False})
    result = generate_seed(1000, output_dir=tmp_path, as_of=date(2026, 10, 17))
    for path in result["files"].values():
        with path.open(newline="") as handle:
            rows = csv.reader(handle)
            headers = next(rows)
            assert len(headers) == len(set(headers))
            assert all(len(row) == len(headers) for row in rows)
    with result["files"]["donor_crm_contacts.csv"].open(newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames
        first = next(reader)
        assert headers.count("donor_account_number") == 1
        assert first["donor_account_number"] == "donor-0"
        assert first["zeta_cdp_id"] == "cdp-0"
        assert len(first["email_sha256"]) == 64
        mapped = _mapping(headers, "contact")
        validated = map_and_validate_row(first, mapped["columns"], "contact", mapped["options"])
        assert not validated["errors"]
    # Exercise real fresh CSV -> SQL schema creation, the failing benchmark path.
    report = measure_audit(tmp_path)
    assert report["profiles"] == 1000
    assert next(m["actual"] for m in report["metrics"] if m["metric"] == "Donor accounts") == 302
