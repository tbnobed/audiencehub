"""Migration readiness checks that do not touch the application database."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from app import main


def _versioned_engine(*revisions):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    with engine.begin() as connection:
        if revisions:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            for revision in revisions:
                connection.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                    {"revision": revision},
                )
    return engine


def test_ready_at_dynamic_script_head(monkeypatch):
    heads = main.migration_script().get_heads()
    assert heads and "0022_client_errors" in heads
    engine = _versioned_engine(*heads)
    monkeypatch.setattr(main, "engine", engine)
    try:
        response = TestClient(main.app).get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}
    finally:
        engine.dispose()


@pytest.mark.parametrize("revisions", [
    ("0007_admin_settings",),
    ("revision_not_in_scripts",),
    (),
])
def test_ready_rejects_behind_ahead_and_missing_version_table(monkeypatch, revisions):
    engine = _versioned_engine(*revisions)
    monkeypatch.setattr(main, "engine", engine)
    try:
        response = TestClient(main.app).get("/readyz")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "http_503"
    finally:
        engine.dispose()


def test_ready_compares_all_heads_in_branched_migrations(monkeypatch):
    engine = _versioned_engine("branch_a", "branch_b")
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "migration_script",
                        lambda: SimpleNamespace(get_heads=lambda: ("branch_b", "branch_a")))
    try:
        assert TestClient(main.app).get("/readyz").status_code == 200
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM alembic_version WHERE version_num='branch_b'"))
        assert TestClient(main.app).get("/readyz").status_code == 503
    finally:
        engine.dispose()


def test_ready_handles_database_unavailable_without_exposing_error(monkeypatch):
    class BrokenEngine:
        def connect(self):
            raise OperationalError("secret connection detail", {}, Exception("secret connection detail"))

    monkeypatch.setattr(main, "engine", BrokenEngine())
    response = TestClient(main.app).get("/readyz")
    assert response.status_code == 503
    assert "secret connection detail" not in response.text


def test_ready_fails_explicitly_when_scripts_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "__file__", str(tmp_path / "installed" / "app" / "main.py"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="scripts are unavailable"):
        main.migration_script()
    response = TestClient(main.app).get("/readyz")
    assert response.status_code == 503
    assert "scripts" not in response.text


def test_installed_app_finds_scripts_from_backend_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "__file__", str(tmp_path / "installed" / "app" / "main.py"))
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    assert main.migration_script().get_heads() == ["0022_client_errors"]


def test_system_reports_actual_database_revision():
    engine = _versioned_engine("0007_admin_settings")
    class Database:
        def __init__(self, connection):
            self.connection = lambda: connection

        def execute(self, statement):
            return []

        def scalars(self, statement):
            return SimpleNamespace(all=lambda: [])

        def scalar(self, statement):
            return "test database"

    try:
        with engine.connect() as connection:
            result = main.system(user=SimpleNamespace(role="admin"), db=Database(connection))
        assert result["migration"] == "0007_admin_settings"
    finally:
        engine.dispose()


@pytest.mark.skipif(os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
                    reason="requires the benchmark's isolated migrated PostgreSQL database")
def test_ready_with_real_migrated_database():
    assert TestClient(main.app).get("/readyz").status_code == 200