"""First-party graph APIs. Policy and decisions are admin-only and audited."""
import csv
import io
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile, File
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.auth.deps import check_csrf, require_role
from app.db import session_scope
from app.identity.api import private_response
from app.identity.locking import IdentityResolutionDeferred
from app.privacy.locking import privacy_shared
from app.privacy.service import audit

router = APIRouter(prefix="/api/identity/graph", tags=["identity"],
                   dependencies=[Depends(private_response)])


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    review_threshold: float = Field(gt=0, lt=1)
    auto_threshold: float = Field(gt=0, lt=1)
    device_expiry_days: int = Field(ge=1, le=3650)
    household_surname: bool
    schedule_hour: int = Field(ge=0, le=23)


class Run(BaseModel):
    dry_run: bool = True


class Sample(BaseModel):
    count: int = Field(ge=1, le=1000)


class Verdict(BaseModel):
    verdict: str = Field(pattern="^(match|not_match|unsure)$")
    note: str = Field(default="", max_length=2000)


@router.get("/policy")
def policy(db=Depends(session_scope), user=Depends(require_role("admin"))):
    row = db.execute(text("SELECT * FROM identity_graph_policy WHERE id=1")).mappings().one()
    return {key: row[key] for key in Policy.model_fields}


@router.put("/policy", dependencies=[Depends(check_csrf)])
def save_policy(body: Policy, db=Depends(session_scope), user=Depends(require_role("admin"))):
    if body.review_threshold >= body.auto_threshold:
        raise HTTPException(422, "Review threshold must be below auto-link threshold")
    values = body.model_dump()
    from app.identity.locking import acquire_resolver_lock
    acquire_resolver_lock(db)
    db.execute(text("""UPDATE identity_graph_policy SET enabled=:enabled,
      review_threshold=:review_threshold,auto_threshold=:auto_threshold,
      device_expiry_days=:device_expiry_days,household_surname=:household_surname,
      schedule_hour=:schedule_hour WHERE id=1"""), values)
    # Existing links get the configured expiry as well, including shorter TTLs.
    db.execute(text("""UPDATE identity_devices SET expires_at=last_seen_at+
      make_interval(days=>:days)"""), {"days": body.device_expiry_days})
    from app.identity.addresses import refresh_households
    privacy_shared(db)
    refresh_households(db)
    audit(db, user.id, "identity.graph.policy", "identity_graph_policy", 1, values)
    db.commit()
    return values


@router.post("/runs", dependencies=[Depends(check_csrf)])
def start(body: Run, db=Depends(session_scope), user=Depends(require_role("admin"))):
    from app.identity.graph_jobs import queue_run
    identifier = queue_run(db, body.dry_run)
    audit(db, user.id, "identity.graph.queue", "job", identifier, {"dry_run": body.dry_run})
    db.commit()
    return {"job_id": identifier}


@router.get("/runs")
def runs(db=Depends(session_scope), user=Depends(require_role("admin"))):
    return {"items": [dict(row) for row in db.execute(text("""
      SELECT r.id,r.job_id,r.dry_run,r.started_at,coalesce(r.finished_at,j.finished_at) AS finished_at,
        r.counts,CASE WHEN r.status IN ('queued','running') AND j.status IN ('failed','cancelled')
          THEN j.status ELSE r.status END AS status,
        CASE WHEN r.status IN ('queued','running') AND j.status IN ('failed','cancelled')
          THEN 'Worker stopped before completion; no uncommitted graph changes were kept.'
          ELSE r.error END AS error
      FROM identity_graph_runs r LEFT JOIN jobs j ON j.id=r.job_id ORDER BY r.id DESC LIMIT 100
    """)).mappings()]}


