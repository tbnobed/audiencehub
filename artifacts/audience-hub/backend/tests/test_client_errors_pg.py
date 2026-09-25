"""Real PostgreSQL + real signed sessions/CSRF. Never connects to DATABASE_URL."""
import importlib.util
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.main import app
from app.models import ClientError, User


@pytest.fixture(scope="module")
def isolated_engine(tmp_path_factory):
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("local PostgreSQL binaries required for disposable cluster")
    root = tmp_path_factory.mktemp("client-errors-pg")
    data, socket = root / "data", root / "socket"
    socket.mkdir()
    subprocess.run(["initdb", "-D", str(data), "-A", "trust", "-U", "testuser", "--no-locale", "--encoding=UTF8"], check=True, capture_output=True)
    subprocess.run(["pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"),
                    "-o", f"-k {socket} -h '' -p 55439", "-w", "start"], check=True, capture_output=True)
    engine = create_engine(f"postgresql+psycopg://testuser@/postgres?host={socket}&port=55439")
    try:
        User.__table__.create(engine)
        path = Path(__file__).resolve().parents[1] / "alembic/versions/0022_client_errors.py"
        spec = importlib.util.spec_from_file_location("client_error_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        assert migration.down_revision == "0021_consent_channel_status"
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        yield engine
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
    finally:
        engine.dispose()
        subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"], check=True, capture_output=True)


@pytest.fixture
def client(isolated_engine):
    with Session(isolated_engine) as db:
        db.query(ClientError).delete()
        db.query(User).delete()
        db.commit()

    def database():
        with Session(isolated_engine) as db:
            yield db
    app.dependency_overrides[session_scope] = database
    # No lifespan: the global app engine is never used.
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.pop(session_scope, None)


def login(client, role="admin"):
    assert client.post("/auth/dev-login", json={"role": role}).status_code == 200
    return {"X-CSRF-Token": client.get("/api/me").json()["csrf_token"]}


def payload(**kwargs):
    return {"category": "TypeError", "route": "/profiles/42",
            "profile_id": 42, "component_stack": ["ProfileDetail", "Card"], **kwargs}


def test_auth_csrf_and_rbac(client):
    assert client.get("/api/admin/client-errors").status_code == 401
    assert client.post("/api/client-errors", json=payload()).status_code == 403
    for role in ("viewer", "analyst", "admin"):
        headers = login(client, role)
        assert client.post("/api/client-errors", json=payload()).status_code == 403
        assert client.post("/api/client-errors", json=payload(), headers={"X-CSRF-Token": "wrong"}).status_code == 403
        assert client.post("/api/client-errors", json=payload(), headers=headers).status_code == 202
        assert client.get("/api/admin/client-errors").status_code == (200 if role == "admin" else 403)
    headers = login(client)
    client.post("/auth/logout", headers=headers)
    assert client.post("/api/client-errors", json=payload(), headers=headers).status_code in (401, 403)


def test_privacy_and_persistence(client, isolated_engine, caplog):
    headers = login(client)
    secret = "Jane Doe jane.private@example.org +15551234567 cookie=secret-token"
    reports = [
        payload(message=secret, category=secret, route="/profiles/42?email=" + secret,
                component_stack=["ProfileDetail", secret, "at Card (https://private.test/?token=secret)", "JaneDoe"]),
        payload(route="https://private.test/JohnSmith?cookie=secret"),
    ]
    for body in reports:
        response = client.post("/api/client-errors", json=body, headers=headers)
        assert response.status_code == 202
        assert secret not in response.text
    items = client.get("/api/admin/client-errors").json()["items"]
    assert items[1]["component_stack"] == ["ProfileDetail"]
    assert items[1]["route"] == "/profiles/42"
    assert items[1]["category"] == "RenderError"
    assert items[0]["route"] == "/unknown"
    assert items[1]["profile_id"] == 42
    with Session(isolated_engine) as db:
        rows = db.execute(select(ClientError.__table__)).mappings().all()
        stored = str(rows)
    for forbidden in ("Jane", "private", "15551234567", "secret-token", "JohnSmith", "https://"):
        assert forbidden not in stored
        assert forbidden not in json.dumps(items)
        assert forbidden not in caplog.text


@pytest.mark.parametrize("body", [
    [], {"profile_id": True}, {"profile_id": -1}, {"profile_id": "42"},
    {"profile_id": 10**19}, {"component_stack": "raw stack"},
    {"component_stack": ["Card"] * 17}, {"component_stack": [{}]},
    {"category": {}}, {"route": None}, {"cookie": "secret"},
])
def test_validation(client, body):
    response = client.post("/api/client-errors", json=body, headers=login(client))
    assert response.status_code == 422
    assert "secret" not in response.text


def test_malformed_and_size_limit(client):
    headers = login(client)
    for raw, status in [(b"{", 422), (b"\xff", 422), (b"x" * 4097, 413)]:
        assert client.post("/api/client-errors", content=raw, headers=headers).status_code == status
    # Streaming body without relying on Content-Length.
    assert client.post("/api/client-errors", content=iter([b"x" * 2048] * 3), headers=headers).status_code == 413


def test_rate_limit_and_retention(client, isolated_engine):
    headers = login(client)
    for _ in range(10):
        assert client.post("/api/client-errors", json=payload(), headers=headers).status_code == 202
    assert client.post("/api/client-errors", json=payload(), headers=headers).status_code == 429
    with Session(isolated_engine) as db:
        db.query(ClientError).update({"created_at": datetime.now(timezone.utc) - timedelta(days=8)})
        db.commit()
    assert client.get("/api/admin/client-errors").json()["items"] == []
    with Session(isolated_engine) as db:
        assert db.scalar(select(func.count()).select_from(ClientError)) == 0
        db.add_all([ClientError(user_id=999, category="RenderError", message="A component failed to render.",
                               route="/", component_stack=[], created_at=datetime.now(timezone.utc) - timedelta(hours=1))
                    for _ in range(1005)])
        db.commit()
    assert client.post("/api/client-errors", json=payload(), headers=headers).status_code == 202
    with Session(isolated_engine) as db:
        assert db.scalar(select(func.count()).select_from(ClientError)) == 1000
    result = client.get("/api/admin/client-errors").json()
    assert len(result["items"]) == 100
    assert result["retention_days"] == 7
    assert result["items"][0]["profile_id"] == 42


def test_global_rate_limit(client, isolated_engine):
    headers = login(client)
    with Session(isolated_engine) as db:
        db.add_all([ClientError(user_id=999, category="RenderError", message="A component failed to render.",
                               route="/", component_stack=[]) for _ in range(100)])
        db.commit()
    assert client.post("/api/client-errors", json=payload(), headers=headers).status_code == 429