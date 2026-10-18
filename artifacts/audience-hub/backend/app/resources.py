"""First-class reference resources; batch upserts use the importer COPY path."""
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.db import session_scope
from app.imports.staging import copy_upsert
from app.models import User
from app.canonical import FIELDS, EVENT_NAMES

router = APIRouter(tags=["resources"])
ResourceType = Literal["source_codes", "products", "vod_titles"]


class ResourceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=200)
    name: str | None = None
    series: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class ResourceBatch(BaseModel):
    rows: list[ResourceRow] = Field(min_length=1, max_length=10000)


@router.get("/api/schema/canonical")
def catalog(user: User = Depends(require_role("viewer"))):
    return {"fields": {k: v for k, v in FIELDS.items() if user.role != "viewer" or not v["sensitive"]},
            "events": EVENT_NAMES,
            "resources": ["source_codes", "products", "vod_titles"]}


@router.get("/api/resources/{resource_type}")
def list_resources(resource_type: ResourceType, limit: int = Query(100, ge=1, le=1000),
                   offset: int = Query(0, ge=0),
                   user: User = Depends(require_role("viewer")), db: Session = Depends(session_scope)):
    return {"items": list(db.execute(text(
        f"SELECT * FROM {resource_type} ORDER BY key LIMIT :limit OFFSET :offset"
    ), {"limit": limit, "offset": offset}).mappings())}


@router.post("/api/resources/{resource_type}/import")
def import_resources(resource_type: ResourceType, body: ResourceBatch,
                     user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    if len({r.key for r in body.rows}) != len(body.rows):
        raise HTTPException(422, "Duplicate resource keys in batch")
    copy_upsert(db, f"""
        INSERT INTO {resource_type} (key,name,series,attributes)
        SELECT :key,:name,:series,:attributes FROM {{stage}} WHERE true
        ON CONFLICT (key) DO UPDATE SET name=EXCLUDED.name,series=EXCLUDED.series,
          attributes=EXCLUDED.attributes,updated_at=now()
    """, [{**r.model_dump(), "attributes": json.dumps(r.attributes)} for r in body.rows],
        "key text, name text, series text, attributes jsonb")
    db.commit()
    return {"accepted": len(body.rows), "resource_type": resource_type}
