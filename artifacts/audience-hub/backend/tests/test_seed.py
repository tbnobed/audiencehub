import csv
import json
import random
import subprocess
import sys
import os
from pathlib import Path

from app.importer import _mapping
import app.seed as seed_module
from app.seed import CSV_FILES, _email, generate_seed
from app.imports.validation import map_and_validate_row, normalize_email


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