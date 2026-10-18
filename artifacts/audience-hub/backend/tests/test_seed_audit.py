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


@pytest.fixture(scope="module")
def giving_ledger(audit):
    from collections import defaultdict
    from decimal import Decimal
    from pathlib import Path
    result, _ = audit
    directory = Path(result["output_dir"])
    positive, refunds, by_person = {}, [], defaultdict(list)
    with (directory / "donation_events_gifts.csv").open() as handle:
        for row in csv.DictReader(handle):
            row["cents"] = int(Decimal(row["amount"]) * 100)
            if row["is_refund"] == "true":
                refunds.append(row)
            else:
                positive[row["external_id"]] = row
                by_person[row["contact_external_id"]].append(row)
    with (directory / "donor_direct.csv").open() as handle:
        people = {row["external_id"]: row for row in csv.DictReader(handle)}
    returned = {row["original_donation_id"] for row in refunds}
    retained = {key: [row for row in rows if row["external_id"] not in returned]
                for key, rows in by_person.items()}
    return result, people, positive, refunds, retained


def test_audit_giving_volume_frequency_tail_and_returns(giving_ledger):
    from statistics import median, quantiles
    result, people, positive, refunds, retained = giving_ledger
    assert len(positive) == round(result["requested_people"] * 345000 / 500000)
    assert len(refunds) == round(len(positive) * 27 / 2957)
    frequencies = [len(rows) for rows in retained.values()]
    assert 2 <= median(frequencies) <= 3
    assert quantiles(frequencies, n=20, method="inclusive")[18] > median(frequencies)
    assert abs(sum(n == 1 for n in frequencies) / len(frequencies) - .35) < .002
    assert 0 < sum(n >= 20 for n in frequencies) / len(frequencies) < .05
    cents = [row["cents"] for row in positive.values()]
    assert sum(c >= 100000 for c in cents) == round(len(cents) * .015)
    assert sum(c >= 1000000 for c in cents) == round(len(cents) * .0005)
    assert max(cents) == 5000000
    assert 4000 <= median(cents) <= 6000
    assert len({r["original_donation_id"] for r in refunds}) == len(refunds)
    for refund in refunds:
        original = positive[refund["original_donation_id"]]
        assert refund["cents"] == original["cents"]
        assert refund["contact_external_id"] == original["contact_external_id"]
        assert refund["source_code"] == original["source_code"]
        assert original["gift_date"] <= refund["gift_date"] <= result["end_date"]


def test_audit_giving_keeps_memberships_and_models_consistent(giving_ledger):
    from decimal import Decimal
    from app.seed_audit import TARGETS, DENOMINATOR, GT, GT_CODES
    result, people, positive, refunds, retained = giving_ledger
    reference = date.fromisoformat(result["as_of"])
    n = lambda name: round(result["requested_people"] * TARGETS[name] / DENOMINATOR)
    subscription_end = n("Basic Donors") + n("TBN Plus Premium")
    app_recent = min(n("Donors Past 30 Days"), n("TBN App Donors"))
    expected_recent = {f"crm-{i:08d}" for i in range(app_recent)} | {
        f"crm-{i:08d}" for i in range(subscription_end,
            subscription_end + n("Donors Past 30 Days") - app_recent)}
    cutoff = reference.toordinal() - 30
    recent_rows = [row for row in positive.values()
                   if date.fromisoformat(row["gift_date"]).toordinal() >= cutoff]
    assert len(recent_rows) == result["giving_targets"]["recent_donations"]
    assert 1.2 <= len(recent_rows) / n("Donors Past 30 Days") <= 1.6
    assert {r["contact_external_id"] for r in recent_rows} == expected_recent
    actual_recent = {key for key, rows in retained.items()
                     if any(date.fromisoformat(r["gift_date"]).toordinal() >= cutoff for r in rows)}
    assert actual_recent == expected_recent
    for key, rows in retained.items():
        person = people[key]
        total = sum(row["cents"] for row in rows)
        latest = max(row["gift_date"] for row in rows)
        assert int(person["rfm_raw_frequency"]) == int(person["number_of_donations"]) == len(rows)
        assert int(Decimal(person["lifetime_amount"]) * 100) == total
        assert int(Decimal(person["rfm_raw_monetary"]) * 100) == total
        assert person["last_donation_date"] == latest
        recency = (reference-date.fromisoformat(latest)).days
        assert int(person["rfm_raw_recency"]) == recency
        assert (int(person["rfm_recency_score"]) == 0) == (recency > 730)
        assert (person["donor_status"] == "lapsed") == (recency > 730)
    actual_one_time_basic = {key for key, person in people.items()
                            if person.get("tbn_plus_subscription") == "basic"
                            and person.get("rfm_raw_frequency") == "1"}
    assert actual_one_time_basic == {f"crm-{i:08d}" for i in range(
        n("Basic Recurring Donors"), n("Basic Recurring Donors") + n("Basic One-Time Donors"))}
    assert sum(p.get("rfm_recency_score") == "0" for p in people.values()) == n("RFM Lapsed Donors")
    gt_gifts = [row for row in positive.values() if row["source_code"] in GT_CODES]
    assert len(gt_gifts) == (n("Giving Tuesday 2024") if result["giving_tuesday_in_window"] else 0)
    assert all(row["gift_date"] == str(GT) for row in gt_gifts)


