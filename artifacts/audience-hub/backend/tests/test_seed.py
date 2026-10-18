import csv
import json
import random
import subprocess
import sys
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.importer import _mapping
import app.seed as seed_module
from app.seed import CSV_FILES, _email, generate_seed as _generate_seed
from functools import partial

# This suite tests the deliberately messy original CSV fixtures, not audit mix.
generate_seed = partial(_generate_seed, mode="legacy")
from app.imports.validation import map_and_validate_row, normalize_email
from app.seed_dates import seed_window


def test_synthetic_enrichment_has_explicit_demo_evidence(tmp_path):
    result = generate_seed(profiles=3, output_dir=tmp_path)
    with result["files"]["zeta_enrichment.csv"].open() as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if not row["external_id"]:  # Deliberately unrecoverable row remains invalid.
            continue
        assert row["vendor"] == "kinship-synthetic-demo"
        assert row["license_id"] == "synthetic-demo-only"
        assert date.fromisoformat(row["append_date"])
        assert row["sensitive"] == "false"
        mapped = _mapping(list(row), "enrichment")
        converted = map_and_validate_row(row, mapped["columns"], "enrichment", mapped["options"])
        assert converted["errors"] == []


def test_seed_email_ids_do_not_collide_after_gmail_normalization():
    class RepeatingFaker:
        def user_name(self):
            return "same.name"

    rng = random.Random(1)
    emails = [normalize_email(_email(RepeatingFaker(), rng, number))
              for number in range(1, 1_001)]
    assert None not in emails
    assert len(set(emails)) == len(emails)