@router.get("/quality")
def quality(limit: int = Query(default=100, ge=1, le=1000),
            db=Depends(session_scope), user=Depends(require_role("admin"))):
    privacy_shared(db)
    rows = db.execute(text("""
      SELECT q.*,q.left_record_id AS left_source_record_id,q.right_record_id AS right_source_record_id,
        l.master_id AS left_master_id,r.master_id AS right_master_id,
        coalesce(ls.first_name||' '||ls.last_name,l.first_name||' '||l.last_name) AS left_name,
        coalesce(rs.first_name||' '||rs.last_name,r.first_name||' '||r.last_name) AS right_name,
        ls.source_id AS left_source_id,rs.source_id AS right_source_id
      FROM identity_pairs q JOIN profiles l ON l.id=q.left_id JOIN profiles r ON r.id=q.right_id
      LEFT JOIN source_records ls ON ls.id=q.left_record_id
      LEFT JOIN source_records rs ON rs.id=q.right_record_id
      WHERE NOT l.is_deleted AND NOT r.is_deleted
      ORDER BY (q.verdict IS NULL) DESC,q.sampled DESC,q.id DESC LIMIT :n
    """), {"n": limit}).mappings()
    return {"items": [dict(r) for r in rows]}


@router.post("/quality/sample", dependencies=[Depends(check_csrf)])
def sample(body: Sample, db=Depends(session_scope), user=Depends(require_role("admin"))):
    privacy_shared(db)
    from app.identity.quality import sample as create_sample
    count = create_sample(db, body.count)
    audit(db, user.id, "identity.quality.sample", "identity", None, {"sampled": count})
    db.commit()
    return {"sampled": count}


@router.post("/quality/{identifier}/verdict", dependencies=[Depends(check_csrf)])
def verdict(identifier: int, body: Verdict, db=Depends(session_scope), user=Depends(require_role("admin"))):
    from app.identity.quality import verdict as decide
    try:
        result = decide(db, identifier, body.verdict, body.note, user.id)
        db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, IdentityResolutionDeferred) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/trends")
def trends(db=Depends(session_scope), user=Depends(require_role("analyst"))):
    return {"items": [dict(row) for row in db.execute(text("""
      SELECT d.*,s.name AS source_name FROM identity_graph_daily d JOIN sources s ON s.id=d.source_id
      WHERE d.day>=current_date-90 ORDER BY d.day DESC,s.name
    """)).mappings()]}


@router.get("/profiles/{profile_id}")
def graph_profile(profile_id: int, db=Depends(session_scope), user=Depends(require_role("analyst"))):
    privacy_shared(db)
    profile = db.execute(text("SELECT * FROM active_profiles WHERE id=:id"),
                         {"id": profile_id}).mappings().first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    address = db.execute(text("SELECT * FROM identity_addresses WHERE profile_id=:id"),
                         {"id": profile_id}).mappings().first()
    members = db.execute(text("""SELECT id,master_id,first_name,last_name FROM active_profiles
      WHERE household_id=:household ORDER BY id"""), {"household": profile["household_id"]}).mappings().all()
    devices = db.execute(text("""SELECT d.* FROM identity_devices d
      WHERE d.profile_id=:id AND d.expires_at>now() ORDER BY d.last_seen_at DESC"""), {"id": profile_id}).mappings()
    return {"address": dict(address) if address else None,
            "household": {"id": profile["household_id"], "members": [dict(r) for r in members]}
                if profile["household_id"] else None,
            "devices": [dict(r) for r in devices]}


@router.post("/addresses/vendor", dependencies=[Depends(check_csrf)])
async def load_vendor(file: UploadFile = File(...), db=Depends(session_scope),
                      user=Depends(require_role("admin"))):
    """Bounded CSV adapter for CASS/NCOA, no file bytes persisted."""
    from app.identity.addresses import vendor_result, refresh_households
    from app.identity.locking import acquire_resolver_lock
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "Vendor CSV must be at most 10 MiB; split larger files")
    acquire_resolver_lock(db)
    count, stale = 0, 0
    try:
        for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
            identifier = int(row.pop("profile_id"))
            input_hash, vendor = row.pop("input_hash"), row.pop("vendor")
            if not vendor or len(vendor) > 100:
                raise ValueError("Invalid vendor")
            changed = vendor_result(db, identifier, input_hash, vendor, row)
            count += changed
            stale += int(not changed)
        refresh_households(db)
    except (KeyError, ValueError, UnicodeError, csv.Error):
        raise HTTPException(422, "Invalid vendor CSV; no changes saved. Check required columns and address components.") from None
    audit(db, user.id, "identity.address.vendor", "identity", None, {"updated": count, "stale": stale})
    db.commit()
    return {"updated": count, "stale": stale}
