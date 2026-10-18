"""Cheap compiler/catalog contracts; full populations are explicitly opt-in."""
import json
import os
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.segments.compiler import compile_definition
from app.segments.templates import template_definitions


def test_audit_catalog_matches_targets_and_compiles():
    from app.seed_audit import TARGETS, DENOMINATOR
    targets = json.loads((Path(__file__).parents[2] / "docs/ZETA_AUDIENCE_TARGETS.json").read_text())
    assert targets["audiences"] == TARGETS
    assert targets["denominator"] == DENOMINATOR == 7807899
    templates = template_definitions()
    assert len(templates) == len({t["key"] for t in templates}) == 23
    assert {t["name"] for t in templates} == set(targets["audiences"])
    db = MagicMock()
    db.execute.return_value.all.return_value = []
    for template in templates:
        query = compile_definition(db, template["definition"], date(2026, 10, 17))
        sql = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        assert "active_profiles" in sql
        assert "cohort" not in sql
    by_name = {t["name"]: json.dumps(t["definition"]) for t in templates}
    assert '"rfm_raw_frequency"' in by_name["Basic One-Time Donors"]
    assert '"rfm_recency_score"' in by_name["RFM Lapsed Donors"]
    assert '"type": "trait"' not in by_name["RFM Lapsed Donors"]
    assert '"negate": true' in by_name["TBN App Non-Donors"]
    assert '"op": "or"' in by_name["Active (120 days)"]
    assert '"2024-12-03"' in by_name["Giving Tuesday 2024"]
    assert '"years": 5' in by_name["Living Legacy Prospects V2"]
    assert '"name": "vod_viewed"' in by_name["Customers with VOD Viewed"]


def test_five_year_window_uses_calendar_anniversary_and_pinned_utc():
    from datetime import datetime, timezone
    db = MagicMock()
    db.execute.return_value.all.return_value = []
    definition = next(t["definition"] for t in template_definitions()
                      if t["name"] == "Living Legacy Prospects V2")
    query = compile_definition(db, definition, date(2024, 2, 29))
    params = query.compile(dialect=postgresql.dialect()).params
    assert datetime(2019, 2, 28, tzinfo=timezone.utc) in params.values()
    assert datetime(2024, 2, 29, tzinfo=timezone.utc) in params.values()


@pytest.mark.parametrize("profiles", [50_000, 500_000])
def test_audit_segments_full_population(tmp_path, profiles):
    if os.environ.get("KINSHIP_AUDIT_SEGMENTS_PROFILES") != str(profiles):
        pytest.skip("Opt-in population; run inside the private benchmark runner")
    if os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        pytest.skip("Requires migrated private schema/socket")
    from app.segments.audit_verification import measure
    report = measure(tmp_path, profiles, scale="small" if profiles == 50_000 else "medium",
                     seed_dir=os.environ.get("KINSHIP_AUDIT_SEGMENTS_SEED_DIR"))
    assert report["status"] == "passed"
    assert len(report["templates"]) == 23
    assert all(t["missing_count"] == t["unexpected_count"] == 0 for t in report["templates"])
