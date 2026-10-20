"""Password-only sign-in cannot create an authenticated session for TOTP users."""
import hmac
import secrets
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import text
from app.auth.local import audit, credentials, establish, failed, login_throttle
from app.auth.passwords import token_hash
from app.auth.recovery import verify
from app.models import User, now


def fingerprint(row):
    return token_hash((row["password_hash"] or "") + ":" + bytes(row["totp_secret"] or b"").hex())


def cancel(db, request):
    token = request.session.pop("login_challenge", "")
    if token:
        db.execute(text("DELETE FROM local_login_challenges WHERE token_hash=:hash"), {"hash": token_hash(token)})
    db.commit()
    return {"ok": True}


def begin(db, request, user, row):
    previous = request.session.get("login_challenge")
    if previous:
        db.execute(text("DELETE FROM local_login_challenges WHERE token_hash=:hash"), {"hash": token_hash(previous)})
    token = secrets.token_urlsafe(48)
    expires = now() + timedelta(minutes=5)
    db.execute(text("""INSERT INTO local_login_challenges
        (token_hash,user_id,credential_fingerprint,expires_at) VALUES(:hash,:id,:fingerprint,:expires)"""),
        {"hash": token_hash(token), "id": user.id, "fingerprint": fingerprint(row), "expires": expires})
    audit(db, "auth.password_verified", user.id, {"second_factor_pending": True})
    db.commit()
    csrf = request.session.get("csrf") or secrets.token_urlsafe(32)
    request.session.clear()
    # No uid, sid or authenticated local_sessions row until factor verification.
    request.session.update(csrf=csrf, login_challenge=token)
    return {"mfa_required": True, "expires_at": expires.isoformat()}


def complete(db, request, code):
    limit_key = login_throttle(db, request)
    token = request.session.get("login_challenge", "")
    hashed = token_hash(token)
    user_id = db.scalar(text("SELECT user_id FROM local_login_challenges WHERE token_hash=:hash"), {"hash": hashed})
    if not user_id:
        failed(db, None, limit_key, "invalid_second_factor_challenge", status_code=410,
               message="This sign-in request has expired or was cancelled. Close this window and sign in again.")
    # Same lock order as password login / administrative credential changes.
    row = credentials(db, user_id)
    challenge = db.execute(text("""SELECT * FROM local_login_challenges
        WHERE token_hash=:hash FOR UPDATE"""), {"hash": hashed}).mappings().first()
    user = db.get(User, user_id)
    if (not challenge or challenge["expires_at"] <= now() or not user or not user.is_active
            or not row["totp_secret"]
            or not hmac.compare_digest(challenge["credential_fingerprint"], fingerprint(row))):
        db.execute(text("DELETE FROM local_login_challenges WHERE token_hash=:hash"), {"hash": hashed})
        request.session.pop("login_challenge", None)
        failed(db, None, limit_key, "expired_or_revoked_second_factor_challenge", status_code=410,
               message="This sign-in request has expired or was revoked. Close this window and sign in again.")
    if row["locked_until"] and row["locked_until"] > now():
        audit(db, "auth.login_failed", user.id, {"reason": "account_cooldown"})
        db.commit()
        raise HTTPException(429, "Account locked. Wait 15 minutes or ask an administrator to unlock, then sign in again.")
    if row["locked_until"]:
        db.execute(text("UPDATE local_credentials SET failures=0,locked_until=NULL WHERE user_id=:id"), {"id": user.id})
    if not verify(db, user.id, row, code):
        failed(db, user.id, limit_key, "invalid_second_factor",
               message="Authenticator or recovery code is incorrect. Try again.")
    if len(code) != 6:
        audit(db, "auth.recovery_code_used", user.id)
    db.execute(text("DELETE FROM local_login_challenges WHERE token_hash=:hash"), {"hash": hashed})
    db.execute(text("UPDATE local_credentials SET failures=0,locked_until=NULL WHERE user_id=:id"), {"id": user.id})
    db.execute(text("UPDATE local_login_limits SET failures=0,locked_until=NULL WHERE key_hash=:key"), {"key": limit_key})
    establish(db, request, user, True)
    return {"ok": True}
