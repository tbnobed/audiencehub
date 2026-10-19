"""Administrator-only, write-only SendGrid credentials and delivery history."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.auth.deps import require_role
from app.auth.local import audit
from app.activations.security import encrypt
from app.db import session_scope
from app.jobs.queue import enqueue
from app.models import User
from app.operations.email import public_url, queue_message, settings

router = APIRouter(prefix="/api/admin/email", tags=["account email"])
admin = Depends(require_role("admin"))
session = Depends(session_scope)


class Relay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    from_email: EmailStr
    from_name: str = Field(default="Kinship", min_length=1, max_length=100)
    public_url: str = Field(max_length=2048)
    api_key: SecretStr | None = None


class TestEmail(BaseModel):
    recipient: EmailStr


@router.get("")
def get_relay(user: User = admin, db: Session = session):
    row = settings(db)
    return {key: row[key] for key in ("enabled", "from_email", "from_name", "public_url")} | {
        "key_configured": bool(row["api_key"])}


@router.put("")
def save_relay(body: Relay, user: User = admin, db: Session = session):
    try:
        url = public_url(body.public_url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    # Serializes credential replacement without ever returning its ciphertext.
    db.execute(text("SELECT id FROM account_email_settings WHERE id=1 FOR UPDATE"))
    row = settings(db)
    key = body.api_key.get_secret_value().strip() if body.api_key else ""
    if key and (len(key) > 512 or not key.startswith("SG.") or any(c.isspace() for c in key)):
        raise HTTPException(422, "Enter a valid SendGrid API key")
    if body.enabled and not (key or row["api_key"]):
        raise HTTPException(422, "A SendGrid API key is required before enabling the relay")
    db.execute(text("""UPDATE account_email_settings SET enabled=:enabled,from_email=:email,
        from_name=:name,public_url=:url,api_key=coalesce(:key,api_key) WHERE id=1"""),
        {"enabled": body.enabled, "email": str(body.from_email), "name": body.from_name,
         "url": url, "key": encrypt({"key": key}) if key else None})
    audit(db, "admin.email_relay_changed", user.id, {"enabled": body.enabled, "key_replaced": bool(key)})
    db.commit()
    return {"ok": True}


@router.post("/test")
def test_relay(body: TestEmail, user: User = admin, db: Session = session):
    key = queue_message(db, recipient=str(body.recipient), subject="Kinship email relay test",
                        content="Your Kinship SendGrid relay is working. No action is required.", purpose="test")
    audit(db, "admin.email_test_queued", user.id, {"message_id": key})
    db.commit()
    return {"message_id": key, "email_status": "queued"}


@router.get("/history")
def history(user: User = admin, db: Session = session):
    rows = db.execute(text("""SELECT o.id,o.user_id,o.purpose,o.created_at,o.expires_at,
        o.accepted_at,o.cancelled_at,j.status AS job_status,j.attempts,coalesce(o.last_error,j.error) AS error
        FROM account_email_outbox o LEFT JOIN jobs j ON j.id=o.job_id
        ORDER BY o.id DESC LIMIT 100""")).mappings()
    return {"items": [dict(row) for row in rows]}


@router.post("/{message_id}/retry")
def retry(message_id: int, user: User = admin, db: Session = session):
    row = db.execute(text("""SELECT o.*,j.status AS job_status FROM account_email_outbox o
        LEFT JOIN jobs j ON j.id=o.job_id WHERE o.id=:id FOR UPDATE OF o"""),
        {"id": message_id}).mappings().first()
    if not row:
        raise HTTPException(404, "Message not found")
    if row["accepted_at"] or row["cancelled_at"] or not row["payload"]:
        raise HTTPException(409, "Message already accepted or cancelled")
    from app.models import now
    if row["expires_at"] <= now():
        raise HTTPException(409, "Message expired; issue a new invitation or reset")
    if row["job_status"] in ("queued", "running"):
        return {"email_status": row["job_status"]}
    job = enqueue(db, "account.email", {"message_id": message_id}, max_attempts=5)
    db.execute(text("UPDATE account_email_outbox SET job_id=:job WHERE id=:id"), {"id": message_id, "job": job.id})
    audit(db, "admin.email_retry", user.id, {"message_id": message_id})
    db.commit()
    return {"email_status": "queued"}