def test_audit_recurring_partners_have_monthly_history(giving_ledger):
    import calendar
    from datetime import timedelta
    result, people, positive, refunds, retained = giving_ledger
    reference = date.fromisoformat(result["as_of"])
    start = date.fromisoformat(result["start_date"])
    expected = set()
    month = start.replace(day=1)
    while month < reference.replace(day=1):
        day = month.replace(day=calendar.monthrange(month.year, month.month)[1])
        if start <= day:
            expected.add(day.isoformat())
        month = (day + timedelta(days=1)).replace(day=1)
    monthly = {}
    for row in positive.values():
        if row["is_recurring"] == "true":
            monthly.setdefault(row["contact_external_id"], set()).add(row["gift_date"])
    recurring = {key for key, p in people.items() if p["has_recurring_donation"] == "true"}
    assert set(monthly) == recurring
    assert all(days == expected for days in monthly.values())


def test_audit_seasonal_weights_preserve_year_end_and_summer_dip():
    from app.seed_audit_giving import seasonal_days
    days, cumulative = seasonal_days(date(2024, 1, 1), date(2024, 12, 31))
    weights = [cumulative[0], *(right-left for left, right in zip(cumulative, cumulative[1:]))]
    year_end = sum(weight for day, weight in zip(days, weights) if day.month >= 11)
    assert year_end / cumulative[-1] == pytest.approx(.30)
    july = sum(weight for day, weight in zip(days, weights) if day.month == 7)
    january = sum(weight for day, weight in zip(days, weights) if day.month == 1)
    assert july < january


def test_audit_calendar_is_balanced_without_changing_gifts(giving_ledger):
    from collections import defaultdict
    from app.seed_timeline import timeline_checks
    result, people, positive, refunds, retained = giving_ledger
    returned = {row["original_donation_id"] for row in refunds}
    grouped = defaultdict(lambda: dict(gifts=0, dollars=0, retained_dollars=0,
                                       year_end=0, gross_year_end=0))
    for gift in positive.values():
        day = date.fromisoformat(gift["gift_date"])
        row = grouped[day.year, gift["is_recurring"] == "true"]
        row["gifts"] += 1
        row["dollars"] += gift["cents"]
        if day.month >= 11:
            row["gross_year_end"] += gift["cents"]
        if gift["external_id"] not in returned:
            row["retained_dollars"] += gift["cents"]
            if day.month >= 11:
                row["year_end"] += gift["cents"]
    rows = [dict(year=year, is_recurring=recurring, **values)
            for (year, recurring), values in grouped.items()]
    reference = date.fromisoformat(result["as_of"])
    verdict = timeline_checks(rows, reference)
    assert verdict["errors"] == []
    stops = [date.fromisoformat(person["last_donation_date"])
             for person in people.values() if person["donor_status"] == "lapsed"]
    assert len({day.year for day in stops}) >= 3
    assert sum(731 <= (reference-day).days <= 791 for day in stops) / len(stops) < .35
    # The calendar plan is checked before writing files and independently
    # recomputed here from the CSV ledger, including linked return exclusions.
    assert result["timeline_checks"]["errors"] == []


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
