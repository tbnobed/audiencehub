import argparse
import csv

import pytest

from app.benchmark import acceptance, add_arguments, generate_csv, run_benchmark


def test_benchmark_defaults():
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(["--imports"])
    assert args.gift_rows == 1_000_000
    assert args.contact_rows == 500_000
    assert not args.profile


def test_benchmark_requires_opt_in(monkeypatch):
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    with pytest.raises(ValueError, match="explicitly"):
        run_benchmark(parser.parse_args([]))
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValueError, match="forbidden"):
        run_benchmark(parser.parse_args(["--imports"]))


@pytest.mark.parametrize("kind", ["gift", "contact"])
def test_generator_deterministic(tmp_path, kind):
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    generate_csv(first, kind, 3)
    generate_csv(second, kind, 3)
    assert first.read_bytes() == second.read_bytes()
    with first.open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 3
    assert len({row["external_id"] for row in rows}) == 3


def test_acceptance_requires_persisted_counts_and_strict_duration():
    row = dict(kind="gift", status="completed", rows_ok=100, rows=100,
               rows_rejected=0, rows_per_second=5000, elapsed_seconds=300,
               source_records=100, gifts=99)
    checks = acceptance([row])
    assert checks["gift_gte_5000_rows_per_second"]
    assert checks["gift_source_count"]
    assert not checks["gift_under_300_seconds"]
    assert not checks["gift_database_count"]