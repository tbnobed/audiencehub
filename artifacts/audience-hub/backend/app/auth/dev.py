import hashlib
import unicodedata
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.auth.deps import start_session
from app.config import get_settings
from app.db import session_scope
from app.models import User, now

router = APIRouter()


class DevLogin(BaseModel):
    role: str
    name: str = Field(default="Demo Analyst", min_length=1, max_length=80)


def normalized_name(name):
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


@router.post("/auth/dev-login")
def dev_login(body: DevLogin, request: Request, db: Session = Depends(session_scope)):
    if get_settings().auth_mode != "dev" or get_settings().app_env == "production":
        raise HTTPException(404)
    if body.role not in ("admin", "analyst", "viewer"):
        raise HTTPException(422, detail="Unknown role")
    name = normalized_name(body.name)
    if not name:
        raise HTTPException(422, detail="A development login name is required")
    subject = f"dev:{body.role}:{hashlib.sha256(name.encode()).hexdigest()}"
    user = db.scalar(select(User).where(User.subject == subject))
    if not user:
        legacy = db.scalar(select(User).where(User.subject == f"dev:{body.role}"))
        if legacy and normalized_name(legacy.name) == name:
            user = legacy
    if not user:
        user = User(subject=subject, email=f"{body.role}@example.com",
                    name=" ".join(unicodedata.normalize("NFKC", body.name).split()), role=body.role, is_active=True)
        db.add(user)
    if not user.is_active:
        raise HTTPException(403, detail="This development user is inactive")
    user.last_login_at = now()
    db.commit()
    db.refresh(user)
    start_session(request, user)
    return {"id": user.id, "name": user.name, "role": user.role}