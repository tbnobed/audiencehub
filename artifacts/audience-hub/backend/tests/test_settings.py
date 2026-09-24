from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.settings import api


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Database:
    def __init__(self, current_month=1):
        self.current_month = current_month
        self.executed = []
        self.entries = []
        self.committed = False

    def execute(self, statement, parameters=None):
        self.executed.append((str(statement), parameters))
        if str(statement).startswith("SELECT"):
            return _Result(self.current_month)
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
        with pytest.raises(HTTPException) as error:
            guard(SimpleNamespace(role="analyst"))
        assert error.value.status_code == 403
        assert guard(SimpleNamespace(role="admin")).role == "admin"

    viewer_guard = _role_dependency(dashboard_get, "user")
    assert viewer_guard(SimpleNamespace(role="viewer")).role == "viewer"


@pytest.mark.parametrize("month", [0, 13, True, 1.0, "1"])
def test_fiscal_year_month_rejects_values_outside_strict_integer_range(month):
    with pytest.raises(ValidationError):
        api.SettingsUpdate.model_validate({"fiscal_year_start_month": month})


def test_update_persists_setting_and_audits_only_changed_values():
    db = _Database(current_month=1)
    result = api.update_admin_settings(
        api.SettingsUpdate(fiscal_year_start_month=7),
        user=SimpleNamespace(id=42),
        db=db,
    )

    assert result == {"fiscal_year_start_month": 7}
    assert db.executed[-1][1] == {"month": 7}
    assert db.committed
    assert len(db.entries) == 1
    audit = db.entries[0]
    assert audit.action == "settings.fiscal_year_start_month.update"
    assert audit.entity_type == "setting"
    assert audit.entity_id == "fiscal_year_start_month"
    assert audit.details == {"previous": 1, "updated": 7}
    assert audit.user_id == 42


def test_unchanged_setting_does_not_create_audit_entry():
    db = _Database(current_month=7)
    result = api.update_admin_settings(
        api.SettingsUpdate(fiscal_year_start_month=7),
        user=SimpleNamespace(id=42),
        db=db,
    )

    assert result == {"fiscal_year_start_month": 7}
    assert db.entries == []
    assert not db.committed


def test_dashboard_settings_response_contains_only_viewer_safe_configuration():
    result = api.get_dashboard_settings(
        user=SimpleNamespace(role="viewer"),
        db=_Database(current_month=4),
    )
    assert result == {"fiscal_year_start_month": 4}