import secrets
import time
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.db import session_scope
from app.models import User

ORDER = {"viewer": 0, "analyst": 1, "admin": 2}


def current_user(request: Request, db: Session = Depends(session_scope)) -> User:
    from app.config import get_settings
    if get_settings().auth_mode == "local":
        from sqlalchemy import text
        from app.auth.passwords import token_hash
        sid = request.session.get("sid", "")
        row = db.execute(text("""UPDATE local_sessions SET
            last_seen=CASE WHEN :activity THEN clock_timestamp() ELSE last_seen END
            WHERE token_hash=:hash AND last_seen>clock_timestamp()-interval '8 hours'
            RETURNING user_id,mfa_verified"""),
            {"hash": token_hash(sid), "activity": request.headers.get("X-Kinship-Activity", "1") == "1"}).mappings().first() if sid else None
        if not row:
            request.session.clear()
            raise HTTPException(401, "Sign in required")
        user = db.get(User, row["user_id"])
        if not user or not user.is_active or user.role not in ORDER:
            db.rollback()
            request.session.clear()
            raise HTTPException(401, "Account inactive")
        required = user.role == "admin" and db.scalar(text("SELECT require_admin_totp FROM operations_policy WHERE id=1"))
        enabled = db.scalar(text("SELECT totp_secret IS NOT NULL FROM local_credentials WHERE user_id=:id"), {"id": user.id})
        db.commit()
        request.state.mfa_enabled = bool(enabled)
        request.state.mfa_required = bool(required)
        # Enrollment-only sessions cannot access partner/admin data.
        if (required or enabled) and not row["mfa_verified"] and not (
            request.url.path in ("/api/me", "/auth/logout") or request.url.path.startswith("/api/account/")
        ):
            raise HTTPException(403, "Authenticator enrollment required")
        request.session["touched"] = int(time.time())
        return user
    uid = request.session.get("uid")
    issued = request.session.get("issued", 0)
    touched = request.session.get("touched", 0)
    if not uid or time.time() - touched > 8 * 3600:
        request.session.clear()
        raise HTTPException(401, detail="Sign in required")
    user = db.get(User, uid)
    if not user or not user.is_active or user.role not in ORDER:
        request.session.clear()
        raise HTTPException(401, detail="Account inactive")
    # Signed cookie session's idle timestamp is refreshed on each request.
    request.session["touched"] = int(time.time())
    return user


def require_role(minimum: str):
    def guard(user: User = Depends(current_user)) -> User:
        if ORDER.get(user.role, -1) < ORDER[minimum]:
            raise HTTPException(403, detail="Insufficient role")
        return user
    return guard


def start_session(request: Request, user: User):
    request.session.clear()
    request.session.update(uid=user.id, issued=int(time.time()),
                           touched=int(time.time()), csrf=secrets.token_urlsafe(32))


def check_csrf(request: Request):
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""),
                                  request.session.get("csrf", "missing")):
        raise HTTPException(403, detail="Invalid CSRF token")