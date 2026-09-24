from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.auth.deps import start_session
from app.config import get_settings
from app.db import session_scope
from app.models import User, now

router = APIRouter()


class DevLogin(BaseModel):
    role: str
    name: str = "Demo Analyst"


@router.post("/auth/dev-login")
def dev_login(body: DevLogin, request: Request, db: Session = Depends(session_scope)):
    if get_settings().auth_mode != "dev":
        raise HTTPException(404)
    if body.role not in ("admin", "analyst", "viewer"):
        raise HTTPException(422, detail="Unknown role")
    subject = f"dev:{body.role}"
    user = db.scalar(select(User).where(User.subject == subject))
    if not user:
        user = User(subject=subject, email=f"{body.role}@example.com",
                    name=body.name[:80], role=body.role, is_active=True)
        db.add(user)
    user.last_login_at = now()
    db.commit()
    db.refresh(user)
    start_session(request, user)
    return {"id": user.id, "name": user.name, "role": user.role}