import base64
import hashlib
import hmac
import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet
from app.dev_bootstrap import development_keys


def test_development_keys_stable_domain_separated_and_override_safe():
    environment = {"SESSION_SECRET": "fixture-only-master"}
    first = development_keys(environment)
    assert first == development_keys(dict(environment))
    assert first["SECRET_KEY"] == hmac.new(b"fixture-only-master", b"kinship:development:v1:SECRET_KEY", hashlib.sha256).hexdigest()
    assert len({bytes.fromhex(first["SECRET_KEY"]), bytes.fromhex(first["PII_HASH_PEPPER"]),
                base64.urlsafe_b64decode(first["FERNET_KEY"])}) == 3
    encrypted = Fernet(first["FERNET_KEY"]).encrypt(b"fixture-only-value")
    assert Fernet(development_keys(environment)["FERNET_KEY"]).decrypt(encrypted) == b"fixture-only-value"
    explicit = {"SECRET_KEY": "explicit-secret", "FERNET_KEY": "explicit-fernet", "PII_HASH_PEPPER": "explicit-pepper"}
    assert development_keys(explicit) == explicit
    assert development_keys({**environment, "SECRET_KEY": "explicit"})["SECRET_KEY"] == "explicit"
    with pytest.raises(ValueError, match="stable SESSION_SECRET"):
        development_keys({})
    with pytest.raises(ValueError, match="production"):
        development_keys({**environment, "APP_ENV": "production"})


def test_settings_validation_never_formats_secret_inputs():
    from app.config import Settings
    values = ("fixture-secret-not-in-errors", "fixture-fernet-not-in-errors", "fixture-pepper-not-in-errors")
    with pytest.raises(ValueError) as error:
        Settings(_env_file=None, database_url="postgresql://fixture/unused",
                 secret_key=values[0], fernet_key=values[1], pii_hash_pepper=values[2],
                 app_env="production", auth_mode="dev")
    assert all(value not in str(error.value) for value in values)


@pytest.mark.parametrize("mode", ["derived", "explicit", "missing", "production"])
def test_dev_script_uses_only_fixture_environment_and_never_launches_services(tmp_path, mode):
    # This shim aborts at the first would-be alembic call. No DB/app is started.
    shim = tmp_path / "python3"
    shim.write_text(f"#!{sys.executable}\n" + """
import os, runpy, sys
if sys.argv[1:3] == ['-m', 'app.dev_bootstrap']:
    sys.argv = ['app.dev_bootstrap', *sys.argv[3:]]
    runpy.run_module('app.dev_bootstrap', run_name='__main__')
else:
    assert sys.argv[1:3] == ['-m', 'alembic']
    from app.dev_bootstrap import development_keys
    expected = development_keys({'SESSION_SECRET': 'fixture-only-master'}) if os.environ['FIXTURE_MODE'] == 'derived' else {
        'SECRET_KEY': 'explicit-secret', 'FERNET_KEY': 'explicit-fernet', 'PII_HASH_PEPPER': 'explicit-pepper'}
    assert all(os.environ.get(key) == value for key, value in expected.items())
    print('fixture keys verified; services not started')
    sys.exit(74)
""")
    shim.chmod(0o700)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path),
           "DATABASE_URL": "postgresql://fixture/unused", "FIXTURE_MODE": mode,
           "UPLOAD_DIR": str(tmp_path / "uploads"), "EXPORT_DIR": str(tmp_path / "exports")}
    if mode == "derived":
        env["SESSION_SECRET"] = "fixture-only-master"
    if mode == "explicit":
        env.update(SECRET_KEY="explicit-secret", FERNET_KEY="explicit-fernet", PII_HASH_PEPPER="explicit-pepper")
    if mode == "production":
        env["APP_ENV"] = "production"
    script = Path(__file__).resolve().parents[2] / "scripts" / "dev.sh"
    results = [subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True) for _ in range(2)]
    for result in results:
        assert result.returncode == (74 if mode in ("derived", "explicit") else 1)
        assert all(value not in result.stdout + result.stderr for value in (
            "fixture-only-master", "explicit-secret", "explicit-fernet", "explicit-pepper"))
        if mode == "missing":
            assert "stable SESSION_SECRET" in result.stderr
        if mode == "production":
            assert "must not run in production" in result.stderr


@pytest.mark.skipif(os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
                    reason="Requires disposable PostgreSQL fixture")
def test_named_development_admins_complete_two_person_approval(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware
    from sqlalchemy import text
    from app.auth.dev import router as login_router
    from app.config import get_settings
    from app.db import session_scope
    from app.models import User
    from app.privacy.api import router as privacy_router
    monkeypatch.setattr(get_settings(), "auth_mode", "dev")
    monkeypatch.setattr(get_settings(), "app_env", "development")
    legacy = User(subject="dev:admin", name="Legacy Admin", email="legacy@example.org", role="admin", is_active=True)
    db.add(legacy); db.commit()
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="fixture-only-session")
    app.include_router(login_router); app.include_router(privacy_router)
    app.dependency_overrides[session_scope] = lambda: db
    with TestClient(app) as client:
        first = client.post("/auth/dev-login", json={"role": "admin", "name": " Legacy   ADMIN "}).json()
        assert first["id"] == legacy.id
        request = client.post("/api/admin/deletion-requests", json={"type": "email", "value": "synthetic@example.org"})
        assert request.status_code in (200, 201), request.text
        request_id = request.json()["id"]
        assert client.post(f"/api/admin/deletion-requests/{request_id}/approve").status_code == 422
        second = client.post("/auth/dev-login", json={"role": "admin", "name": "Second Admin"}).json()
        assert second["id"] != first["id"]
        assert client.post("/auth/dev-login", json={"role": "admin", "name": " second  ADMIN "}).json()["id"] == second["id"]
        assert client.post(f"/api/admin/deletion-requests/{request_id}/approve").status_code == 200
        assert db.scalar(text("SELECT approved_by FROM deletion_requests WHERE id=:id"), {"id": request_id}) == second["id"]
        client.post("/auth/dev-login", json={"role": "viewer", "name": "Second Admin"})
        assert client.get("/api/admin/deletion-requests").status_code == 403
        assert client.post("/auth/dev-login", json={"role": "owner", "name": "X"}).status_code == 422
        monkeypatch.setattr(get_settings(), "app_env", "production")
        assert client.post("/auth/dev-login", json={"role": "admin", "name": "X"}).status_code == 404
        monkeypatch.setattr(get_settings(), "app_env", "development")
        monkeypatch.setattr(get_settings(), "auth_mode", "oidc")
        assert client.post("/auth/dev-login", json={"role": "admin", "name": "X"}).status_code == 404


from test_consent_ingestion_pg import db  # disposable, schema-scoped PostgreSQL only
