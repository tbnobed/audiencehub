"""M8 regression definitions. Run only on a disposable server-side test database."""
from types import SimpleNamespace
import pytest
from fastapi import FastAPI, Depends, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware
from test_consent_ingestion_pg import db
from app.auth import local
from app.auth.passwords import validate_password, hash_password, check_password, totp_counter
from app.auth.deps import current_user
from app.config import get_settings
from app.db import session_scope
from app.models import User
from app.operations import api

PASSWORD = "A uniquely chosen test phrase 746!"


@pytest.fixture
def local_client(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    user = User(subject="local:m8", email="admin@example.test", name="Admin", role="admin", is_active=True)
    db.add(user)
    db.flush()
    db.execute(text("INSERT INTO local_credentials(user_id,password_hash) VALUES(:id,:hash)"),
               {"id": user.id, "hash": hash_password(PASSWORD)})
    db.commit()
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="m8-test-only-session-signature-key")
    app.include_router(local.router)
    app.include_router(api.router)
    app.dependency_overrides[session_scope] = lambda: db
    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return {"id": user.id}
    client = TestClient(app)
    csrf = client.get("/auth/local/session").json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    return client, user


def test_password_floor_denylist_and_scrypt():
    with pytest.raises(ValueError):
        validate_password("too short")
    with pytest.raises(ValueError):
        validate_password("password123456789")
    validate_password(PASSWORD)
    encoded = hash_password(PASSWORD)
    assert encoded != PASSWORD and check_password(PASSWORD, encoded)
    assert not check_password(PASSWORD+"x", encoded)


def test_totp_rfc_vector_and_replay():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_counter(secret, "287082", now=59) == 1
    assert totp_counter(secret, "287082", previous=1, now=59) is None
    assert totp_counter(secret, "abcdef", now=59) is None


def test_ten_failures_lock_and_admin_unlock(local_client, db):
    client, user = local_client
    for _ in range(10):
        assert client.post("/auth/local/login", json={"email": user.email, "password": "wrong"}).status_code == 401
    assert db.scalar(text("SELECT locked_until>now() FROM local_credentials WHERE user_id=:id"), {"id": user.id})
    assert db.scalar(text("SELECT count(*) FROM audit_log WHERE action='auth.lockout'")) == 1
    api.unlock(user.id, user, db)
    assert db.scalar(text("SELECT failures FROM local_credentials WHERE user_id=:id"), {"id": user.id}) == 0


def test_password_change_revokes_all_sessions(local_client, db):
    client, user = local_client
    assert client.post("/auth/local/login", json={"email": user.email, "password": PASSWORD}).status_code == 200
    assert client.get("/api/me").status_code == 200
    changed = local.change_password(local.Password(current_password=PASSWORD, new_password=PASSWORD+" updated"),
        SimpleNamespace(session={}), user, db)
    assert changed == {"ok": True}
    assert client.get("/api/me").status_code == 401
    assert db.scalar(text("SELECT count(*) FROM local_sessions")) == 0


def test_idle_session_expires_on_server(local_client, db):
    client, user = local_client
    client.post("/auth/local/login", json={"email": user.email, "password": PASSWORD})
    db.execute(text("UPDATE local_sessions SET last_seen=now()-interval '8 hours 1 second'"))
    db.commit()
    assert client.get("/api/me").status_code == 401


def test_anonymous_checks_preserve_login_csrf(local_client):
    client, user = local_client
    csrf = client.headers["X-CSRF-Token"]
    for _ in range(3):
        assert client.get("/api/me").status_code == 401
    assert client.get("/auth/local/session").json()["csrf_token"] == csrf
    assert client.post("/auth/local/login", json={"email": user.email, "password": PASSWORD}).status_code == 200
    assert client.get("/api/me").status_code == 200


def test_csrf_is_still_required_for_login(local_client):
    client, user = local_client
    assert client.post("/auth/local/login", headers={"X-CSRF-Token": "incorrect"},
                       json={"email": user.email, "password": PASSWORD}).status_code == 403
    assert client.post("/auth/local/login", json={"email": user.email, "password": PASSWORD}).status_code == 200


