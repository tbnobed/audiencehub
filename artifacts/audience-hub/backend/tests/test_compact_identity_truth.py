"""Bounded compact audit truth must detect missing, merged and split identities."""
import json
import sqlite3
from datetime import date

import pytest

from app.seed import generate_seed
from test_identity_acceptance import identity_acceptance as accuracy


@pytest.fixture
def compact(tmp_path):
    result = generate_seed(1000, output_dir=tmp_path, as_of=date(2026, 10, 17))
    local = sqlite3.connect(str(tmp_path/"truth.sqlite"))
    accuracy._sqlite_database(local)
    metadata = accuracy._iter_ground_truth(result["ground_truth"], local)
    # Simulate the normalized actual relation loaded by the read-only PG query.
    local.execute("""INSERT INTO actual(source_key,external_id,profile_id)
        SELECT source_key,external_id,CAST(substr(person_id,8) AS INTEGER) FROM truth""")
    try:
        yield result, local, metadata
    finally:
        local.close()


def test_compact_truth_labels_every_source_and_record(compact):
    result, local, metadata = compact
    expected = sum(result["counts"].values())
    assert metadata["streamed_record_count"] == expected
    assert set(metadata["truth_records_by_source"]) == set(accuracy.FILES.values())
    assert local.execute("SELECT count(DISTINCT person_id) FROM truth").fetchone()[0] == 1000
    metrics = accuracy._pairwise_accuracy(local)
    assert metrics["truth_records"] == metrics["matched_records"] == expected
    assert metrics["truth_positive_pairs_all_records"] > 0
    assert accuracy._compact_accuracy_pass(metrics)


@pytest.mark.parametrize("mutation", [
    "UPDATE actual SET profile_id=999 WHERE profile_id=1000",  # shared household, STILL wrong
    "UPDATE actual SET profile_id=999999 WHERE source_key='esp' AND external_id='esp-0'",
    "DELETE FROM actual WHERE source_key='esp' AND external_id='esp-0'",
    "INSERT INTO actual(source_key,external_id,profile_id) VALUES('esp','unknown',999999)",
    "DELETE FROM actual",
])
def test_compact_truth_rejects_wrong_merge_split_missing_and_unknown(compact, mutation):
    _, local, _ = compact
    local.execute(mutation)
    metrics = accuracy._pairwise_accuracy(local)
    assert not accuracy._compact_accuracy_pass(metrics)
    assert metrics["intentional_household_cross_person_pairs"] == 0


def test_compact_truth_batch_size_is_bounded(tmp_path, monkeypatch):
    result = generate_seed(50, output_dir=tmp_path, as_of=date(2026, 10, 17))
    monkeypatch.setattr(accuracy, "DB_BATCH_SIZE", 7)
    class BoundedConnection(sqlite3.Connection):
        def executemany(self, sql, parameters):
            assert len(parameters) <= 7
            return super().executemany(sql, parameters)
    local = sqlite3.connect(":memory:", factory=BoundedConnection)
    accuracy._sqlite_database(local)
    try:
        metadata = accuracy._iter_ground_truth(result["ground_truth"], local)
        assert metadata["streamed_record_count"] == sum(result["counts"].values())
    finally:
        local.close()


@pytest.mark.parametrize("corruption", ["missing", "truncated", "reference", "unsupported"])
def test_compact_truth_fails_closed_on_incomplete_or_unsupported_input(tmp_path, corruption):
    result = generate_seed(50, output_dir=tmp_path, as_of=date(2026, 10, 17))
    esp = result["files"]["esp_contacts.csv"]
    if corruption == "missing":
        esp.unlink()
    elif corruption == "truncated":
        esp.write_text(esp.read_text().splitlines()[0]+"\n")
    elif corruption == "reference":
        esp.write_text(esp.read_text().replace("crm-00000000", "crm-00000001", 1))
    else:
        truth = json.loads(result["ground_truth"].read_text())
        truth["identity_rule"] = "unsupported-rule"
        result["ground_truth"].write_text(json.dumps(truth))
    local = sqlite3.connect(":memory:")
    accuracy._sqlite_database(local)
    try:
        with pytest.raises(ValueError):
            accuracy._iter_ground_truth(result["ground_truth"], local)
    finally:
        local.close()


def test_cli_parses_eight_million_large_without_generation(monkeypatch):
    import argparse
    import sys
    from app import cli, seed
    class ParsedOnly(Exception):
        pass
    original = argparse.ArgumentParser.parse_args
    def parse_only(parser, *args, **kwargs):
        parsed = original(parser, *args, **kwargs)
        assert parsed.command == "seed"
        assert parsed.profiles == 8_000_000
        assert parsed.scale == "large"
        assert parsed.mode == "audit"
        raise ParsedOnly
    def forbidden(*args, **kwargs):
        pytest.fail("CLI parse-only test must never invoke generation")
    monkeypatch.setattr(sys, "argv", ["kinship", "seed", "--profiles", "8000000", "--scale", "large"])
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse_only)
    monkeypatch.setattr(seed, "generate_seed", forbidden)
    with pytest.raises(ParsedOnly):
        cli.main()
