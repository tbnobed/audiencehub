from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.dashboards import api
from app.dashboards.rollups import refresh_dashboard_rollups
from test_consent_ingestion_pg import db


def test_dashboard_routes_and_chart_response_contract():
    routes = {route.path: route for route in api.router.routes if "GET" in (route.methods or set())}
    tabs = ("overview", "giving", "retention", "engagement", "sources", "data-health")
    for tab in tabs:
        path = f"/api/dashboards/{tab}"
        assert path in routes
        assert routes[path].dependant.query_params
    for card in ("kpis", "giving-by-month", "needs-attention", "campaigns", "partner-status"):
        assert f"/api/dashboards/overview/{card}" in routes
    assert set(api._CSV_CHARTS) == set(tabs)


def test_analyst_csv_role_gate_rejects_viewers():
    csv_route = next(route for route in api.router.routes if route.path.endswith("/csv"))
    gate = next(d.call for d in csv_route.dependant.dependencies if d.name == "user")
    with pytest.raises(HTTPException) as error:
        gate(SimpleNamespace(role="viewer"))
    assert error.value.status_code == 403
    assert gate(SimpleNamespace(role="analyst")).role == "analyst"


def test_dashboard_prior_range_matches_inclusive_period_length():
    assert api._date_range(date(2025, 3, 10), date(2025, 3, 20)) == (
        date(2025, 3, 10), date(2025, 3, 20), date(2025, 2, 27), date(2025, 3, 9))


@pytest.mark.parametrize("end,start", [
    (date(2026, 9, 24), date(2025, 9, 25)),
    (date(2024, 2, 29), date(2023, 3, 1)),
    (date(2025, 2, 28), date(2024, 2, 29)),
    (date(2026, 1, 1), date(2025, 1, 2)),
])
def test_dashboard_missing_start_uses_inclusive_rolling_twelve_months(end, start):
    actual_start, actual_end, prior_start, prior_end = api._date_range(None, end)
    assert (actual_start, actual_end) == (start, end)
    assert (prior_end - prior_start).days == (end - start).days
    assert prior_end < start


def test_dashboard_missing_end_uses_utc_today(monkeypatch):
    class Clock:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            return datetime(2026, 1, 1, 0, 30, tzinfo=tz)

    monkeypatch.setattr(api, "datetime", Clock)
    assert api._date_range(None, None)[:2] == (date(2025, 1, 2), date(2026, 1, 1))
    assert api._date_range(date(2025, 12, 1), None)[:2] == (date(2025, 12, 1), date(2026, 1, 1))


def test_email_count_only_reads_published_ledger_aggregate(monkeypatch):
    seen = []
    monkeypatch.setattr(api, "_kpi", lambda db, key: seen.append(key) or 17)
    assert api._email_opted_in_count(None) == 17
    assert seen == ["email_opted_in"]


def test_all_dashboard_chart_contracts(db):
    refresh_dashboard_rollups(db)
    for name, charts in api._CSV_CHARTS.items():
        result = api._build_dashboard(db, name, date(2025, 1, 1), date(2025, 12, 31),
                                     date(2024, 1, 1), date(2024, 12, 31))
        assert set(result["charts"]) == charts
        assert set(result) == ({"range", "metrics", "charts", "overview"} if name == "overview"
                               else {"range", "metrics", "charts"})


def test_split_card_payload_contracts(db):
    refresh_dashboard_rollups(db)
    db.commit()
    user = SimpleNamespace(role="viewer")
    expected = {
        "kpis": {"range", "kpis", "stats"}, "giving-by-month": {"range", "giving_by_month"},
        "needs-attention": {"attention"}, "campaigns": {"top_campaigns"}, "partner-status": {"partner_status"},
    }
    for card, keys in expected.items():
        result = api._card_endpoint(card)(date(2025, 1, 1), date(2025, 1, 31), user, db)
        assert set(result) == keys
        if card == "giving-by-month":
            assert set(result["giving_by_month"]) == {"monthly_giving", "top_two_month_share", "top_two_months"}
        if card == "campaigns":
            assert set(result["top_campaigns"]) == {"campaigns", "campaigns_href"}