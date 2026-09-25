import os
import hashlib
import hmac
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.dashboards import api


def test_dashboard_routes_and_chart_response_contract():
    routes = {
        route.path: route for route in api.router.routes
        if "GET" in (route.methods or set())
    }
    tabs = ("overview", "giving", "retention", "engagement", "sources", "data-health")
    for tab in tabs:
        path = f"/api/dashboards/{tab}"
        assert path in routes
        assert {"from", "to"} <= set(routes[path].endpoint.__annotations__) or routes[path].dependant.query_params

    assert set(api._CSV_CHARTS) == set(tabs)
    assert api.invalidate_cache.__doc__


def test_analyst_csv_role_gate_rejects_viewers():
    csv_route = next(route for route in api.router.routes if route.path.endswith("/csv"))
    analyst_gate = next(
        dependency.call for dependency in csv_route.dependant.dependencies
        if dependency.name == "user"
    )
    with pytest.raises(HTTPException) as error:
        analyst_gate(SimpleNamespace(role="viewer"))
    assert error.value.status_code == 403
    assert analyst_gate(SimpleNamespace(role="analyst")).role == "analyst"


def test_dashboard_prior_range_matches_inclusive_period_length():
    start, end, prior_start, prior_end = api._date_range(
        date(2025, 3, 10), date(2025, 3, 20)
    )
    assert (start, end) == (date(2025, 3, 10), date(2025, 3, 20))
    assert (prior_start, prior_end) == (date(2025, 2, 27), date(2025, 3, 9))


def test_email_opted_in_kpi_unifies_signals_and_excludes_optouts_and_suppressions(monkeypatch):
    pepper = "test-pepper"
    suppressed_email = "bounce@example.org"
    suppressed_hash = hmac.new(
        pepper.encode(), suppressed_email.encode(), hashlib.sha256
    ).hexdigest()
    monkeypatch.setattr(
        api, "get_settings", lambda: type("Settings", (), {"pii_hash_pepper": pepper})()
    )
    candidate_rows = [
        {"profile_id": 1, "email": "open@example.org", "source_opted_in": True,
         "ledger_opted_in": False, "ledger_opted_out": False},
        {"profile_id": 2, "email": "out@example.org", "source_opted_in": False,
         "ledger_opted_in": True, "ledger_opted_out": True},
        {"profile_id": 3, "email": suppressed_email, "source_opted_in": True,
         "ledger_opted_in": False, "ledger_opted_out": False},
    ]

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return self

        def all(self):
            return self._rows

    class ReadOnlyFakeDb:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "FROM consent_flags" in sql:
                return Result(candidate_rows)
            if "FROM suppressions" in sql:
                assert suppressed_hash in params["hashes"]
                return Result([{"value_hash": suppressed_hash}])
            raise AssertionError("Unexpected query")

    assert api._email_opted_in_count(ReadOnlyFakeDb()) == 1


def test_all_dashboard_queries_against_isolated_m4_smoke_database():
    if os.getenv("DATABASE_URL") != "postgresql+psycopg:///ah_m4_smoke":
        pytest.skip("Set DATABASE_URL to the isolated ah_m4_smoke database")

    from app.db import engine
    from sqlalchemy.orm import Session

    expected_charts = {
        "overview": {"monthly_giving", "donor_status", "top_campaigns"},
        "giving": {"monthly_giving", "new_returning", "by_channel", "by_fund",
                   "top_campaigns", "appeal_codes", "gift_size_distribution"},
        "retention": {"retention_by_year", "cohorts", "status_over_time"},
        "engagement": {"events_by_day", "top_event_names", "viewer_to_donor"},
        "sources": {"profiles_by_source", "overlap_matrix", "identifier_coverage"},
        "data-health": {"pending_resolutions", "merges_per_day", "blocklist_hits",
                        "rejected_rows_by_import", "blocklist_review", "expiring_enrichment"},
    }
    with Session(engine) as db:
        for tab, chart_keys in expected_charts.items():
            result = api._build_dashboard(
                db, tab, date(2025, 1, 1), date(2025, 12, 31),
                date(2024, 1, 1), date(2024, 12, 31),
            )
            assert set(result["charts"]) == chart_keys
            assert set(result) == ({"range", "metrics", "charts", "overview"} if tab == "overview"
                                   else {"range", "metrics", "charts"})