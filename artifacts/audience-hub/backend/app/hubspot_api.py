"""HubSpot-specific discovery, safe preview and destination run controls."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.activations import security, service
from app.auth.deps import require_role
from app.db import session_scope
from app.hubspot_client import HubSpot
from app.hubspot_destination import enqueue_destination
from app.imports.mapping import suggest_mapping
from app.models import Destination, User
from app.source_api import source
from app.source_connectors import SourceError
from app.source_pulls import connection_row

router = APIRouter(prefix="/api", tags=["hubspot"])


@router.get("/sources/{identifier}/hubspot-properties")
def properties(identifier: int, db: Session = Depends(session_scope), user: User = Depends(require_role("admin"))):
    if source(db, identifier).kind != "hubspot":
        raise HTTPException(422, "Choose a HubSpot source")
    try:
        row = connection_row(db, identifier)
        with HubSpot(security.decrypt(row["credentials"])) as client:
            data = client.request("GET", "/crm/v3/properties/contacts")
        items = [{key: prop.get(key) for key in ("name", "label", "type")} for prop in data.get("results", [])]
        columns = {**suggest_mapping([item["name"] for item in items], "contact"),
                   "firstname": "first_name", "lastname": "last_name", "address": "address1",
                   "state": "region", "zip": "postal_code", "hs_object_id": "hubspot_record_id"}
        return {"items": items, "suggested_columns": columns}
    except SourceError as error:
        raise HTTPException(422, str(error)) from None


@router.post("/destinations/{identifier}/run", status_code=202)
def run_destination(identifier: int, db: Session = Depends(session_scope), user: User = Depends(require_role("admin"))):
    destination = db.get(Destination, identifier)
    if destination is None:
        raise HTTPException(404, "Destination not found")
    try:
        result = enqueue_destination(db, destination, user.id)
        service.audit(db, "destination.run", "destination", identifier, user.id, run_id=result["run_id"])
        db.commit()
        return result
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get("/destinations/{identifier}/hubspot-preview")
def preview(identifier: int, db: Session = Depends(session_scope), user: User = Depends(require_role("admin"))):
    from app.models import Segment
    from app.segments.compiler import compile_definition
    from app.privacy.service import digest
    from app.identity.normalize import normalize_email
    destination = db.get(Destination, identifier)
    if not destination or destination.type != "hubspot":
        raise HTTPException(404, "HubSpot destination not found")
    segment = db.get(Segment, destination.config["segment_id"])
    if not segment:
        raise HTTPException(422, "Destination segment no longer exists")
    query = compile_definition(db, segment.definition).subquery()
    ids = list(db.scalars(select(query.c.id).limit(100)))
    if not ids:
        return {"items": [], "limit": 100}
    rows = db.execute(text("""SELECT DISTINCT p.master_id,p.email::text AS email,x.key_value AS hubspot_record_id
        FROM active_profiles p JOIN identity_xref x ON x.master_id=p.master_id AND x.key_type='hubspot_record_id'
        JOIN consents c ON c.profile_id=p.id AND c.channel='email' AND c.status='opted_in'
        WHERE p.id=ANY(:ids) LIMIT 100"""), {"ids": ids}).mappings().all()
    hashes = {row["hubspot_record_id"]: digest(normalize_email(row["email"]) or "") for row in rows}
    blocked = set(db.scalars(text("SELECT value_hash FROM suppressions WHERE type='email' AND value_hash=ANY(:hashes)"),
                            {"hashes": list(hashes.values())}))
    return {"items": [dict(row) for row in rows if row["email"] and hashes[row["hubspot_record_id"]] not in blocked],
            "limit": 100}
