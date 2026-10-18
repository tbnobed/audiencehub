import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.db import session_scope
from app.models import Source, User

router = APIRouter(prefix="/api/sources", tags=["sources"])
RECORD_TYPES = {"contact", "gift", "event", "enrichment", "consent", "resource"}


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=2, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    kind: str = "csv"
    record_types: list[str] = Field(min_length=1)
    priority: int = Field(default=100, ge=0, le=10000)
    vendor: str | None = None
    license_expires_at: date | None = None
    settings: dict = Field(default_factory=dict)
    is_active: bool = True

    @field_validator("key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,99}", value):
            raise ValueError("Key must be a lowercase slug using letters, digits and underscores")
        return value

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in {"csv", "event_api", "sftp_jsonl", "snowflake", "zeta_exit"}:
            raise ValueError("Unsupported source kind")
        return value

    @field_validator("record_types")
    @classmethod
    def valid_types(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or not set(values) <= RECORD_TYPES:
            raise ValueError("Invalid or duplicate record type")
        return values

    @field_validator("settings")
    @classmethod
    def safe_settings(cls, value: dict) -> dict:
        # Pull config and secrets belong exclusively in /connection; settings
        # are historically readable by viewers and cannot hold credentials.
        def check(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if any(word in key.lower() for word in ("password", "secret", "credential", "private_key", "token")):
                        raise ValueError("Use the encrypted connection endpoint for credentials")
                    check(child)
            elif isinstance(item, list):
                for child in item:
                    check(child)
        check(value)
        return value


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    record_types: list[str] | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    vendor: str | None = None
    license_expires_at: date | None = None
    settings: dict | None = None
    is_active: bool | None = None

    _valid_types = field_validator("record_types")(SourceCreate.valid_types.__func__)
    _safe_settings = field_validator("settings")(SourceCreate.safe_settings.__func__)


def source_json(source: Source) -> dict:
    return {
        "id": source.id, "key": source.key, "name": source.name, "kind": source.kind,
        "record_types": source.record_types, "priority": source.priority,
        "vendor": source.vendor,
        "license_expires_at": source.license_expires_at.isoformat() if source.license_expires_at else None,
        "settings": source.settings, "is_active": source.is_active,
        "has_write_key": bool(source.write_key_hash),
    }


def get_source(db: Session, source_id: int) -> Source:
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    return source


@router.get("")
def list_sources(user: User = Depends(require_role("viewer")), db: Session = Depends(session_scope)):
    sources = db.scalars(select(Source).order_by(Source.priority, Source.id)).all()
    return {"items": [source_json(source) for source in sources], "next_cursor": None}


@router.get("/{source_id}")
def read_source(source_id: int, user: User = Depends(require_role("viewer")),
                db: Session = Depends(session_scope)):
    return source_json(get_source(db, source_id))


@router.post("", status_code=201)
def create_source(data: SourceCreate, user: User = Depends(require_role("admin")),
                  db: Session = Depends(session_scope)):
    if data.kind in {"sftp_jsonl", "snowflake", "zeta_exit"} and data.settings:
        raise HTTPException(422, "Use the encrypted connection endpoint for pull configuration")
    source = Source(**data.model_dump())
    db.add(source)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Source key already exists") from exc
    db.refresh(source)
    return source_json(source)


@router.patch("/{source_id}")
def update_source(source_id: int, data: SourceUpdate,
                  user: User = Depends(require_role("admin")),
                  db: Session = Depends(session_scope)):
    source = get_source(db, source_id)
    if source.kind in {"sftp_jsonl", "snowflake", "zeta_exit"} and data.settings:
        raise HTTPException(422, "Use the encrypted connection endpoint for pull configuration")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(source, key, value)
    db.commit()
    db.refresh(source)
    return source_json(source)


@router.delete("/{source_id}")
def delete_source(source_id: int, user: User = Depends(require_role("admin")),
                  db: Session = Depends(session_scope)):
    source = get_source(db, source_id)
    for table in ("imports", "source_records", "gifts", "events", "enrichment_values", "ingestion_receipts", "source_import_receipts"):
        count = db.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE source_id = :id)"), {"id": source_id})
        if count:
            raise HTTPException(409, "Source has data; deactivate it instead of deleting its history")
    db.delete(source)
    db.commit()
    return {"ok": True}


@router.post("/{source_id}/write-key")
def rotate_write_key(source_id: int, user: User = Depends(require_role("admin")),
                     db: Session = Depends(session_scope)):
    import hashlib
    import secrets
    from app.models import AuditLog
    source = get_source(db, source_id)
    if source.kind != "event_api" or "event" not in source.record_types:
        raise HTTPException(422, "Write keys require an event API source with event records")
    key = "wk_live_" + secrets.token_urlsafe(32)
    source.write_key_hash = hashlib.sha256(key.encode()).hexdigest()
    db.add(AuditLog(user_id=user.id, actor_type="user", action="source.rotate_write_key",
                   entity_type="source", entity_id=str(source.id), details={}))
    db.commit()
    from fastapi.responses import JSONResponse
    return JSONResponse({"write_key": key}, headers={"Cache-Control": "no-store"})


@router.get("/{source_id}/events")
def event_tail(source_id: int, cursor: int | None = None, limit: int = 50,
               user: User = Depends(require_role("analyst")), db: Session = Depends(session_scope)):
    from app.models import Event
    get_source(db, source_id)
    if not 1 <= limit <= 100 or (cursor is not None and cursor < 1):
        raise HTTPException(422, "Invalid event cursor or limit")
    stmt = select(Event).where(Event.source_id == source_id).order_by(Event.id.desc()).limit(limit + 1)
    if cursor:
        stmt = stmt.where(Event.id < cursor)
    events = db.scalars(stmt).all()
    # Tail is intentionally payload-free for every role: event properties,
    # URL/query strings and anonymous IDs can contain contact data.
    items = [{"id": e.id, "type": e.type, "name": e.name, "profile_id": e.profile_id,
              "occurred_at": e.occurred_at.isoformat(), "received_at": e.received_at.isoformat(),
              "timestamp_clamped": bool(e.context.get("timestamp_clamped"))} for e in events[:limit]]
    return {"items": items, "next_cursor": items[-1]["id"] if len(events) > limit else None}


from app.source_api import router as connection_router
router.include_router(connection_router)