import secrets
import time
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.db import session_scope
from app.models import User

ORDER = {"viewer": 0, "analyst": 1, "admin": 2}


def current_user(request: Request, db: Session = Depends(session_scope)) -> User:
    uid = request.session.get("uid")
    issued = request.session.get("issued", 0)
    touched = request.session.get("touched", 0)
    if not uid or time.time() - issued > 8 * 3600 or time.time() - touched > 3600:
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