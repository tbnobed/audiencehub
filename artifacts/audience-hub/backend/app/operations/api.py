import csv
import io
import json
from datetime import date, timedelta
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.auth.deps import require_role
from app.auth.local import audit, credentials, issue_token, revoke, status
from app.db import session_scope
from app.models import User
from app.jobs.queue import enqueue

router = APIRouter(prefix="/api/admin", tags=["operations"])
admin = Depends(require_role("admin"))
session = Depends(session_scope)
ROLE = Literal["admin", "analyst", "viewer"]


class Invite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    name: str = Field(min_length=1, max_length=200)
    role: ROLE = "viewer"


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: ROLE | None = None
    is_active: bool | None = None


class Reset(BaseModel):
    reset_totp: bool = False


class Policy(BaseModel):
    require_admin_totp: bool


class Maintenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_retention_days: int = Field(ge=0, le=36500)
    audit_retention_days: int = Field(ge=0, le=36500)
    job_retention_days: int = Field(ge=0, le=36500)
    enrichment_retention_days: int = Field(ge=0, le=36500)
    partitions_ahead_months: int = Field(ge=1, le=24)


def admin_lock(db):
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('kinship:admin-users'))"))


def target(db, key):
    result = db.get(User, key)
    if result is None:
        raise HTTPException(404, "User not found")
    return result


@router.get("/users")
def users(user: User = admin, db: Session = session):
    rows = db.execute(text("""SELECT u.id,u.email,u.name,u.role,u.is_active,u.last_login_at,
        c.locked_until,c.totp_secret IS NOT NULL AS mfa_enabled,
        c.password_hash IS NOT NULL AS password_set
        FROM users u LEFT JOIN local_credentials c ON c.user_id=u.id ORDER BY u.id""")).mappings()
    return {"items": [dict(row) for row in rows]}


@router.post("/users")
def invite(body: Invite, user: User = admin, db: Session = session):
    admin_lock(db)
    email = str(body.email).strip().lower()
    if db.scalar(text("SELECT id FROM users WHERE lower(email)=:email"), {"email": email}):
        raise HTTPException(409, "A user with this email already exists")
    saved = User(subject="local:" + str(uuid4()), email=email, name=body.name.strip(), role=body.role, is_active=True)
    db.add(saved)
    db.flush()
    credentials(db, saved.id)
    result = issue_token(db, saved.id, "invite")
    audit(db, "admin.user_invited", user.id, {"target_user_id": saved.id, "role": body.role})
    db.commit()
    return result


@router.patch("/users/{user_id}")
def change(user_id: int, body: Change, user: User = admin, db: Session = session):
    admin_lock(db)
    saved = target(db, user_id)
    next_role = body.role if body.role is not None else saved.role
    next_active = body.is_active if body.is_active is not None else saved.is_active
    if saved.role == "admin" and saved.is_active and (next_role != "admin" or not next_active):
        remaining = db.scalar(text("SELECT count(*) FROM users WHERE role='admin' AND is_active AND id<>:id"), {"id": user_id})
        if not remaining:
            raise HTTPException(409, "Cannot disable or demote the last active administrator")
    if user_id == user.id and not next_active:
        raise HTTPException(409, "Use another administrator to disable your account")
    previous = {"role": saved.role, "is_active": saved.is_active}
    saved.role, saved.is_active = next_role, next_active
    revoke(db, user_id)
    if not next_active:
        db.execute(text("DELETE FROM local_account_tokens WHERE user_id=:id"), {"id": user_id})
    audit(db, "admin.role_changed" if previous["role"] != next_role else "admin.user_updated",
          user.id, {"target_user_id": user_id, "before": previous,
                    "after": {"role": next_role, "is_active": next_active}})
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/unlock")
def unlock(user_id: int, user: User = admin, db: Session = session):
    target(db, user_id)
    credentials(db, user_id)
    db.execute(text("UPDATE local_credentials SET failures=0,locked_until=NULL WHERE user_id=:id"), {"id": user_id})
    audit(db, "admin.user_unlocked", user.id, {"target_user_id": user_id})
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/reset")
def reset(user_id: int, body: Reset, user: User = admin, db: Session = session):
    admin_lock(db)
    saved = target(db, user_id)
    if not saved.is_active:
        raise HTTPException(409, "Enable the account before issuing a reset")
    credentials(db, user_id)
    revoke(db, user_id)
    db.execute(text("UPDATE local_credentials SET password_hash=NULL,failures=0,locked_until=NULL WHERE user_id=:id"), {"id": user_id})
    if body.reset_totp:
        db.execute(text("""UPDATE local_credentials SET totp_secret=NULL,pending_totp=NULL,
            pending_totp_until=NULL,totp_counter=-1 WHERE user_id=:id"""), {"id": user_id})
    result = issue_token(db, user_id)
    audit(db, "admin.password_reset_issued", user.id, {"target_user_id": user_id, "reset_totp": body.reset_totp})
    db.commit()
    return result


