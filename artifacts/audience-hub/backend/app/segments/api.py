"""Role-checked partner audiences; definitions always compile to bound SQL."""
import copy
import csv
import io
import re
import time
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from app.auth.deps import require_role
from app.db import session_scope
from app.models import Segment, User, AuditLog
from app.profiles.api import _json_safe
from app.segments.compiler import compile_definition, validate_definition
from app.segments.registry import field_registry
from app.segments.service import graph_lock, references, validate_references, request_materialization

router = APIRouter(prefix="/api/segments", tags=["segments"])
viewer = Depends(require_role("viewer"))
analyst = Depends(require_role("analyst"))
admin = Depends(require_role("admin"))
session = Depends(session_scope)


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: dict
    as_of: datetime | None = None


class Delta(Preview):
    path: str


class Write(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    definition: dict
    status: Literal["draft", "active"] = "draft"
    refresh_schedule: Literal["daily"] | None = "daily"

    @field_validator("refresh_schedule", mode="before")
    @classmethod
    def canonical_schedule(cls, value):
        return "daily" if value == "0 4 * * *" else value


class BulkAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[int] = Field(min_length=1, max_length=500)
    action: Literal["activate", "pause", "refresh"]


def now(body):
    return body.as_of or datetime.now(timezone.utc)


def checked(db, definition, as_of):
    try:
        return compile_definition(db, definition, as_of)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def timed(db, fn):
    previous = db.scalar(text("SHOW statement_timeout"))
    try:
        db.execute(text("SET LOCAL statement_timeout='10s'"))
        result = fn()
        db.execute(text("SELECT set_config('statement_timeout', :previous, true)"),
                   {"previous": previous})
        return result
    except DBAPIError as exc:
        db.rollback()
        if getattr(exc.orig, "sqlstate", None) == "57014":
            raise HTTPException(408, "Partner audience query timed out. Try a narrower window.") from exc
        raise
    except BaseException:
        db.execute(text("SELECT set_config('statement_timeout', :previous, true)"),
                   {"previous": previous})
        raise


def count(db, definition, as_of):
    query = checked(db, definition, as_of)
    return db.scalar(select(func.count()).select_from(query.subquery()))


def get_segment(db, key):
    result = db.get(Segment, key)
    if result is None:
        raise HTTPException(404, "Partner audience not found.")
    return result


def serialize(db, row):
    result = {key: getattr(row, key) for key in (
        "id", "name", "description", "definition", "status", "refresh_schedule",
        "last_count", "last_materialized_at", "created_by", "updated_at")}
    result["owner"] = db.scalar(text("SELECT name FROM users WHERE id=:id"), {"id": row.created_by})
    result["activation_count"] = db.scalar(text("SELECT count(*) FROM activations WHERE segment_id=:id"), {"id": row.id})
    result["change"] = db.scalar(text("SELECT added-removed FROM segment_counts WHERE segment_id=:id ORDER BY counted_at DESC,id DESC LIMIT 1"), {"id": row.id})
    return result


@router.get("/fields")
def fields(user: User = viewer, db: Session = session):
    result = field_registry(db)
    if user.role == "viewer":
        result["fields"] = [field for field in result["fields"] if not field.get("sensitive")]
    return result


@router.get("/typeahead")
@router.get("/fields/{field}/values")
def typeahead(field: str, q: str = "", user: User = viewer, db: Session = session):
    gift_key = field.removeprefix("gift.")
    if gift_key in ("fund", "campaign", "appeal_code", "channel"):
        from app.models import Gift
        col = getattr(Gift, gift_key)
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return timed(db, lambda: {"items": list(db.scalars(select(col).where(
            col.is_not(None), col.ilike(pattern, escape="\\")).distinct().order_by(col).limit(200)))})
    registry = field_registry(db)
    item = next((f for f in registry["fields"] if f["key"] == field), None)
    if item is None:
        raise HTTPException(422, "Unknown partner audience field.")
    return {"items": [v for v in item.get("enum_values", []) if q.casefold() in v.casefold()][:200]}


@router.post("/preview")
def preview(body: Preview, user: User = viewer, db: Session = session):
    start = time.monotonic()
    value = timed(db, lambda: count(db, body.definition, now(body)))
    return {"count": value, "took_ms": round((time.monotonic()-start)*1000, 2)}


def sample_rows(db, ids, user):
    from app.models import Profile, ProfileTrait as ProfileTraits
    # Text metadata does not include contact data; samples mask contact fields recursively.
    query = select(Profile.id, Profile.first_name, Profile.last_name, Profile.email,
                   Profile.phone, ProfileTraits.donor_status, ProfileTraits.source_keys).outerjoin(
                       ProfileTraits, ProfileTraits.profile_id == Profile.id).where(Profile.id.in_(ids)).order_by(Profile.id)
    return [_json_safe(dict(row), mask_pii=user.role == "viewer") for row in db.execute(query).mappings()]


@router.post("/sample")
def sample(body: Preview, user: User = viewer, db: Session = session):
    def run():
        query = checked(db, body.definition, now(body))
        ids = list(db.scalars(query.order_by(query.selected_columns.id).limit(25)))
        # Do not aggregate the full audience to display 25 rows. The count is
        # already supplied by /preview; optional breakdowns have their own path.
        return {"items": sample_rows(db, ids, user),
                "breakdowns": {"status": [], "source": []}}
    return timed(db, run)


@router.post("/breakdowns")
def breakdowns(body: Preview, user: User = viewer, db: Session = session):
    def run():
        query = checked(db, body.definition, now(body))
        sub = query.subquery()
        from app.models import ProfileTrait as ProfileTraits
        status = db.execute(select(ProfileTraits.donor_status, func.count()).select_from(
            sub.outerjoin(ProfileTraits, ProfileTraits.profile_id == sub.c.id)).group_by(ProfileTraits.donor_status)).all()
        # Array expansion counts a partner once per represented source.
        sources = db.execute(select(func.unnest(ProfileTraits.source_keys).label("key"), func.count()).select_from(
            sub.join(ProfileTraits, ProfileTraits.profile_id == sub.c.id)).group_by(text("key"))).all()
        return {"breakdowns": {
            "status": [{"key": k or "unknown", "count": n} for k,n in status],
            "source": [{"key": k, "count": n} for k,n in sources]}}
    return timed(db, run)


@router.post("/delta")
def delta(body: Delta, user: User = viewer, db: Session = session):
    if not re.fullmatch(r"root(?:\.rules\[\d+\])+", body.path):
        raise HTTPException(422, "Select a rule within the partner audience.")
    modified = copy.deepcopy(body.definition)
    indices = [int(x) for x in re.findall(r"\[(\d+)\]", body.path)]
    try:
        node = modified["root"]
        ancestors = []
        for index in indices[:-1]:
            ancestors.append((node, index))
            node = node["rules"][index]
        del node["rules"][indices[-1]]
        # Removal means removing the constraint, not evaluating an empty AND/OR
        # (or its negation). Prune only newly empty groups on the selected path.
        for parent, index in reversed(ancestors):
            if node["rules"]:
                break
            del parent["rules"][index]
            node = parent
    except (KeyError, IndexError, TypeError):
        raise HTTPException(422, "Partner audience rule path no longer exists.")
    def run():
        instant = now(body)
        original = count(db, body.definition, instant)
        without = (count(db, modified, instant) if modified["root"]["rules"] else
                   db.scalar(text("SELECT count(*) FROM active_profiles")))
        return {"count": original, "without_count": without, "delta": without-original}
    return timed(db, run)


@router.post("/explain")
def explain(body: Preview, user: User = admin, db: Session = session):
    query = checked(db, body.definition, now(body))
    compiled = query.compile(dialect=db.bind.dialect, compile_kwargs={"render_postcompile": True})
    # The statement is generated solely by the compiler; values remain DBAPI parameters.
    def run():
        rows = db.connection().exec_driver_sql("EXPLAIN (FORMAT JSON) " + str(compiled), compiled.params).scalar()
        return {"plan": rows}
    return timed(db, run)


@router.get("")
def listing(status: str = "", owner: str = "", user: User = viewer, db: Session = session):
    query = select(Segment).order_by(Segment.updated_at.desc(), Segment.id.desc())
    if status:
        if status not in ("active", "draft", "archived"):
            raise HTTPException(422, "Unknown partner audience status.")
        query = query.where(Segment.status == status)
    if owner:
        if not owner.isdigit():
            raise HTTPException(422, "Invalid owner.")
        query = query.where(Segment.created_by == int(owner))
    return {"items": [serialize(db, row) for row in db.scalars(query.limit(500))]}


def save(db, user, body, row=None):
    graph_lock(db)
    if row is not None and row.status == "active" and body.status != "active":
        for other in db.scalars(select(Segment).where(Segment.status == "active")):
            if row.id in references(other.definition):
                raise HTTPException(409, f"Active partner audience '{other.name}' references this audience.")
    definition = body.definition
    checked(db, definition, datetime.now(timezone.utc))
    try:
        validate_references(db, row.id if row else -1, definition, body.status)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if row is None:
        row = Segment(created_by=user.id)
        db.add(row)
    for key in ("name", "description", "status", "refresh_schedule"):
        setattr(row, key, getattr(body, key))
    row.name = row.name.strip()
    if not row.name:
        raise HTTPException(422, "Enter a partner audience name.")
    row.definition = definition
    row.updated_by = user.id
    db.flush()
    db.add(AuditLog(user_id=user.id, actor_type="user", action="segment.save",
                    entity_type="segment", entity_id=str(row.id), details={"status": row.status}))
    if row.status == "active":
        request_materialization(db, row.id)
    db.commit()
    return serialize(db, row)


@router.post("")
def create(body: Write, user: User = analyst, db: Session = session):
    return save(db, user, body)


@router.post("/bulk")
def bulk(body: BulkAction, user: User = analyst, db: Session = session):
    from app.segments.service import bulk_action
    try:
        result = bulk_action(db, body.ids, body.action, user.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return result


@router.get("/{segment_id}")
def detail(segment_id: int, user: User = viewer, db: Session = session):
    return serialize(db, get_segment(db, segment_id))


@router.put("/{segment_id}")
def update(segment_id: int, body: Write, user: User = analyst, db: Session = session):
    graph_lock(db)
    return save(db, user, body, get_segment(db, segment_id))


@router.delete("/{segment_id}")
def archive(segment_id: int, user: User = analyst, db: Session = session):
    graph_lock(db)
    row = get_segment(db, segment_id)
    for other in db.scalars(select(Segment).where(Segment.status == "active")):
        if segment_id in references(other.definition):
            raise HTTPException(409, f"Active partner audience '{other.name}' references this audience.")
    row.status = "archived"
    db.add(AuditLog(user_id=user.id, actor_type="user", action="segment.archive",
                    entity_type="segment", entity_id=str(row.id), details={}))
    db.commit()
    return {"id": row.id, "status": row.status}


@router.post("/{segment_id}/materialize")
def refresh(segment_id: int, user: User = analyst, db: Session = session):
    graph_lock(db)
    if get_segment(db, segment_id).status != "active":
        raise HTTPException(409, "Activate this partner audience before refreshing.")
    job = request_materialization(db, segment_id)
    db.add(AuditLog(user_id=user.id, actor_type="user", action="segment.materialize",
                    entity_type="segment", entity_id=str(segment_id), details={"job_id": job.id}))
    db.commit()
    return {"job_id": job.id}


@router.get("/{segment_id}/count-history")
@router.get("/{segment_id}/counts")
def history(segment_id: int, user: User = viewer, db: Session = session):
    get_segment(db, segment_id)
    return {"items": [dict(r) for r in db.execute(text("""SELECT counted_at,count,added,removed
      FROM segment_counts WHERE segment_id=:id ORDER BY counted_at DESC,id DESC LIMIT 100"""), {"id": segment_id}).mappings()]}


@router.post("/{segment_id}/count-history/export")
def export_history(segment_id: int, user: User = analyst, db: Session = session):
    get_segment(db, segment_id)
    rows = db.execute(text("""SELECT counted_at,count,added,removed
      FROM segment_counts WHERE segment_id=:id ORDER BY counted_at DESC,id DESC LIMIT 100"""),
      {"id": segment_id}).mappings().all()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["counted_at", "count", "added", "removed"])
    for row in rows:
        writer.writerow([row["counted_at"].isoformat(), row["count"], row["added"], row["removed"]])
    db.add(AuditLog(user_id=user.id, actor_type="user", action="segment.count_history.export",
                    entity_type="segment", entity_id=str(segment_id), details={"rows": len(rows)}))
    db.commit()
    return {"filename": f"partner-audience-{segment_id}-count-history.csv", "csv": output.getvalue()}


@router.get("/{segment_id}/members")
def members(segment_id: int, cursor: str = "", user: User = analyst, db: Session = session):
    get_segment(db, segment_id)
    if cursor and not cursor.isdigit():
        raise HTTPException(422, "Invalid partner cursor.")
    ids = list(db.scalars(text("""SELECT m.profile_id FROM segment_membership m
      JOIN active_profiles p ON p.id=m.profile_id WHERE segment_id=:id AND m.profile_id>:cursor
      ORDER BY m.profile_id LIMIT 51"""), {"id": segment_id, "cursor": int(cursor or 0)}))
    return {"items": sample_rows(db, ids[:50], user), "next_cursor": str(ids[49]) if len(ids)>50 else None}


@router.get("/{segment_id}/activity")
def activity(segment_id: int, user: User = viewer, db: Session = session):
    get_segment(db, segment_id)
    audit = db.execute(text("""SELECT action,at AS created_at,user_id FROM audit_log
      WHERE entity_type='segment' AND entity_id=:id ORDER BY at DESC,id DESC LIMIT 100"""), {"id": str(segment_id)}).mappings()
    activations = db.execute(text("""SELECT a.id,d.name,
      CASE WHEN a.is_active THEN 'active' ELSE 'paused' END AS status
      FROM activations a JOIN destinations d ON d.id=a.destination_id
      WHERE a.segment_id=:id ORDER BY a.id"""), {"id": segment_id}).mappings()
    return {"audit": list(audit), "activations": list(activations)}