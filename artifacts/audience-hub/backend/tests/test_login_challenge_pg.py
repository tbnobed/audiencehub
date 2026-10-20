"""Server-only definitions for the password-then-popup sign-in flow."""
from sqlalchemy import text
from app.auth.recovery import replace_codes
from app.activations.security import encrypt
from app.operations import api
from test_m8_operations_pg import db, local_client, PASSWORD


def enable_factor(db, user):
    db.execute(text("UPDATE local_credentials SET totp_secret=:secret WHERE user_id=:id"),
               {"id": user.id, "secret": encrypt({"secret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"})})
    codes = replace_codes(db, user.id)
    db.commit()
    return codes


def password_step(client, user, password=PASSWORD):
    return client.post("/auth/local/login", json={"email": user.email, "password": password})


def test_user_without_totp_signs_in_directly(local_client, db):
    client, user = local_client
    result = password_step(client, user)
    assert result.status_code == 200 and result.json() == {"ok": True}
    assert client.get("/api/me").status_code == 200
    assert db.scalar(text("SELECT count(*) FROM local_login_challenges")) == 0


def test_correct_password_only_opens_challenge_not_authenticated_session(local_client, db):
    client, user = local_client
    enable_factor(db, user)
    result = password_step(client, user)
    assert result.status_code == 200 and result.json()["mfa_required"] is True
    assert "expires_at" in result.json()
    assert client.get("/api/me").status_code == 401
    assert db.scalar(text("SELECT count(*) FROM local_sessions")) == 0
    assert db.scalar(text("SELECT failures FROM local_credentials WHERE user_id=:id"), {"id": user.id}) == 0
    assert db.scalar(text("SELECT count(*) FROM audit_log WHERE action='auth.login'")) == 0


def test_wrong_password_does_not_reveal_totp_status(local_client, db):
    client, user = local_client
    enable_factor(db, user)
    response = password_step(client, user, "wrong")
    assert response.status_code == 401 and "mfa_required" not in response.json()
    assert db.scalar(text("SELECT count(*) FROM local_login_challenges")) == 0


def test_challenge_verification_is_single_use_including_signed_cookie_replay(local_client, db):
    client, user = local_client
    codes = enable_factor(db, user)
    assert password_step(client, user).json()["mfa_required"]
    original_cookie = client.cookies.get("session")
    csrf = client.headers["X-CSRF-Token"]
    response = client.post("/auth/local/totp", json={"code": codes[0]})
    assert response.status_code == 200 and response.json()["ok"]
    assert client.get("/api/me").status_code == 200
    assert db.scalar(text("SELECT count(*) FROM local_login_challenges")) == 0
    assert db.scalar(text("SELECT count(*) FROM local_sessions WHERE mfa_verified")) == 1
    client.cookies.clear()
    client.cookies.set("session", original_cookie)
    client.headers["X-CSRF-Token"] = csrf
    assert client.post("/auth/local/totp", json={"code": codes[1]}).status_code == 410
    assert client.get("/api/me").status_code == 401
    assert db.scalar(text("SELECT count(*) FROM local_sessions")) == 1


def test_expiry_and_cancellation_require_password_again(local_client, db):
    client, user = local_client
    codes = enable_factor(db, user)
    password_step(client, user)
    db.execute(text("UPDATE local_login_challenges SET expires_at=now()-interval '1 second'"))
    db.commit()
    assert client.post("/auth/local/totp", json={"code": codes[0]}).status_code == 410
    assert client.get("/api/me").status_code == 401
    password_step(client, user)
    assert client.post("/auth/local/totp/cancel").status_code == 200
    assert db.scalar(text("SELECT count(*) FROM local_login_challenges")) == 0
    assert client.post("/auth/local/totp", json={"code": codes[0]}).status_code == 410


def test_failed_codes_share_account_lockout_and_cannot_bypass_csrf(local_client, db):
    client, user = local_client
    codes = enable_factor(db, user)
    password_step(client, user)
    assert client.post("/auth/local/totp", headers={"X-CSRF-Token": "wrong"}, json={"code": codes[0]}).status_code == 403
    for _ in range(10):
        assert client.post("/auth/local/totp", json={"code": "not-a-code"}).status_code == 401
    assert client.post("/auth/local/totp", json={"code": codes[0]}).status_code == 429
    assert client.get("/api/me").status_code == 401
    assert db.scalar(text("SELECT count(*) FROM local_sessions")) == 0


def test_password_reset_revokes_pending_challenge(local_client, db):
    client, user = local_client
    codes = enable_factor(db, user)
    password_step(client, user)
    api.reset(user.id, api.Reset(), user, db)
    assert client.post("/auth/local/totp", json={"code": codes[0]}).status_code == 410


def test_disabled_account_cannot_complete_challenge(local_client, db):
    client, user = local_client
    codes = enable_factor(db, user)
    password_step(client, user)
    user.is_active = False
    db.commit()
    assert client.post("/auth/local/totp", json={"code": codes[0]}).status_code == 410
    assert db.scalar(text("SELECT count(*) FROM local_sessions")) == 0


def test_cli_combined_credentials_still_work(local_client, db):
    client, user = local_client
    codes = enable_factor(db, user)
    response = client.post("/auth/local/login", json={"email": user.email, "password": PASSWORD, "totp": codes[0]})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert client.get("/api/me").status_code == 200