@router.get("/security-policy")
def security_policy(user: User = admin, db: Session = session):
    return {"require_admin_totp": bool(db.scalar(text("SELECT require_admin_totp FROM operations_policy WHERE id=1")))}


@router.patch("/security-policy")
def set_security_policy(body: Policy, user: User = admin, db: Session = session):
    if body.require_admin_totp and not status(db, user)["mfa_enabled"]:
        raise HTTPException(409, "Enroll your own authenticator before requiring it for administrators")
    db.execute(text("UPDATE operations_policy SET require_admin_totp=:required WHERE id=1"), {"required": body.require_admin_totp})
    audit(db, "admin.security_policy_changed", user.id, body.model_dump())
    db.commit()
    return body.model_dump()


def audit_query(user_id, action, object, from_date, to_date, before_id, limit):
    clauses, params = ["true"], {"limit": limit}
    for name, value, clause in [
        ("user", user_id, "user_id=:user"), ("action", action, "action=:action"),
        ("from", from_date, "at>=:from"), ("to", to_date + timedelta(days=1) if to_date else None, "at<:to"),
        ("before", before_id, "id<:before"),
    ]:
        if value is not None and value != "":
            clauses.append(clause)
            params[name] = value
    if object:
        kind, _, key = object.partition(":")
        clauses.append("entity_type=:kind")
        params["kind"] = kind
        if key:
            clauses.append("entity_id=:entity")
            params["entity"] = key
    return text("SELECT * FROM audit_log WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT :limit"), params


@router.get("/audit")
def audit_log(user_id: int | None = None, action: str = "", object: str = "",
              from_date: date | None = None, to_date: date | None = None,
              before_id: int | None = None, limit: int = Query(100, ge=1, le=500),
              user: User = admin, db: Session = session):
    query, params = audit_query(user_id, action, object, from_date, to_date, before_id, limit+1)
    rows = [dict(row) for row in db.execute(query, params).mappings()]
    return {"items": rows[:limit], "next_cursor": rows[limit-1]["id"] if len(rows) > limit else None}


def csv_cell(value):
    value = "" if value is None else str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


@router.get("/audit/export")
def export_audit(user_id: int | None = None, action: str = "", object: str = "",
                 from_date: date | None = None, to_date: date | None = None,
                 user: User = admin, db: Session = session):
    query, params = audit_query(user_id, action, object, from_date, to_date, None, 100001)
    rows = db.execute(query, params).mappings().all()
    if len(rows) > 100000:
        raise HTTPException(422, "Export exceeds 100,000 entries; narrow the date range or filters")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    fields = ["id", "at", "user_id", "actor_type", "action", "entity_type", "entity_id", "details"]
    writer.writerow(fields)
    for row in rows:
        writer.writerow([csv_cell(json.dumps(row[key]) if key == "details" else row[key]) for key in fields])
    audit(db, "admin.audit_exported", user.id, {"rows": len(rows)})
    db.commit()
    return Response(output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="kinship-audit.csv"', "Cache-Control": "no-store"})


@router.get("/maintenance")
def maintenance(user: User = admin, db: Session = session):
    config = dict(db.execute(text("SELECT * FROM operations_policy WHERE id=1")).mappings().one())
    config.pop("id")
    config.pop("require_admin_totp")
    runs = [dict(row) for row in db.execute(text("""SELECT id,status,payload,error,created_at,finished_at,progress
        FROM jobs WHERE type='maintenance.run' ORDER BY id DESC LIMIT 100""")).mappings()]
    return {"config": config, "runs": runs}


@router.patch("/maintenance")
def set_maintenance(body: Maintenance, user: User = admin, db: Session = session):
    values = body.model_dump()
    assignments = ",".join(f"{key}=:{key}" for key in values)  # Pydantic-owned keys, not caller SQL.
    db.execute(text(f"UPDATE operations_policy SET {assignments} WHERE id=1"), values)
    audit(db, "admin.maintenance_policy_changed", user.id, values)
    db.commit()
    return values


@router.post("/maintenance/run")
def run_maintenance(user: User = admin, db: Session = session):
    job = enqueue(db, "maintenance.run", {}, dedupe_key="maintenance:manual")
    audit(db, "admin.maintenance_requested", user.id, {"job_id": job.id})
    db.commit()
    return {"job_id": job.id}