def test_expired_session_keeps_csrf_for_reauthentication(local_client, db):
    client, user = local_client
    assert client.post("/auth/local/login", json={"email": user.email, "password": PASSWORD}).status_code == 200
    csrf = client.get("/auth/local/session").json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    db.execute(text("UPDATE local_sessions SET last_seen=now()-interval '8 hours 1 second'"))
    db.commit()
    assert client.get("/api/me").status_code == 401
    assert client.get("/auth/local/session").json()["csrf_token"] == csrf
    assert client.post("/auth/local/login", json={"email": user.email, "password": PASSWORD}).status_code == 200


def test_last_admin_cannot_be_demoted(local_client, db):
    _, user = local_client
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        api.change(user.id, api.Change(role="viewer"), user, db)


def test_invite_token_single_use_and_no_secret_in_audit(local_client, db):
    client, user = local_client
    issued = api.invite(api.Invite(email="invited@example.test", name="Invited", role="viewer"), user, db)
    assert issued["token"] not in str(db.execute(text("SELECT details FROM audit_log")).all())
    assert client.post("/auth/local/accept", json={"token": issued["token"], "password": PASSWORD}).status_code == 200
    client.get("/auth/local/session")
    # Direct endpoint invocation avoids using the now-rotated CSRF token.
    with pytest.raises(HTTPException):
        local.accept(local.Accept(token=issued["token"], password=PASSWORD), SimpleNamespace(
            headers={"X-CSRF-Token": "test"}, session={"csrf": "test"}), db)


def test_csv_formula_escape():
    assert api.csv_cell("=HYPERLINK(\"bad\")").startswith("'")
    assert api.csv_cell("ordinary") == "ordinary"


def test_production_guard_requires_flag_and_exact_name(monkeypatch):
    from app.operations.guards import production_guard
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValueError, match="explicit"):
        production_guard(None)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def scalar(self, query): return "audience_hub"
    bind = SimpleNamespace(connect=lambda: Connection())
    with pytest.raises(ValueError, match="does not match"):
        production_guard(bind, allow=True, database="different")
    production_guard(bind, allow=True, database="audience_hub")


def test_old_pepper_tombstones_still_block_ingestion(db, monkeypatch):
    from app.keyring import pii_hashes
    from app.ingestion.service import suppressed
    settings = get_settings()
    monkeypatch.setattr(settings, "pii_hash_pepper", "old-privacy-key")
    hashed = pii_hashes("blocked@example.test")[0]
    db.execute(text("INSERT INTO suppressions(type,value_hash,reason) VALUES('email',:hash,'deletion_request')"), {"hash": hashed})
    monkeypatch.setattr(settings, "pii_hash_pepper", "new-privacy-key")
    monkeypatch.setattr(settings, "pii_hash_peppers_previous", "old-privacy-key")
    assert suppressed(db, {("email", "blocked@example.test")})


def test_fernet_rotation_preserves_old_reads_and_new_writes(monkeypatch):
    from cryptography.fernet import Fernet
    from app.keyring import fernet_ring
    from app.activations.security import encrypt, decrypt
    settings = get_settings()
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "fernet_key", old)
    ciphertext = encrypt({"private": "test-only"})
    monkeypatch.setattr(settings, "fernet_key", new)
    monkeypatch.setattr(settings, "fernet_keys_previous", old)
    assert decrypt(ciphertext) == {"private": "test-only"}
    rotated = fernet_ring().rotate(ciphertext)
    assert Fernet(new.encode()).decrypt(rotated) == Fernet(old.encode()).decrypt(ciphertext)
    assert Fernet(new.encode()).decrypt(encrypt({"private": "new"}))


def test_audit_filter_and_csv(local_client, db):
    _, user = local_client
    local.audit(db, "auth.login", user.id)
    local.audit(db, "auth.login_failed", user.id)
    db.commit()
    filters = dict(user_id=user.id, action="auth.login", object="", from_date=None, to_date=None)
    result = api.audit_log(**filters, before_id=None, limit=100, user=user, db=db)
    assert result["items"] and all(row["action"] == "auth.login" for row in result["items"])
    response = api.export_audit(**filters, user=user, db=db)
    assert b"auth.login" in response.body and b"auth.login_failed" not in response.body


def test_empty_database_retention_queries(db):
    from app.operations.maintenance import clean_batch
    policy = dict(db.execute(text("SELECT * FROM operations_policy WHERE id=1")).mappings().one())
    for stage in ("licensed_sources", "enrichment", "events", "audit", "jobs", "sessions"):
        assert clean_batch(db, stage, policy, 0) == 0
