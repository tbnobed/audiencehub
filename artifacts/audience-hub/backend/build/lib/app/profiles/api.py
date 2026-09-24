"""Profile search/detail and identity-resolution data health endpoints."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.db import session_scope
from app.models import AuditLog, IdentifierBlocklist, User
from app.traits.registry import TRAITS

router = APIRouter(tags=["profiles"])

_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d().\-\s]{7,}\d)(?!\w)")


def _mask_email(value: str) -> str:
    local, domain = value.rsplit("@", 1)
    return f"{local[:1]}***@{domain}"


def _mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***"


def _json_safe(value: Any, *, mask_pii: bool = False) -> Any:
    """Normalize DB values and recursively mask contact data for viewer responses."""
    if isinstance(value, dict):
        return {key: _json_safe(item, mask_pii=mask_pii) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, mask_pii=mask_pii) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if not isinstance(value, str) or not mask_pii:
        return value

    value = _EMAIL_RE.sub(lambda match: _mask_email(match.group(0)), value)
    return _PHONE_RE.sub(lambda match: _mask_phone(match.group(0)), value)


def _rows(db: Session, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    return [dict(row) for row in db.execute(text(sql), params or {}).mappings().all()]


@router.get("/api/profiles")
def list_profiles(
    search: str | None = Query(default=None, max_length=200),
    source_id: int | None = Query(default=None, ge=1),
    has_email: bool | None = None,
    has_phone: bool | None = None,
    donor_status: Literal[
        "prospect", "new", "active", "reactivated", "lapsing", "lapsed"
    ] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(session_scope),
):
    clauses = ["p.merged_into_id IS NULL", "p.is_deleted = false"]
    params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}
    if search and search.strip():
        clauses.append("p.search_text % :search")
        params["search"] = search.strip()
    if source_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM source_records sr WHERE sr.profile_id=p.id AND sr.source_id=:source_id)"
        )
        params["source_id"] = source_id
    if has_email is not None:
        clauses.append(
            "((p.email IS NOT NULL AND p.email <> '') OR EXISTS "
            "(SELECT 1 FROM identifiers i WHERE i.profile_id=p.id AND i.type='email')) = :has_email"
        )
        params["has_email"] = has_email
    if has_phone is not None:
        clauses.append(
            "((p.phone IS NOT NULL AND p.phone <> '') OR EXISTS "
            "(SELECT 1 FROM identifiers i WHERE i.profile_id=p.id AND i.type='phone')) = :has_phone"
        )
        params["has_phone"] = has_phone
    if donor_status:
        clauses.append("COALESCE(pt.donor_status, 'prospect') = :donor_status")
        params["donor_status"] = donor_status
    where = " AND ".join(clauses)
    total = db.execute(text(f"""
        SELECT count(*)
        FROM profiles p
        LEFT JOIN profile_traits pt ON pt.profile_id=p.id
        WHERE {where}
    """), params).scalar_one()
    order = "similarity(p.search_text, :search) DESC, p.id" if search and search.strip() else "p.id"
    items = _rows(db, f"""
        SELECT p.id, p.first_name, p.last_name, p.email, p.phone, p.city, p.region,
               p.country, p.first_seen_at, p.last_seen_at,
               COALESCE(pt.donor_status, 'prospect') AS donor_status,
               COALESCE(
                   to_jsonb(pt) - 'profile_id',
                   jsonb_build_object('donor_status', COALESCE(pt.donor_status, 'prospect'))
               ) AS traits
        FROM profiles p
        LEFT JOIN profile_traits pt ON pt.profile_id=p.id
        WHERE {where}
        ORDER BY {order}
        LIMIT :limit OFFSET :offset
    """, params)
    return _json_safe({"items": items, "total": total, "page": page, "page_size": page_size},
                      mask_pii=user.role == "viewer")


def _profile_or_404(db: Session, profile_id: int) -> dict:
    row = db.execute(text("""
        SELECT p.*, COALESCE(pt.donor_status, 'prospect') AS donor_status,
               pt.gift_count_total, pt.ltv_total, pt.gift_amount_12m,
               pt.gift_count_12m, pt.first_gift_date, pt.last_gift_date,
               pt.largest_gift_amount, pt.avg_gift_amount, pt.is_recurring_active,
               pt.days_since_last_gift, pt.rfm_recency, pt.rfm_frequency,
               pt.rfm_monetary, pt.rfm_score, pt.event_count_30d, pt.last_event_at,
               pt.video_views_30d, pt.last_engagement_channel, pt.source_keys,
                pt.computed_at, pt.computed_at AS traits_computed_at
        FROM profiles p
        LEFT JOIN profile_traits pt ON pt.profile_id=p.id
        WHERE p.id=:id AND p.merged_into_id IS NULL AND p.is_deleted=false
    """), {"id": profile_id}).mappings().first()
    if not row:
        merged_into = db.execute(text(
            "SELECT merged_into_id FROM profiles WHERE id=:id AND merged_into_id IS NOT NULL"
        ), {"id": profile_id}).scalar()
        if merged_into:
            raise HTTPException(301, detail={"merged_into": merged_into})
        raise HTTPException(404, detail="Profile not found")
    return dict(row)


@router.get("/api/profiles/{profile_id}")
def profile_detail(
    profile_id: int,
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(session_scope),
):
    profile = _profile_or_404(db, profile_id)
    scalar_fields = (
        "id", "merged_into_id", "email", "phone", "first_name", "last_name",
        "address1", "city", "region", "postal_code", "country", "first_seen_at",
        "last_seen_at", "donor_status",
    )
    trait_fields = (
        "gift_count_total", "ltv_total", "gift_amount_12m", "gift_count_12m",
        "first_gift_date", "last_gift_date", "largest_gift_amount", "avg_gift_amount",
        "is_recurring_active", "days_since_last_gift", "rfm_recency", "rfm_frequency",
        "rfm_monetary", "rfm_score", "event_count_30d", "last_event_at",
        "donor_status", "video_views_30d", "last_engagement_channel", "source_keys",
        "computed_at", "traits_computed_at",
    )
    pid = profile_id
    gifts = _rows(db, """
        SELECT g.id, g.amount, g.currency, g.gift_date, g.fund, g.campaign,
               g.appeal_code, g.channel, g.payment_method, g.is_recurring,
               s.id AS source_id, s.key AS source_key, s.name AS source_name
        FROM gifts g JOIN sources s ON s.id=g.source_id
        WHERE g.profile_id=:id ORDER BY g.gift_date DESC NULLS LAST, g.id DESC
    """, {"id": pid})
    events = _rows(db, """
        SELECT e.id, e.type, e.name, e.occurred_at, e.properties, e.context,
               s.id AS source_id, s.key AS source_key, s.name AS source_name
        FROM events e JOIN sources s ON s.id=e.source_id
        WHERE e.profile_id=:id ORDER BY e.occurred_at DESC, e.id DESC
    """, {"id": pid})
    identifiers = _rows(db, """
        SELECT id, type, value, first_seen_at FROM identifiers
        WHERE profile_id=:id ORDER BY type, first_seen_at, id
    """, {"id": pid})
    source_records = _rows(db, """
        SELECT sr.id, sr.external_id, sr.email_norm AS email, sr.phone_e164 AS phone,
               sr.first_name, sr.last_name, sr.address1, sr.address2, sr.city,
               sr.region, sr.postal_code, sr.country, sr.attributes,
               sr.last_import_id, sr.created_at, sr.updated_at,
               s.id AS source_id, s.key AS source_key, s.name AS source_name, s.priority
        FROM source_records sr JOIN sources s ON s.id=sr.source_id
        WHERE sr.profile_id=:id ORDER BY s.priority, sr.updated_at DESC, sr.id
    """, {"id": pid})
    merges = _rows(db, """
        SELECT m.id, m.winner_id, m.loser_id, m.reason, m.merged_at, m.job_id
        FROM profile_merges m
        WHERE m.winner_id=:id OR m.loser_id=:id ORDER BY m.merged_at DESC, m.id DESC
    """, {"id": pid})
    enrichment = _rows(db, """
        SELECT ev.source_id, s.key AS source_key, s.name AS source_name,
               ev.attribute_key, ev.value_text, ev.value_num, ev.value_bool,
               ev.value_date, ev.imported_at, ev.license_expires_at
        FROM enrichment_values ev JOIN sources s ON s.id=ev.source_id
        WHERE ev.profile_id=:id
          AND (ev.license_expires_at IS NULL OR ev.license_expires_at >= CURRENT_DATE)
        ORDER BY s.priority, ev.attribute_key
    """, {"id": pid})
    consents = _rows(db, """
        SELECT c.channel, c.status, c.source_id, s.key AS source_key,
               c.captured_at, c.evidence
        FROM consents c JOIN sources s ON s.id=c.source_id
        WHERE c.profile_id=:id ORDER BY c.channel
    """, {"id": pid})

    result = {key: profile[key] for key in scalar_fields}
    result["traits"] = {key: profile[key] for key in trait_fields}
    result.update({
        "gifts": gifts, "events": events, "identifiers": identifiers,
        "source_records": source_records, "merges": merges,
        "enrichment": enrichment, "consents": consents,
    })
    db.add(AuditLog(user_id=user.id, actor_type="user", action="profile.view",
                    entity_type="profile", entity_id=str(profile_id),
                    details={"role": user.role}))
    db.commit()
    return _json_safe(result, mask_pii=user.role == "viewer")


@router.get("/api/traits")
def trait_catalog(user: User = Depends(require_role("viewer"))):
    """Return the documented trait catalog for profile/segment builders."""
    return {"items": [trait.public_dict() for trait in TRAITS]}


@router.get("/api/data-health")
def data_health(user: User = Depends(require_role("viewer")),
                db: Session = Depends(session_scope)):
    pending = db.execute(text(
        "SELECT count(*) FROM source_records WHERE resolved_at IS NULL"
    )).scalar_one()
    merges = _rows(db, """
        SELECT merged_at::date AS day, count(*) AS count
        FROM profile_merges
        WHERE merged_at >= CURRENT_DATE - INTERVAL '29 days'
        GROUP BY merged_at::date ORDER BY day
    """)
    blocklist_hits = db.execute(text("""
        SELECT count(DISTINCT sr.id)
        FROM source_records sr
        WHERE EXISTS (
          SELECT 1 FROM identifier_blocklist b
          WHERE (b.type='email' AND sr.email_norm IS NOT NULL
                 AND lower(sr.email_norm::text) LIKE lower(replace(b.value, '*', '%')))
             OR (b.type='phone' AND sr.phone_e164=b.value)
        )
    """)).scalar_one()
    auto_blocklisted = _rows(db, """
        SELECT b.id, b.type, b.value, b.reason, b.created_at
        FROM identifier_blocklist b
        WHERE b.reason='high_cardinality'
          AND NOT EXISTS (
            SELECT 1 FROM audit_log a
            WHERE a.action='data_health.blocklist.approve'
              AND a.details->>'blocklist_id'=b.id::text
          )
        ORDER BY b.created_at DESC, b.id DESC
    """)
    rejected_rows = db.execute(text(
        "SELECT COALESCE(sum(rows_rejected), 0) FROM imports"
    )).scalar_one()
    recent_imports = _rows(db, """
        SELECT i.id, i.filename, i.record_type, i.status, i.rows_total,
               i.rows_ok, i.rows_rejected, i.started_at, i.finished_at, i.created_at,
               s.id AS source_id, s.key AS source_key, s.name AS source_name
        FROM imports i JOIN sources s ON s.id=i.source_id
        ORDER BY i.created_at DESC, i.id DESC LIMIT 10
    """)
    return _json_safe({
        "pending_count": pending,
        "merges_per_day": merges,
        "blocklist_hits": blocklist_hits,
        "auto_blocklisted": auto_blocklisted,
        "rejected_rows": rejected_rows,
        "recent_imports": recent_imports,
    }, mask_pii=user.role == "viewer")


def _get_blocklist_item(db: Session, blocklist_id: int) -> IdentifierBlocklist:
    item = db.get(IdentifierBlocklist, blocklist_id)
    if not item or item.reason != "high_cardinality":
        raise HTTPException(404, detail="Auto-blocklisted identifier not found")
    return item


@router.post("/api/data-health/blocklist/{blocklist_id}/approve")
def approve_blocklist_item(blocklist_id: int, user: User = Depends(require_role("admin")),
                           db: Session = Depends(session_scope)):
    item = _get_blocklist_item(db, blocklist_id)
    db.add(AuditLog(user_id=user.id, actor_type="user", action="data_health.blocklist.approve",
                    entity_type="identifier_blocklist", entity_id=str(item.id),
                    details={"blocklist_id": item.id}))
    db.commit()
    return {"id": item.id, "status": "approved"}


@router.post("/api/data-health/blocklist/{blocklist_id}/unblock")
def unblock_blocklist_item(blocklist_id: int, user: User = Depends(require_role("admin")),
                           db: Session = Depends(session_scope)):
    item = _get_blocklist_item(db, blocklist_id)
    db.add(AuditLog(user_id=user.id, actor_type="user", action="data_health.blocklist.unblock",
                    entity_type="identifier_blocklist", entity_id=str(item.id),
                    details={"blocklist_id": item.id, "type": item.type}))
    db.delete(item)
    db.commit()
    return {"id": blocklist_id, "status": "unblocked"}