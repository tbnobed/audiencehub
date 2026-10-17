"""Segment CLI smoke; production-schema isolation is exercised on private PG."""
import argparse
import json
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text

from app.benchmark import add_arguments, run_benchmark
from app.segments.verification import REFERENCE_SQL
from test_schema_benchmark_pg import admin


def test_segment_benchmark_arguments_and_independent_references():
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(["--segments", "--profiles", "50000", "--scale", "small"])
    assert args.segments and args.scale == "small" and args.profiles == 50000
    assert len(REFERENCE_SQL) == 6
    assert len(set(REFERENCE_SQL.values())) == 6
    args.traits = True
    with pytest.raises(ValueError, match="cannot be combined"):
        run_benchmark(args)
    args.traits = False
    args.profiles = 0
    with pytest.raises(ValueError, match="positive"):
        run_benchmark(args)


def test_segments_cli_isolated_small_seed(admin, tmp_path):
    engine, url = admin
    profiles = int(os.environ.get("KINSHIP_SEGMENTS_ACCEPTANCE_PROFILES", "1500"))
    with engine.begin() as db:
        db.execute(text("CREATE TABLE public.segment_benchmark_sentinel (value text)"))
        db.execute(text("INSERT INTO public.segment_benchmark_sentinel VALUES ('untouched')"))
        before_jobs = db.scalar(text("SELECT count(*) FROM public.jobs"))
    try:
        env = dict(os.environ, DATABASE_URL=url, APP_ENV="production", AUTH_MODE="oidc",
                   OIDC_ISSUER="https://oidc.invalid", OIDC_CLIENT_ID="fixture",
                   OIDC_CLIENT_SECRET="fixture", PUBLIC_BASE_URL="https://fixture.invalid",
                   SECRET_KEY="x" * 40, KINSHIP_BENCHMARK_BACKEND="schema",
                   KINSHIP_BENCHMARK_OUTPUT=str(tmp_path))
        result = subprocess.run(["kinship", "benchmark", "--segments",
                                 "--profiles", str(profiles), "--scale", "small"],
                                env=env, capture_output=True, text=True, timeout=1800)
        assert result.returncode == 0, result.stdout + result.stderr
        report_path = next(tmp_path.glob("segments-*/segments.json"))
        report = json.loads(report_path.read_text())
        assert report["status"] == "passed" and len(report["templates"]) == 6
        assert report["requested_people"] == profiles and report["scale"] == "small"
        assert all(row["pass"] and row["count"] == row["materialized_count"] for row in report["templates"])
        assert all(len(row["preview_warm_ms"]) == 3 for row in report["templates"])
        assert all(row["sample_rows"] <= 25 for row in report["templates"])
        assert all(row["key"] in result.stdout for row in report["templates"])
        assert "preview cold ms" in result.stdout and "materialize ms" in result.stdout
        receipt = json.loads((report_path.parent / "isolation.json").read_text())
        assert receipt["cleanup"] == "complete"
        assert not (report_path.parent / "seed").exists()
        with engine.connect() as db:
            assert db.scalar(text("SELECT value FROM public.segment_benchmark_sentinel")) == "untouched"
            assert db.scalar(text("SELECT count(*) FROM public.jobs")) == before_jobs
            assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:s)"), {"s": receipt["schema"]})
            assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:s)"), {"s": receipt["schema"]})
        if output := os.environ.get("KINSHIP_SEGMENTS_REPORT_FILE"):
            Path(output).write_text(report_path.read_text())
    finally:
        with engine.begin() as db:
            db.execute(text("DROP TABLE public.segment_benchmark_sentinel"))
