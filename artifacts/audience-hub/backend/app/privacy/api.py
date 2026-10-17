from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from app.auth.deps import require_role
from app.db import session_scope
from app.config import get_settings
from app.privacy import service

router = APIRouter(prefix="/api", tags=["privacy"])
admin = Depends(require_role("admin"))
viewer = Depends(require_role("viewer"))
session = Depends(session_scope)


class IdentifierBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["email", "phone"]
    value: str = Field(min_length=1, max_length=320)


class ConsentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["email", "sms", "phone", "mail", "ads_personalization"]
    status: Literal["opted_in", "opted_out", "unknown"]
    note: str = Field(min_length=10, max_length=2000)


def checked(fn, *args):
    try:
        return fn(*args)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def public_request(row):
    return {key: row[key] for key in (
        "id", "requested_by", "approved_by", "status", "created_at", "approved_at",
        "executed_at", "result_counts", "error")}


@router.get("/profiles/{profile_id}/consents")
def consents(profile_id: int, db=session, user=viewer):
    return {"items": checked(service.partner_consents, db, profile_id)}


@router.post("/profiles/{profile_id}/consents")
def change_consent(profile_id: int, body: ConsentBody, db=session, user=admin):
    result = checked(service.change_consent, db, profile_id, body.channel, body.status, body.note, user.id)
    db.commit()
    return result


@router.get("/admin/suppressions")
def suppressions(db=session, user=admin):
    return {"items": [dict(r) for r in db.execute(text(
        "SELECT id,type,value_hash,reason,created_at FROM suppressions ORDER BY id DESC LIMIT 500"
    )).mappings()]}


@router.post("/admin/suppressions")
def add_suppression(body: IdentifierBody, db=session, user=admin):
    result = checked(service.add_suppression, db, body.type, body.value, user.id)
    db.commit()
    return result


@router.delete("/admin/suppressions/{suppression_id}")
def remove_suppression(suppression_id: int, db=session, user=admin):
    checked(service.remove_suppression, db, suppression_id, user.id)
    db.commit()
    return {"removed": True}


@router.get("/admin/deletion-requests")
def requests(db=session, user=admin):
    return {"items": [public_request(r) for r in db.execute(text(
        "SELECT * FROM deletion_requests ORDER BY id DESC LIMIT 500"
    )).mappings()], "two_person": get_settings().deletion_two_person,
        "backup_keep_days": get_settings().backup_keep_days}


@router.post("/admin/deletion-requests/preview")
def preview(body: IdentifierBody, db=session, user=admin):
    return checked(service.preview_deletion, db, body.type, body.value)


@router.post("/admin/deletion-requests")
def create_request(body: IdentifierBody, db=session, user=admin):
    result = checked(service.create_deletion, db, body.type, body.value, user.id)
    db.commit()
    return public_request(result)


@router.post("/admin/deletion-requests/{request_id}/approve")
def approve(request_id: int, db=session, user=admin):
    job_id = checked(service.approve_deletion, db, request_id, user.id)
    result = db.execute(text("SELECT * FROM deletion_requests WHERE id=:id"), {"id": request_id}).mappings().one()
    db.commit()
    return {"job_id": job_id, "request": public_request(result)}