def test_seed_default_uses_upload_dir_not_installed_module_or_cwd(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(seed_module, "__file__", "/usr/local/lib/python3.12/site-packages/app/seed.py")
    monkeypatch.chdir(tmp_path)

    result = generate_seed(profiles=1)

    assert result["output_dir"] == uploads / "seed-data"
    assert all(path.is_file() for path in result["files"].values())
    assert result["ground_truth"].is_file()
    assert result["generator_stats"].is_file()
    assert not (tmp_path / "seed-data").exists()


def test_seed_explicit_output_dir_overrides_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    result = generate_seed(profiles=1, output_dir=tmp_path / "chosen")
    assert result["output_dir"] == tmp_path / "chosen"
    assert result["ground_truth"].is_file()
    assert not (tmp_path / "uploads").exists()


def test_seed_cli_without_credentials_or_database(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    database = tmp_path / "must-not-create.sqlite"
    env = os.environ.copy()
    for key in ("SECRET_KEY", "FERNET_KEY", "PII_HASH_PEPPER", "OIDC_ISSUER",
                "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "PUBLIC_BASE_URL"):
        env.pop(key, None)
    env.update({
        "DATABASE_URL": f"sqlite:///{database}",
        "AUTH_MODE": "oidc",
        "UPLOAD_DIR": str(tmp_path / "uploads"),
        "PYTHONPATH": str(backend),
    })
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "seed", "--profiles", "1"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert str(tmp_path / "uploads" / "seed-data") in result.stdout
    assert not database.exists()


def test_seed_stats_cover_messiness_and_unrecoverable_rows(tmp_path):
    generated = generate_seed(profiles=3, scale="small", random_seed=19, output_dir=tmp_path)
    stats = json.loads(generated["generator_stats"].read_text(encoding="utf-8"))
    truth = json.loads(generated["ground_truth"].read_text(encoding="utf-8"))

    assert set(stats["files"]) == set(CSV_FILES)
    assert stats["files"]["donor_crm_contacts.csv"]["messiness_injected"]["invalid_phone"] > 0
    assert stats["files"]["esp_contacts.csv"]["messiness_injected"]["phone_only_match"] > 0
    assert stats["files"]["giving_platform_gifts.csv"]["messiness_injected"]["unrecoverable_row"] == 1
    assert all(stats["files"][name]["messiness_injected"]["duplicate_row"] == 1
               for name in CSV_FILES)

    record_types = {
        "donor_crm_contacts.csv": "contact",
        "giving_platform_gifts.csv": "gift",
        "esp_contacts.csv": "contact",
        "five9_calls.csv": "event",
        "zeta_enrichment.csv": "enrichment",
    }
    truth_ids = set()
    for filename in CSV_FILES:
        with generated["files"][filename].open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == stats["files"][filename]["rows_generated"]
        mapping = _mapping(list(rows[0]), record_types[filename])["columns"]
        invalid = map_and_validate_row(rows[-1], mapping, record_types[filename])
        assert invalid["errors"], filename
        source_prefix = filename.split("_")[0]
        truth_ids.update((source_prefix, row["external_id"]) for row in rows
                         if row.get("external_id"))
        assert stats["files"][filename]["import_handling"] is None
    assert len(truth["records"]) == len(truth_ids)


@pytest.mark.parametrize("as_of,expected_start", [
    (date(2026, 7, 18), date(2021, 7, 18)),
    (date(2024, 2, 29), date(2019, 2, 28)),
    (date(2025, 1, 1), date(2020, 1, 1)),
])
def test_seed_calendar_anniversary_window(as_of, expected_start):
    window = seed_window(as_of)
    assert window.start == expected_start
    assert window.end == as_of - timedelta(days=1)
    assert window.start_at == datetime.combine(expected_start, datetime.min.time(), timezone.utc)
    assert window.end_at < datetime.combine(as_of, datetime.min.time(), timezone.utc)


def test_seed_defaults_to_utc_today():
    before = datetime.now(timezone.utc).date()
    window = seed_window()
    after = datetime.now(timezone.utc).date()
    assert before <= window.as_of <= after


def _parse_seed_timestamp(value):
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize("as_of", [date(2026, 7, 18), date(2024, 2, 29), date(2025, 1, 1)])
def test_every_seed_timestamp_is_bounded_and_metadata_explicit(tmp_path, as_of):
    generated = generate_seed(300, random_seed=19, output_dir=tmp_path, as_of=as_of)
    window = seed_window(as_of)
    fields = {
        CSV_FILES[0]: ("created_at",), CSV_FILES[1]: ("gift_date",),
        CSV_FILES[2]: ("consent_captured_at",),
        CSV_FILES[3]: ("occurred_at", "received_at"), CSV_FILES[4]: ("enriched_at",),
    }
    dates, views = {}, Counter()
    for filename, columns in fields.items():
        with generated["files"][filename].open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        seen = set()
        for row in rows:
            if not row["external_id"] or row["external_id"] in seen:
                continue  # Known rejected sentinel or deliberate duplicate.
            seen.add(row["external_id"])
            for column in columns:
                stamp = _parse_seed_timestamp(row[column])
                assert window.start_at <= stamp <= window.end_at, (filename, column, stamp)
            if filename == CSV_FILES[1]:
                dates[row["email"]] = True
            if filename == CSV_FILES[3]:
                assert _parse_seed_timestamp(row["received_at"]) >= _parse_seed_timestamp(row["occurred_at"])
                if row["name"] == "Video Watched":
                    assert _parse_seed_timestamp(row["occurred_at"]).date() >= as_of - timedelta(days=29)
                    views[row["email"]] += 1
    assert any(count >= 3 and email not in dates for email, count in views.items())
    for path in (generated["generator_stats"], generated["ground_truth"]):
        metadata = json.loads(path.read_text())
        assert metadata["as_of"] == as_of.isoformat()
        assert metadata["start_date"] == window.start.isoformat()
        assert metadata["end_date"] == window.end.isoformat()
    assert generated["as_of"] == as_of.isoformat()


def test_seed_explicit_date_is_byte_deterministic(tmp_path):
    first = generate_seed(80, random_seed=19, output_dir=tmp_path / "one", as_of=date(2026, 7, 18))
    second = generate_seed(80, random_seed=19, output_dir=tmp_path / "two", as_of=date(2026, 7, 18))
    for name in CSV_FILES:
        assert first["files"][name].read_bytes() == second["files"][name].read_bytes()
    for name in ("generator_stats", "ground_truth"):
        assert first[name].read_bytes() == second[name].read_bytes()


def test_integrated_small_seed_50k_has_all_six_template_populations(tmp_path):
    """Cross-file CSV acceptance; deliberately independent of importer/trait SQL."""
    as_of = date(2026, 7, 18)
    window = seed_window(as_of)
    result = generate_seed(50_000, random_seed=20250308, output_dir=tmp_path, as_of=as_of)
    dates, lifetime, recent = defaultdict(list), Counter(), Counter()
    recurring, opted_in, videos = set(), set(), Counter()
    for filename in CSV_FILES:
        with result["files"][filename].open(encoding="utf-8", newline="") as stream:
            seen = set()
            for row in csv.DictReader(stream):
                identifier = row["external_id"]
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                for column in ("created_at", "gift_date", "consent_captured_at",
                               "occurred_at", "received_at", "enriched_at"):
                    if row.get(column):
                        assert window.start_at <= _parse_seed_timestamp(row[column]) <= window.end_at
                if filename == CSV_FILES[1]:
                    person = row["contact_external_id"]
                    when = _parse_seed_timestamp(row["gift_date"]).date()
                    cents = round(float(row["amount"]) * 100)
                    dates[person].append(when)
                    lifetime[person] += cents
                    if (as_of - when).days <= 365:
                        recent[person] += cents
                        if row["is_recurring"] == "true":
                            recurring.add(person)
                elif filename == CSV_FILES[2] and row["email_consent"] == "opted_in":
                    opted_in.add(row["contact_external_id"])
                elif filename == CSV_FILES[3] and row["name"] == "Video Watched":
                    videos["crm-" + identifier.split("-")[1]] += 1
    status = Counter("active" if (as_of - max(days)).days <= 365 else
                     "lapsing" if (as_of - max(days)).days <= 730 else "lapsed"
                     for days in dates.values())
    assert len(dates) == 35_000
    assert status == {"active": 14_000, "lapsing": 4_200, "lapsed": 16_800}
    top_count = max(1, (len(recent) + 19) // 20)
    top_threshold = sorted(recent.values(), reverse=True)[top_count - 1]
    populations = {
        "lapsed-high-value": sum((as_of - max(days)).days > 730 and lifetime[person] >= 50_000
                                 for person, days in dates.items()),
        "new-partners": sum(min(days) >= as_of - timedelta(days=90) for days in dates.values()),
        "recurring-partners": len(recurring),
        "top-giving": sum(value >= top_threshold for value in recent.values()),
        "engaged-not-giving": sum(count >= 3 and person not in dates
                                 for person, count in videos.items()),
        "lapsing-opted-in": sum(person in opted_in and len(days) >= 2
                               and (as_of - max(days)).days > 365 for person, days in dates.items()),
    }
    assert all(count > 0 for count in populations.values()), populations