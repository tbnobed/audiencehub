import os
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.settings import api


class _Result:
    def __init__(self, value):
        self.value = value

    def one_or_none(self):
        return self.value


class _Database:
    def __init__(self, current_month=1, preset=None, start=None, end=None):
        self.current = (current_month, preset, start, end)
        self.executed = []
        self.entries = []
        self.committed = False

    def execute(self, statement, parameters=None):
        self.executed.append((str(statement), parameters))
        if str(statement).startswith("SELECT"):
            return _Result(self.current)
        self.current = (
            parameters["month"], parameters["preset"],
            parameters["start"], parameters["end"],
        )
        return _Result(None)

    def add(self, entry):
        self.entries.append(entry)

    def commit(self):
        self.committed = True


def _role_dependency(route, name):
    return next(
        dependency.call
        for dependency in route.dependant.dependencies
        if dependency.name == name
    )


def _update(db, **values):
    return api.update_admin_settings(
        api.SettingsUpdate.model_validate(values),
        user=SimpleNamespace(id=42), db=db,
    )


def test_settings_routes_are_registered_and_role_protected():
    routes = {
        (route.path, method): route
        for route in api.router.routes
        for method in route.methods or set()
    }
    admin_get = routes[("/api/admin/settings", "GET")]
    admin_patch = routes[("/api/admin/settings", "PATCH")]
    dashboard_get = routes[("/api/dashboards/settings", "GET")]

    for route in (admin_get, admin_patch):
        guard = _role_dependency(route, "user")
        for role in ("viewer", "analyst"):
            with pytest.raises(HTTPException) as error:
                guard(SimpleNamespace(role=role))
            assert error.value.status_code == 403
        assert guard(SimpleNamespace(role="admin")).role == "admin"

    viewer_guard = _role_dependency(dashboard_get, "user")
    assert viewer_guard(SimpleNamespace(role="viewer")).role == "viewer"
    assert viewer_guard(SimpleNamespace(role="admin")).role == "admin"


@pytest.mark.parametrize("month", [0, 13, True, 1.0, "1", None])
def test_fiscal_year_month_rejects_values_outside_strict_integer_range(month):
    with pytest.raises(ValidationError):
        api.SettingsUpdate.model_validate({"fiscal_year_start_month": month})


@pytest.mark.parametrize("environment,preset,start,end", [
    ("development", "custom", "2020-01-01", "2024-12-31"),
    ("production", "90d", None, None),
    ("test", "90d", None, None),
])
def test_unset_resolves_at_read_time_without_writing(monkeypatch, environment, preset, start, end):
    monkeypatch.setenv("APP_ENV", environment)
    db = _Database(current_month=4)
    expected = {"fiscal_year_start_month": 4, "dashboard_default_preset": preset,
                "dashboard_default_from": start, "dashboard_default_to": end}
    assert api.get_admin_settings(user=SimpleNamespace(role="admin"), db=db) == expected
    assert api.get_dashboard_settings(user=SimpleNamespace(role="viewer"), db=db) == expected
    assert all(query.startswith("SELECT") for query, _ in db.executed)


