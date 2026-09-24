"""Admin-managed settings and safe settings exposed to dashboard viewers."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.db import session_scope
from app.models import AuditLog, User

router = APIRouter(tags=["settings"])


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fiscal_year_start_month: Annotated[int, Field(strict=True, ge=1, le=12)]


def _fiscal_year_start_month(db: Session) -> int:
    value = db.execute(
        text("SELECT fiscal_year_start_month FROM admin_settings WHERE id = 1")
    ).scalar_one_or_none()
    if value is None:
        raise HTTPException(503, detail="Application settings are not initialized")
    return int(value)


def _settings_json(db: Session) -> dict[str, int]:
    return {"fiscal_year_start_month": _fiscal_year_start_month(db)}


@router.get("/api/admin/settings")
def get_admin_settings(
    user: User = Depends(require_role("admin")),
    db: Session = Depends(session_scope),
):
    return _settings_json(db)


@router.patch("/api/admin/settings")
def update_admin_settings(
    body: SettingsUpdate,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(session_scope),
):
    previous = _fiscal_year_start_month(db)
    updated = body.fiscal_year_start_month
    if updated != previous:
        db.execute(
            text(
                "UPDATE admin_settings SET fiscal_year_start_month = :month WHERE id = 1"
            ),
            {"month": updated},
        )
        db.add(
            AuditLog(
                user_id=user.id,
                actor_type="user",
                action="settings.fiscal_year_start_month.update",
                entity_type="setting",
                entity_id="fiscal_year_start_month",
                details={"previous": previous, "updated": updated},
            )
        )
        db.commit()
    return {"fiscal_year_start_month": updated}


@router.get("/api/dashboards/settings")
def get_dashboard_settings(
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(session_scope),
):
    return _settings_json(db)