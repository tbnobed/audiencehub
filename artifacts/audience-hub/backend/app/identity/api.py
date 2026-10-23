"""Identity lookups are PII-bearing analyst operations; reversal is admin only."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth.deps import require_role, check_csrf
from app.db import session_scope
from app.identity.master import lookup_bulk, resolve_master, unmerge
from app.identity.locking import IdentityResolutionDeferred
from app.privacy.locking import privacy_shared
from app.privacy.service import audit

def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Cookie"


router = APIRouter(prefix="/api/identity", tags=["identity"],
                   dependencies=[Depends(private_response)])


class LookupKey(BaseModel):
    key_type: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=512)


class BulkLookup(BaseModel):
    items: list[LookupKey] = Field(min_length=1, max_length=10000)


def run_lookup(db, items, user):
    privacy_shared(db)
    try:
        result = lookup_bulk(db, items)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Never log submitted identifiers or matches in audit metadata.
    audit(db, user.id, "identity.lookup", "identity", None, {"keys": len(items)})
    db.commit()
    return result


@router.get("/lookup")
def lookup(key_type: str = Query(max_length=40), value: str = Query(max_length=512),
           db=Depends(session_scope), user=Depends(require_role("analyst"))):
    return run_lookup(db, [{"key_type": key_type, "value": value}], user)[0]


@router.post("/lookup/bulk", dependencies=[Depends(check_csrf)])
def bulk_lookup(body: BulkLookup, db=Depends(session_scope), user=Depends(require_role("analyst"))):
    return {"items": run_lookup(db, [x.model_dump() for x in body.items], user)}


@router.get("/health")
def health(db=Depends(session_scope), user=Depends(require_role("analyst"))):
    privacy_shared(db)
    stats = dict(db.execute(text("""
      SELECT (SELECT count(*) FROM active_profiles) AS profiles,
        (SELECT count(*) FROM profiles WHERE master_id IS NULL) AS missing_master_ids,
        (SELECT count(*) FROM identity_reviews) AS conflicts,
        (SELECT count(*) FROM (
          SELECT master_id FROM identity_xref WHERE key_type='hubspot_record_id'
          GROUP BY master_id HAVING count(DISTINCT key_value)>1) d) AS duplicate_hubspot_profiles,
        (SELECT count(*) FROM active_profiles p WHERE
          EXISTS(SELECT 1 FROM identity_xref x WHERE x.master_id=p.master_id
            AND x.key_type IN ('email_md5','email_sha256'))
          AND NOT EXISTS(SELECT 1 FROM identity_xref x WHERE x.master_id=p.master_id
            AND x.key_type='email')) AS hash_only_profiles
    """)).mappings().one())
    coverage = [dict(r) for r in db.execute(text("""
      SELECT x.key_type,count(DISTINCT x.master_id) AS profiles FROM identity_xref x
      JOIN active_profiles p ON p.master_id=x.master_id
      GROUP BY x.key_type ORDER BY x.key_type
    """)).mappings()]
    tbn_plus_identity = dict(db.execute(text("""
      WITH keys AS MATERIALIZED (
        SELECT master_id,bool_or(key_type='okta_customer_id') AS okta,
          bool_or(key_type='fusionauth_user_id') AS fusionauth
        FROM identity_xref WHERE key_type IN ('okta_customer_id','fusionauth_user_id')
        GROUP BY master_id
      ), users AS (
        SELECT COALESCE(k.okta,false) AS okta,COALESCE(k.fusionauth,false) AS fusionauth
        FROM active_profiles p LEFT JOIN keys k ON k.master_id=p.master_id
        LEFT JOIN profile_traits t ON t.profile_id=p.id
        WHERE k.master_id IS NOT NULL OR NULLIF(btrim(t.attr_tbn_plus_subscription),'') IS NOT NULL
      )
      SELECT count(*) AS total,
        count(*) FILTER(WHERE okta) AS okta,
        count(*) FILTER(WHERE fusionauth) AS fusionauth,
        count(*) FILTER(WHERE okta AND fusionauth) AS both,
        count(*) FILTER(WHERE okta AND NOT fusionauth) AS okta_only,
        count(*) FILTER(WHERE fusionauth AND NOT okta) AS fusionauth_only,
        count(*) FILTER(WHERE NOT okta AND NOT fusionauth) AS neither FROM users
    """)).mappings().one())
    # Show zero coverage explicitly before the migration starts.
    for key_type in ("okta_customer_id", "fusionauth_user_id"):
        if not any(row["key_type"] == key_type for row in coverage):
            coverage.append({"key_type": key_type, "profiles": 0})
    coverage.sort(key=lambda row: row["key_type"])
    reviews = [dict(r) for r in db.execute(text("""
      SELECT r.id,r.source_record_id,r.reason,r.created_at,p.master_id
      FROM identity_reviews r LEFT JOIN source_records s ON s.id=r.source_record_id
      LEFT JOIN profiles p ON p.id=s.profile_id ORDER BY r.id DESC LIMIT 100
    """)).mappings()]
    duplicates = [dict(r) for r in db.execute(text("""
      SELECT master_id,array_agg(DISTINCT key_value ORDER BY key_value) AS hubspot_record_ids,
        'duplicate_in_source' AS status FROM identity_xref WHERE key_type='hubspot_record_id'
      GROUP BY master_id HAVING count(DISTINCT key_value)>1 ORDER BY master_id LIMIT 100
    """)).mappings()]
    return {**stats, "key_coverage": coverage, "tbn_plus_identity": tbn_plus_identity, "review_items": reviews,
            "duplicate_hubspot": duplicates}


@router.get("/master/{master_id}")
def master(master_id: str, db=Depends(session_scope), user=Depends(require_role("analyst"))):
    privacy_shared(db)
    try:
        resolved = resolve_master(db, master_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not resolved:
        raise HTTPException(404, "Master identity not found")
    profile_id = db.scalar(text("SELECT id FROM profiles WHERE master_id=:m"), {"m": resolved})
    keys = [dict(r) for r in db.execute(text("""
      SELECT x.key_type,x.key_value,x.source_id,x.first_seen_at,x.last_seen_at,x.status,
        x.method,x.confidence,x.rule,s.name AS source_name
      FROM identity_xref x LEFT JOIN sources s ON s.id=x.source_id
      WHERE master_id=:m ORDER BY key_type,key_value,source_id
    """), {"m": resolved}).mappings()]
    history = [dict(r) for r in db.execute(text("""
      SELECT m.id AS merge_id,w.master_id AS survivor_id,l.master_id AS retired_id,m.merged_at,
        j.undone_at,(j.merge_id IS NOT NULL AND j.undone_at IS NULL
          AND w.merged_into_id IS NULL AND NOT w.is_deleted
          AND NOT EXISTS(
            SELECT 1 FROM jsonb_array_elements((j.sides->'winner'->'source_records') ||
              (j.sides->'loser'->'source_records')) r
            LEFT JOIN source_records sr ON sr.id=(r->>'id')::bigint
            WHERE sr.profile_id IS DISTINCT FROM m.winner_id
              OR sr.updated_at IS DISTINCT FROM (r->>'updated_at')::timestamptz)
          AND (SELECT count(*) FROM source_records WHERE profile_id=m.winner_id)=
            jsonb_array_length(j.sides->'winner'->'source_records')+
            jsonb_array_length(j.sides->'loser'->'source_records')
          AND NOT EXISTS(
          SELECT 1 FROM identity_merge_journal later WHERE later.merge_id>m.id
          AND later.undone_at IS NULL AND (later.winner_id IN (m.winner_id,m.loser_id)
            OR later.loser_id IN (m.winner_id,m.loser_id)))) AS reversible
      FROM profile_merges m JOIN profiles w ON w.id=m.winner_id
      JOIN profiles l ON l.id=m.loser_id LEFT JOIN identity_merge_journal j ON j.merge_id=m.id
      WHERE m.winner_id=:p OR m.loser_id=:p ORDER BY m.id DESC
    """), {"p": profile_id}).mappings()]
    audit(db, user.id, "identity.master", "identity", None, {"requests": 1})
    db.commit()
    return {"requested_master_id": master_id, "master_id": resolved, "profile_id": profile_id,
            "keys": keys, "merge_history": history}


@router.post("/merges/{merge_id}/unmerge", dependencies=[Depends(check_csrf)])
def reverse(merge_id: int, db=Depends(session_scope), user=Depends(require_role("admin"))):
    try:
        result = unmerge(db, merge_id, user.id)
        db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, IdentityResolutionDeferred) as exc:
        raise HTTPException(409, str(exc)) from exc
