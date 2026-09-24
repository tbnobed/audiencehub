import csv
import json
import random

from app.importer import _mapping
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