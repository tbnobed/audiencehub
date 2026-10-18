"""Segment CLI smoke; production-schema isolation is exercised on private PG."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from datetime import date, datetime, timezone

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
    assert all("profile_traits" not in sql for sql in REFERENCE_SQL.values())
    args.traits = True
    with pytest.raises(ValueError, match="cannot be combined"):
        run_benchmark(args)
    args.traits = False
    args.isolated = True
    args.profiles = 0
    with pytest.raises(ValueError, match="positive"):
        run_benchmark(args)


def test_segment_reference_date_is_explicit_or_utc_today(monkeypatch):
    from app.segments import verification

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return cls(2031, 1, 2, tzinfo=tz)

    monkeypatch.setattr(verification, "datetime", Clock)
    assert verification.reference_date() == date(2031, 1, 2)
    assert verification.reference_date("2028-02-29") == date(2028, 2, 29)
    assert verification.reference_date(date(2020, 1, 1)) == date(2020, 1, 1)
    with pytest.raises(ValueError):
        verification.reference_date("not-a-date")


def test_integrated_acceptance_rejects_empty_templates_and_stale_publication():
    from copy import deepcopy
    from app.segments.verification import acceptance_passed

    summary = {
        "givers": 100, "new": 10, "reactivated": 5, "recurring_active": 10,
        "giver_groups": {key: {"count": count, "share_pct": count}
                         for key, count in (("active_ish", 40), ("lapsing", 12), ("lapsed", 48))},
        "dashboard_last_12m": {"nonempty": True},
        "current_status_publication_matches": True,
    }
    templates = [{"key": key, "count": 1, "pass": True} for key in REFERENCE_SQL]
    assert acceptance_passed(summary, templates)
    empty = deepcopy(templates)
    empty[0]["count"] = 0
    assert not acceptance_passed(summary, empty)
    mismatch = deepcopy(templates)
    mismatch[1]["pass"] = False
    assert not acceptance_passed(summary, mismatch)
    assert not acceptance_passed(summary, templates[:-1])
    assert not acceptance_passed({**summary, "current_status_publication_matches": False}, templates)
    assert not acceptance_passed({**summary, "dashboard_last_12m": {"nonempty": False}}, templates)
    assert not acceptance_passed({**summary, "reactivated": 0}, templates)


def test_segments_cli_isolated_small_seed(admin, tmp_path):
    engine, url = admin
    profiles = int(os.environ.get("KINSHIP_SEGMENTS_ACCEPTANCE_PROFILES", "50000"))
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
        command = ["kinship", "benchmark", "--segments", "--isolated",
                   "--profiles", str(profiles), "--scale", "small"]
        # Targeted reruns may reuse the identical generated CSV fixture; the
        # benchmark validates population, random seed, scale and as_of before
        # reuse. Imports, identity, traits and every segment still run afresh.
        if os.environ.get("KINSHIP_SEGMENTS_ACCEPTANCE_SEED_DIR"):
            command += ["--seed-dir", os.environ["KINSHIP_SEGMENTS_ACCEPTANCE_SEED_DIR"]]
        result = subprocess.run(command,
                                env=env, capture_output=True, text=True, timeout=1800)
        assert result.returncode == 0, result.stdout + result.stderr
        report_path = next(tmp_path.glob("segments-*/segments.json"))
        report = json.loads(report_path.read_text())
        assert report["status"] == "passed" and len(report["templates"]) == 6
        assert report["requested_people"] == profiles and report["scale"] == "small"
        from datetime import datetime, timezone
        assert report["as_of"] == datetime.now(timezone.utc).date().isoformat()
        acceptance = report["acceptance"]
        assert acceptance["dashboard_last_12m"]["nonempty"]
        assert 35 <= acceptance["giver_groups"]["active_ish"]["share_pct"] <= 45
        assert 10 <= acceptance["giver_groups"]["lapsing"]["share_pct"] <= 15
        assert all(row["count"] > 0 for row in report["templates"])
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
