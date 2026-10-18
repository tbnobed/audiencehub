"""Opt-in only: run inside the existing private benchmark runner."""
import os
import pytest


@pytest.mark.skipif(os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
                    reason="requires private benchmark schema/socket and migrated database")
def test_audit_seed_full_import(tmp_path):
    from app.seed_audit_verification import measure
    report = measure(tmp_path, profiles=50_000)
    assert report["passed"], report
