"""Authenticated server-to-server device event channel, never public SDK input."""
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.activations.security import encrypt, decrypt
from app.auth.deps import require_role, check_csrf
from app.db import session_scope
from app.identity.devices import observe
from app.identity.locking import acquire_import_lock
from app.privacy.service import audit

router = APIRouter(prefix="/api/identity/graph", tags=["identity"])
event_router = APIRouter(prefix="/v1", tags=["identity"])


@router.post("/device-sources/{source_id}/rotate", dependencies=[Depends(check_csrf)])
def rotate(source_id: int, db=Depends(session_scope), user=Depends(require_role("admin"))):
    if not db.scalar(text("SELECT EXISTS(SELECT 1 FROM sources WHERE id=:id AND is_active)"), {"id": source_id}):
        raise HTTPException(404, "Active source not found")
    secret = secrets.token_urlsafe(48)
    db.execute(text("""INSERT INTO identity_device_credentials(source_id,secret_encrypted)
      VALUES(:id,:secret) ON CONFLICT(source_id) DO UPDATE SET secret_encrypted=EXCLUDED.secret_encrypted"""),
      {"id": source_id, "secret": encrypt({"secret": secret})})
    audit(db, user.id, "identity.device.rotate", "source", source_id, {})
    db.commit()
    from fastapi.responses import JSONResponse
    return JSONResponse({"secret": secret}, headers={"Cache-Control": "no-store"})


class DeviceEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=256)
    occurred_at: datetime
    okta_customer_id: str | None = Field(default=None, min_length=1, max_length=512)
    fusionauth_user_id: str | None = Field(default=None, min_length=1, max_length=512)
    devices: dict[str, str]


@event_router.post("/device-events/{source_id}")
async def authenticated_event(source_id: int, request: Request, db=Depends(session_scope)):
    # This API is authenticated by a separate private server secret. This /v1
    # channel uses no cookie authentication; HMAC covers raw
    # bytes, source-specific route, and a short-lived timestamp.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 16384:
            raise HTTPException(413, "Device event too large")
    timestamp = request.headers.get("X-Kinship-Timestamp", "")
    signature = request.headers.get("X-Kinship-Signature", "")
    try:
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError()
    except ValueError:
        raise HTTPException(401, "Invalid device event signature") from None
    acquire_import_lock(db)
    encrypted = db.scalar(text("""SELECT c.secret_encrypted FROM identity_device_credentials c
      JOIN sources s ON s.id=c.source_id WHERE c.source_id=:id AND s.is_active"""), {"id": source_id})
    if not encrypted:
        raise HTTPException(401, "Invalid device event signature")
    secret = decrypt(encrypted)["secret"]
    payload = f"{source_id}.{timestamp}.".encode() + bytes(raw)
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid device event signature")
    try:
        body = DeviceEvent.model_validate_json(bytes(raw))
        if not (body.okta_customer_id or body.fusionauth_user_id):
            raise ValueError()
        # Validate all device fields before receipt insertion.
        from app.identity.devices import DEVICE_KEY_TYPES
        if set(body.devices) - set(DEVICE_KEY_TYPES) or not body.devices:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, "Invalid device event") from None
    from app.ingestion.service import suppressed
    identities = {kind: value.strip() for kind, value in (
        ("okta_customer_id", body.okta_customer_id),
        ("fusionauth_user_id", body.fusionauth_user_id)) if value}
    if not identities or any(not value for value in identities.values()):
        raise HTTPException(422, "A non-empty login identity is required")
    if suppressed(db, set(identities.items())):
        raise HTTPException(422, "Identity is suppressed")
    event_hash = hashlib.sha256(body.event_id.encode()).hexdigest()
    if not db.scalar(text("""INSERT INTO identity_device_receipts(source_id,event_hash)
      VALUES(:s,:h) ON CONFLICT DO NOTHING RETURNING true"""), {"s": source_id, "h": event_hash}):
        return {"accepted": True, "duplicate": True}
    try:
        count = observe(db, source_id, {"occurred_at": body.occurred_at,
            "properties": {**body.devices, **identities}},
            trusted_authenticated=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not count:
        # Do not consume a receipt before the person exists: the server can
        # retry after contact resolution. No looping background resolver.
        raise HTTPException(409, "No unique current Okta/FusionAuth identity or event outside device lifetime")
    audit(db, None, "identity.device.observed", "source", source_id, {"links": count})
    db.commit()
    return {"accepted": True, "links": count}