def test_update_persists_setting_and_audits_only_changed_values(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    db = _Database()
    assert _update(db, fiscal_year_start_month=7) == {
        "fiscal_year_start_month": 7, "dashboard_default_preset": "90d",
        "dashboard_default_from": None, "dashboard_default_to": None,
    }
    assert db.executed[-2][1] == {"month": 7, "preset": None, "start": None, "end": None}
    assert db.committed
    assert len(db.entries) == 1
    audit = db.entries[0]
    assert audit.action == "settings.fiscal_year_start_month.update"
    assert audit.entity_type == "setting"
    assert audit.entity_id == "fiscal_year_start_month"
    assert audit.details == {"previous": 1, "updated": 7}
    assert audit.user_id == 42


def test_partial_fiscal_update_retains_explicit_custom_range_and_noop_is_not_audited():
    db = _Database(1, "custom", date(2021, 2, 1), date(2021, 4, 1))
    assert _update(db, fiscal_year_start_month=7)["dashboard_default_from"] == "2021-02-01"
    assert db.entries[0].action == "settings.fiscal_year_start_month.update"
    db.entries.clear()
    db.committed = False
    assert _update(db, fiscal_year_start_month=7)["dashboard_default_to"] == "2021-04-01"
    assert db.entries == []
    assert not db.committed


def test_custom_override_persists_across_environment_and_90d_clears_dates(monkeypatch):
    db = _Database()
    monkeypatch.setenv("APP_ENV", "production")
    custom = _update(db, dashboard_default_preset="custom",
                     dashboard_default_from="2020-01-01", dashboard_default_to="2024-12-31")
    assert custom["dashboard_default_preset"] == "custom"
    assert custom["dashboard_default_to"] == "2024-12-31"
    assert db.entries[0].action == "settings.dashboard_default.update"
    assert db.entries[0].details["previous"] == {
        "dashboard_default_preset": None, "dashboard_default_from": None, "dashboard_default_to": None}
    monkeypatch.setenv("APP_ENV", "development")
    assert api.get_dashboard_settings(user=SimpleNamespace(role="viewer"), db=db) == custom
    reset = _update(db, dashboard_default_preset="90d")
    assert reset["dashboard_default_preset"] == "90d"
    assert reset["dashboard_default_from"] is None and reset["dashboard_default_to"] is None
    assert db.executed[-2][1]["start"] is None


@pytest.mark.parametrize("values", [
    {"dashboard_default_preset": "custom"},
    {"dashboard_default_preset": "custom", "dashboard_default_from": "2020-01-01"},
    {"dashboard_default_from": "2020-01-01"},
    {"dashboard_default_preset": "custom", "dashboard_default_from": None, "dashboard_default_to": "2020-01-01"},
    {"dashboard_default_preset": "custom", "dashboard_default_from": "2024-01-01", "dashboard_default_to": "2023-01-01"},
    {"dashboard_default_preset": "custom", "dashboard_default_from": "2020-01-01", "dashboard_default_to": "2031-01-01"},
    {"dashboard_default_preset": "custom", "dashboard_default_from": "2020-02-30", "dashboard_default_to": "2020-03-01"},
    {"dashboard_default_preset": "custom", "dashboard_default_from": "1899-12-31", "dashboard_default_to": "1900-01-01"},
    {"dashboard_default_preset": "custom", "dashboard_default_from": "2020-01-01", "dashboard_default_to": "3000-01-01"},
    {"dashboard_default_preset": "custom", "dashboard_default_from": "2020-1-1", "dashboard_default_to": "2020-01-02"},
    {"dashboard_default_preset": "90d", "dashboard_default_from": "2020-01-01"},
    {"dashboard_default_preset": None},
    {"dashboard_default_preset": "all"},
])
def test_invalid_date_override_rejected_atomically(values):
    db = _Database()
    with pytest.raises(ValidationError):
        _update(db, **values)
    assert db.executed == []
    assert db.entries == []


@pytest.mark.skipif(os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
                    reason="requires benchmark's isolated migrated PostgreSQL database")
def test_migration_constraints_on_disposable_pg_only():
    # The benchmark harness migrates a disposable cluster before enabling this test.
    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0023_dashboard_default_range"
                connection.execute(text(
                    "UPDATE admin_settings SET dashboard_default_preset='custom', "
                    "dashboard_default_from='2020-01-01', dashboard_default_to='2020-01-02' WHERE id=1"
                ))
                for preset, start, end in [
                    ("bogus", None, None), ("custom", None, None),
                    ("90d", "2020-01-01", None),
                    ("custom", "2020-02-01", "2020-01-01"),
                    ("custom", "1899-01-01", "1900-01-01"),
                    ("custom", "2020-01-01", "3000-01-01"),
                    ("custom", "2020-01-01", "2031-01-01"),
                ]:
                    nested = connection.begin_nested()
                    with pytest.raises(IntegrityError):
                        connection.execute(text(
                            "UPDATE admin_settings SET dashboard_default_preset=:preset, "
                            "dashboard_default_from=:start, dashboard_default_to=:end WHERE id=1"
                        ), {"preset": preset, "start": start, "end": end})
                    nested.rollback()
            finally:
                transaction.rollback()
    finally:
        engine.dispose()