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
RECORD_TYPES = {"contact", "gift", "event", "enrichment", "consent"}


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
        if value not in {"csv", "event_api"}:
            raise ValueError("Kind must be csv or event_api")
        return value

    @field_validator("record_types")
    @classmethod
    def valid_types(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or not set(values) <= RECORD_TYPES:
            raise ValueError("Invalid or duplicate record type")
        return values


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


def source_json(source: Source) -> dict:
    return {
        "id": source.id, "key": source.key, "name": source.name, "kind": source.kind,
        "record_types": source.record_types, "priority": source.priority,
        "vendor": source.vendor,
        "license_expires_at": source.license_expires_at.isoformat() if source.license_expires_at else None,
        "settings": source.settings, "is_active": source.is_active,
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
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(source, key, value)
    db.commit()
    db.refresh(source)
    return source_json(source)


@router.delete("/{source_id}")
def delete_source(source_id: int, user: User = Depends(require_role("admin")),
                  db: Session = Depends(session_scope)):
    source = get_source(db, source_id)
    for table in ("imports", "source_records", "gifts", "events", "enrichment_values"):
        count = db.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE source_id = :id)"), {"id": source_id})
        if count:
            raise HTTPException(409, "Source has data; deactivate it instead of deleting its history")
    db.delete(source)
    db.commit()
    return {"ok": True}