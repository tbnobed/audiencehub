"""Admin-managed settings and safe settings exposed to dashboard viewers."""

import os
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.db import session_scope
from app.models import AuditLog, User

router = APIRouter(tags=["settings"])


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fiscal_year_start_month: Annotated[int, Field(strict=True, ge=1, le=12)] | None = None
    dashboard_default_preset: Literal["90d", "custom"] | None = None
    dashboard_default_from: str | None = None
    dashboard_default_to: str | None = None

    @field_validator("dashboard_default_from", "dashboard_default_to")
    @classmethod
    def valid_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Date must be an ISO YYYY-MM-DD string")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Date must be an ISO YYYY-MM-DD string") from exc
        if parsed.isoformat() != value or not 1900 <= parsed.year <= 2999:
            raise ValueError("Date must be an ISO YYYY-MM-DD string between 1900 and 2999")
        return value

    @model_validator(mode="after")
    def validate_date_range(self):
        fields = self.model_fields_set
        date_fields = {"dashboard_default_preset", "dashboard_default_from", "dashboard_default_to"}
        if fields & date_fields:
            if self.dashboard_default_preset == "custom":
                if not date_fields <= fields or not self.dashboard_default_from or not self.dashboard_default_to:
                    raise ValueError("Custom default requires preset, from and to together")
                start = date.fromisoformat(self.dashboard_default_from)
                end = date.fromisoformat(self.dashboard_default_to)
                if start > end or (end - start).days > 3660:
                    raise ValueError("Custom default must have start <= end and span at most 3660 days")
            elif self.dashboard_default_preset == "90d":
                if self.dashboard_default_from is not None or self.dashboard_default_to is not None:
                    raise ValueError("90d default cannot include custom dates")
            else:
                raise ValueError("Date changes require a 90d or custom preset")
        if "fiscal_year_start_month" in fields and self.fiscal_year_start_month is None:
            raise ValueError("Fiscal year start month cannot be null")
        return self


def _stored_settings(db: Session) -> dict:
    row = db.execute(
        text(
            "SELECT fiscal_year_start_month, dashboard_default_preset, "
            "dashboard_default_from, dashboard_default_to FROM admin_settings WHERE id = 1"
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(503, detail="Application settings are not initialized")
    month, preset, start, end = row
    return {
        "fiscal_year_start_month": int(month),
        "dashboard_default_preset": preset,
        "dashboard_default_from": start.isoformat() if start is not None else None,
        "dashboard_default_to": end.isoformat() if end is not None else None,
    }


def _settings_json(db: Session) -> dict:
    settings = _stored_settings(db)
    if settings["dashboard_default_preset"] is None:
        if os.environ.get("APP_ENV", "development") == "development":
            settings.update(dashboard_default_preset="custom",
                            dashboard_default_from="2020-01-01",
                            dashboard_default_to="2024-12-31")
        else:
            settings.update(dashboard_default_preset="90d",
                            dashboard_default_from=None, dashboard_default_to=None)
    return settings


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
    previous = _stored_settings(db)
    updated = previous.copy()
    if "fiscal_year_start_month" in body.model_fields_set:
        updated["fiscal_year_start_month"] = body.fiscal_year_start_month
    if "dashboard_default_preset" in body.model_fields_set:
        updated["dashboard_default_preset"] = body.dashboard_default_preset
        updated["dashboard_default_from"] = (
            body.dashboard_default_from if body.dashboard_default_preset == "custom" else None
        )
        updated["dashboard_default_to"] = (
            body.dashboard_default_to if body.dashboard_default_preset == "custom" else None
        )
    if updated != previous:
        db.execute(
            text(
                "UPDATE admin_settings SET fiscal_year_start_month = :month, "
                "dashboard_default_preset = :preset, dashboard_default_from = :start, "
                "dashboard_default_to = :end WHERE id = 1"
            ),
            {"month": updated["fiscal_year_start_month"],
             "preset": updated["dashboard_default_preset"],
             "start": date.fromisoformat(updated["dashboard_default_from"])
                      if updated["dashboard_default_from"] else None,
             "end": date.fromisoformat(updated["dashboard_default_to"])
                    if updated["dashboard_default_to"] else None},
        )
        if updated["fiscal_year_start_month"] != previous["fiscal_year_start_month"]:
            db.add(AuditLog(
                user_id=user.id, actor_type="user",
                action="settings.fiscal_year_start_month.update",
                entity_type="setting", entity_id="fiscal_year_start_month",
                details={"previous": previous["fiscal_year_start_month"],
                         "updated": updated["fiscal_year_start_month"]},
            ))
        if any(updated[key] != previous[key] for key in
               ("dashboard_default_preset", "dashboard_default_from", "dashboard_default_to")):
            db.add(AuditLog(
                user_id=user.id, actor_type="user",
                action="settings.dashboard_default.update",
                entity_type="setting", entity_id="dashboard_default",
                details={"previous": {key: previous[key] for key in
                                      ("dashboard_default_preset", "dashboard_default_from", "dashboard_default_to")},
                         "updated": {key: updated[key] for key in
                                     ("dashboard_default_preset", "dashboard_default_from", "dashboard_default_to")}},
            ))
        db.commit()
    return _settings_json(db)


@router.get("/api/dashboards/settings")
def get_dashboard_settings(
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(session_scope),
):
    return _settings_json(db)