"""Chart-ready aggregate endpoints backed by audience data."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import threading
import time
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.db import session_scope
from app.models import AuditLog, User
from app.config import get_settings

router = APIRouter(tags=["dashboards"])

_CACHE_TTL_SECONDS = 300
_cache: dict[tuple[str, date, date], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.RLock()


def invalidate_cache() -> None:
    """Clear dashboard aggregates; call after a successful traits recompute."""
    with _cache_lock:
        _cache.clear()


def _date_range(from_date: date | None, to_date: date | None) -> tuple[date, date, date, date]:
    end = to_date or date.today()
    start = from_date or end.replace(day=1)
    if end < start:
        raise HTTPException(422, detail="'to' must be on or after 'from'")
    if (end - start).days > 3660:
        raise HTTPException(422, detail="Date range cannot exceed 10 years")
    span = (end - start).days + 1
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=span - 1)
    return start, end, prior_start, prior_end


def _rows(db: Session, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    return [dict(row) for row in db.execute(text(sql), params or {}).mappings().all()]


def _scalar(db: Session, sql: str, params: dict[str, Any] | None = None) -> Any:
    return db.execute(text(sql), params or {}).scalar_one()


def _email_opted_in_count(db: Session) -> int:
    """Count unified profiles with an eligible email-consent signal, without returning emails."""
    rows = db.execute(text("""
        WITH active_profiles AS (
          SELECT id FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted
        ), profile_emails AS (
          SELECT p.id AS profile_id, lower(btrim(p.email::text)) AS email
          FROM profiles p JOIN active_profiles ap ON ap.id=p.id
          WHERE p.email IS NOT NULL AND btrim(p.email::text) <> ''
          UNION
          SELECT sr.profile_id, lower(btrim(sr.email_norm::text))
          FROM source_records sr JOIN active_profiles ap ON ap.id=sr.profile_id
          WHERE sr.email_norm IS NOT NULL AND btrim(sr.email_norm::text) <> ''
          UNION
          SELECT i.profile_id, lower(btrim(i.value))
          FROM identifiers i JOIN active_profiles ap ON ap.id=i.profile_id
          WHERE i.type='email' AND btrim(i.value) <> ''
        ), consent_flags AS (
          SELECT pe.profile_id, pe.email,
                 COALESCE(bool_or(lower(COALESCE(sr.attributes->>'email_consent', ''))='opted_in'),
                          false) AS source_opted_in,
                 COALESCE(bool_or(c.status IN ('opted_in', 'subscribed', 'granted')), false)
                   AS ledger_opted_in,
                 COALESCE(bool_or(c.status='opted_out'), false) AS ledger_opted_out
          FROM profile_emails pe
          LEFT JOIN source_records sr
            ON sr.profile_id=pe.profile_id
           AND lower(btrim(sr.email_norm::text))=pe.email
          LEFT JOIN consents c ON c.profile_id=pe.profile_id AND c.channel='email'
          GROUP BY pe.profile_id, pe.email
        )
        SELECT profile_id, email, source_opted_in, ledger_opted_in, ledger_opted_out
        FROM consent_flags WHERE source_opted_in OR ledger_opted_in
    """)).mappings().all()
    if not rows:
        return 0

    pepper = get_settings().pii_hash_pepper.encode()
    email_hashes = {
        hmac.new(pepper, row["email"].strip().casefold().encode(), hashlib.sha256).hexdigest()
        for row in rows
    }
    suppressed = {
        row["value_hash"] for row in db.execute(text("""
            SELECT value_hash FROM suppressions
            WHERE type='email' AND value_hash=ANY(CAST(:hashes AS text[]))
              AND (lower(reason) IN ('hard_bounce', 'spam', 'manual')
                   OR lower(reason) LIKE 'hard_bounce_%'
                   OR lower(reason) LIKE 'spam_%'
                   OR lower(reason) LIKE 'manual_%')
        """), {"hashes": list(email_hashes)}).mappings().all()
    }
    eligible_profiles = {
        row["profile_id"] for row in rows
        if not row["ledger_opted_out"]
        and hmac.new(
            pepper, row["email"].strip().casefold().encode(), hashlib.sha256
        ).hexdigest() not in suppressed
    }
    return len(eligible_profiles)


def _safe_rows(rows: list[dict]) -> list[dict]:
    """Make SQL date/decimal values JSON compatible without exposing identifiers."""
    from datetime import datetime
    from decimal import Decimal

    result = []
    for row in rows:
        result.append({
            key: value.isoformat() if isinstance(value, (date, datetime))
            else float(value) if isinstance(value, Decimal)
            else value
            for key, value in row.items()
        })
    return result


def _build_dashboard(db: Session, name: str, start: date, end: date,
                     prior_start: date, prior_end: date) -> dict:
    params = {
        "from_date": start, "to_exclusive": end + timedelta(days=1),
        "prior_from": prior_start, "prior_to_exclusive": prior_end + timedelta(days=1),
        "as_of": end, "prior_as_of": prior_end,
    }
    charts: dict[str, list[dict]] = {}
    metrics: dict[str, dict] = {}

    if name == "overview":
        metric_sql = {
            "active_profiles": (
                "SELECT count(*) FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted "
                "AND first_seen_at < :cutoff",
                end + timedelta(days=1), prior_end + timedelta(days=1)),
            "donors": (
                "SELECT count(DISTINCT profile_id) FROM gifts WHERE profile_id IS NOT NULL "
                "AND gift_date < :cutoff",
                end + timedelta(days=1), prior_end + timedelta(days=1)),
            "active_donors_12m": (
                "SELECT count(DISTINCT profile_id) FROM gifts WHERE profile_id IS NOT NULL "
                "AND gift_date >= :window_start AND gift_date < :cutoff",
                end + timedelta(days=1), prior_end + timedelta(days=1)),
            "giving_12m": (
                "SELECT COALESCE(sum(amount), 0) FROM gifts WHERE gift_date >= :window_start "
                "AND gift_date < :cutoff",
                end + timedelta(days=1), prior_end + timedelta(days=1)),
            "avg_gift": (
                "SELECT COALESCE(avg(amount), 0) FROM gifts WHERE gift_date >= :window_start "
                "AND gift_date < :cutoff",
                end + timedelta(days=1), prior_end + timedelta(days=1)),
            "recurring_donors": (
                "SELECT count(DISTINCT profile_id) FROM gifts WHERE profile_id IS NOT NULL "
                "AND is_recurring AND gift_date >= :window_start AND gift_date < :cutoff",
                end + timedelta(days=1), prior_end + timedelta(days=1)),
            "email_opted_in": (
                None, None, None),
        }
        for key, (sql, cutoff, prior_cutoff) in metric_sql.items():
            if key == "email_opted_in":
                current = previous = _email_opted_in_count(db)
            else:
                current = _scalar(db, sql, {
                    "cutoff": cutoff, "window_start": cutoff - timedelta(days=365),
                })
                previous = _scalar(db, sql, {
                    "cutoff": prior_cutoff, "window_start": prior_cutoff - timedelta(days=365),
                })
            current_number = float(current) if hasattr(current, "as_tuple") else int(current)
            previous_number = float(previous) if hasattr(previous, "as_tuple") else int(previous)
            difference = current_number - previous_number
            metrics[key] = {
                "value": current_number, "prior": previous_number, "change": difference,
                "change_pct": (difference / previous_number * 100) if previous_number else None,
            }
        charts["monthly_giving"] = _rows(db, """
            SELECT date_trunc('month', gift_date)::date AS month,
                   COALESCE(sum(amount), 0) AS amount, count(*) AS gifts
            FROM gifts WHERE gift_date >= :from_date AND gift_date < :to_exclusive
            GROUP BY 1 ORDER BY 1
        """, params)
        charts["donor_status"] = _rows(db, """
            SELECT donor_status AS status, count(*) AS profiles
            FROM profile_traits GROUP BY donor_status ORDER BY donor_status
        """)
        charts["top_campaigns"] = _rows(db, """
            SELECT COALESCE(campaign, '(unspecified)') AS campaign,
                   count(*) AS gifts, COALESCE(sum(amount), 0) AS amount
            FROM gifts WHERE gift_date >= :from_date AND gift_date < :to_exclusive
            GROUP BY 1 ORDER BY amount DESC, gifts DESC LIMIT 5
        """, params)

    elif name == "giving":
        charts["monthly_giving"] = _rows(db, """
            SELECT date_trunc('month', gift_date)::date AS month,
                   COALESCE(sum(amount), 0) AS amount, count(*) AS gifts
            FROM gifts WHERE gift_date >= :from_date AND gift_date < :to_exclusive
            GROUP BY 1 ORDER BY 1
        """, params)
        charts["new_returning"] = _rows(db, """
            WITH first_gift AS (
              SELECT profile_id, min(gift_date) AS first_date FROM gifts
              WHERE profile_id IS NOT NULL GROUP BY profile_id
            )
            SELECT date_trunc('month', g.gift_date)::date AS month,
                   count(*) FILTER (WHERE f.first_date >= date_trunc('month', g.gift_date)::date
                                     AND f.first_date < date_trunc('month', g.gift_date)::date + interval '1 month') AS new_donors,
                   count(*) FILTER (WHERE f.first_date < date_trunc('month', g.gift_date)::date) AS returning_donors
            FROM gifts g JOIN first_gift f ON f.profile_id=g.profile_id
            WHERE g.gift_date >= :from_date AND g.gift_date < :to_exclusive
            GROUP BY 1 ORDER BY 1
        """, params)
        charts["by_channel"] = _rows(db, """
            SELECT COALESCE(channel, '(unspecified)') AS channel, count(*) AS gifts,
                   COALESCE(sum(amount), 0) AS amount
            FROM gifts WHERE gift_date >= :from_date AND gift_date < :to_exclusive
            GROUP BY 1 ORDER BY amount DESC
        """, params)
        charts["by_fund"] = _rows(db, """
            SELECT COALESCE(fund, '(unspecified)') AS fund, count(*) AS gifts,
                   COALESCE(sum(amount), 0) AS amount
            FROM gifts WHERE gift_date >= :from_date AND gift_date < :to_exclusive
            GROUP BY 1 ORDER BY amount DESC
        """, params)
        charts["top_campaigns"] = _rows(db, """
            SELECT COALESCE(campaign, '(unspecified)') AS campaign,
                   count(*) AS gifts, COALESCE(sum(amount), 0) AS amount
            FROM gifts WHERE gift_date >= :from_date AND gift_date < :to_exclusive
            GROUP BY 1 ORDER BY amount DESC, gifts DESC LIMIT 10
        """, params)
        charts["appeal_codes"] = _rows(db, """
            SELECT COALESCE(appeal_code, '(unspecified)') AS appeal_code,
                   count(*) AS responses, COALESCE(sum(amount), 0) AS amount
            FROM gifts WHERE gift_date >= :from_date AND gift_date < :to_exclusive
            GROUP BY 1 ORDER BY amount DESC
        """, params)
        charts["gift_size_distribution"] = _rows(db, """
            SELECT bucket, count(*) AS gifts, COALESCE(sum(amount), 0) AS amount
            FROM (
              SELECT CASE WHEN amount < 25 THEN '$1–24'
                          WHEN amount < 50 THEN '$25–49'
                          WHEN amount < 100 THEN '$50–99'
                          WHEN amount < 250 THEN '$100–249'
                          WHEN amount < 500 THEN '$250–499'
                          WHEN amount < 1000 THEN '$500–999'
                          WHEN amount < 5000 THEN '$1k–4.9k'
                          ELSE '$5k+' END AS bucket,
                     CASE WHEN amount < 25 THEN 1 WHEN amount < 50 THEN 2
                          WHEN amount < 100 THEN 3 WHEN amount < 250 THEN 4
                          WHEN amount < 500 THEN 5 WHEN amount < 1000 THEN 6
                          WHEN amount < 5000 THEN 7 ELSE 8 END AS sort_order, amount
              FROM gifts WHERE gift_date >= :from_date AND gift_date < :to_exclusive
            ) sizes GROUP BY bucket, sort_order ORDER BY sort_order
        """, params)

    elif name == "retention":
        charts["retention_by_year"] = _rows(db, """
            WITH yearly AS (
              SELECT DISTINCT profile_id, extract(year FROM gift_date)::int AS gift_year
              FROM gifts WHERE profile_id IS NOT NULL AND gift_date IS NOT NULL
            )
            SELECT y.gift_year AS year,
                   count(DISTINCT y.profile_id) FILTER (WHERE next_year.profile_id IS NOT NULL)::int AS retained,
                   count(DISTINCT y.profile_id)::int AS donors,
                   CASE WHEN count(DISTINCT y.profile_id)=0 THEN 0
                        ELSE 100.0 * count(DISTINCT y.profile_id)
                             FILTER (WHERE next_year.profile_id IS NOT NULL)
                             / count(DISTINCT y.profile_id) END AS retention_rate
            FROM yearly y LEFT JOIN yearly next_year
              ON next_year.profile_id=y.profile_id AND next_year.gift_year=y.gift_year + 1
            WHERE y.gift_year BETWEEN extract(year FROM :from_date)::int
                                  AND extract(year FROM :to_date)::int
            GROUP BY y.gift_year ORDER BY y.gift_year
        """, {"from_date": start, "to_date": end})
        charts["cohorts"] = _rows(db, """
            WITH firsts AS (
              SELECT profile_id, min(extract(year FROM gift_date))::int AS first_year
              FROM gifts WHERE profile_id IS NOT NULL AND gift_date IS NOT NULL GROUP BY profile_id
            ), yearly AS (
              SELECT DISTINCT profile_id, extract(year FROM gift_date)::int AS gift_year
              FROM gifts WHERE profile_id IS NOT NULL AND gift_date IS NOT NULL
            ), cohort_sizes AS (
              SELECT first_year, count(*)::int AS cohort_size FROM firsts GROUP BY first_year
            )
            SELECT f.first_year, y.gift_year - f.first_year AS years_after_first,
                   count(*)::int AS retained,
                   100.0 * count(*) / cs.cohort_size AS retention_pct
            FROM firsts f JOIN yearly y ON y.profile_id=f.profile_id
            JOIN cohort_sizes cs ON cs.first_year=f.first_year
            WHERE f.first_year BETWEEN extract(year FROM :from_date)::int AND extract(year FROM :to_date)::int
              AND y.gift_year >= f.first_year
              AND y.gift_year <= extract(year FROM :to_date)::int
            GROUP BY f.first_year, y.gift_year, cs.cohort_size ORDER BY f.first_year, y.gift_year
        """, {"from_date": start, "to_date": end})
        charts["status_over_time"] = _rows(db, """
            SELECT month, donor_status AS status, profile_count AS profiles,
                   ltv_sum, giving_12m_sum
            FROM trait_snapshots
            WHERE month >= CAST(date_trunc('month', CAST(:from_date AS date)) AS date)
              AND month < CAST(date_trunc('month', CAST(:to_exclusive AS date)) AS date)
            ORDER BY month, donor_status
        """, params)

    elif name == "engagement":
        charts["events_by_day"] = _rows(db, """
            SELECT e.occurred_at::date AS day, s.key AS source, count(*) AS events
            FROM events e JOIN sources s ON s.id=e.source_id
            WHERE e.occurred_at >= :from_date AND e.occurred_at < :to_exclusive
            GROUP BY 1, 2 ORDER BY 1, 2
        """, params)
        charts["top_event_names"] = _rows(db, """
            SELECT e.name, e.type, s.key AS source, count(*) AS events
            FROM events e JOIN sources s ON s.id=e.source_id
            WHERE e.occurred_at >= :from_date AND e.occurred_at < :to_exclusive
            GROUP BY e.name, e.type, s.key ORDER BY events DESC LIMIT 20
        """, params)
        charts["viewer_to_donor"] = _rows(db, """
            WITH first_event AS (
              SELECT profile_id, min(occurred_at::date) AS event_date
              FROM events WHERE profile_id IS NOT NULL GROUP BY profile_id
            ), first_gift AS (
              SELECT profile_id, min(gift_date) AS gift_date FROM gifts
              WHERE profile_id IS NOT NULL AND gift_date IS NOT NULL GROUP BY profile_id
            )
            SELECT date_trunc('month', g.gift_date)::date AS month, count(*) AS conversions
            FROM first_gift g JOIN first_event e ON e.profile_id=g.profile_id
            WHERE e.event_date < g.gift_date AND g.gift_date >= :from_date AND g.gift_date < :to_exclusive
            GROUP BY 1 ORDER BY 1
        """, params)

    elif name == "sources":
        charts["profiles_by_source"] = _rows(db, """
            SELECT s.key AS source, count(DISTINCT sr.profile_id) AS profiles
            FROM source_records sr JOIN sources s ON s.id=sr.source_id
            JOIN profiles p ON p.id=sr.profile_id
            WHERE p.merged_into_id IS NULL AND NOT p.is_deleted
            GROUP BY s.key ORDER BY profiles DESC, source
        """)
        charts["overlap_matrix"] = _rows(db, """
            WITH memberships AS (
              SELECT DISTINCT sr.profile_id, s.key AS source
              FROM source_records sr JOIN sources s ON s.id=sr.source_id
              JOIN profiles p ON p.id=sr.profile_id
              WHERE p.merged_into_id IS NULL AND NOT p.is_deleted
            )
            SELECT a.source AS source_a, b.source AS source_b, count(*) AS profiles
            FROM memberships a JOIN memberships b ON b.profile_id=a.profile_id
            WHERE a.source <= b.source GROUP BY a.source, b.source
            ORDER BY source_a, source_b
        """)
        charts["identifier_coverage"] = _rows(db, """
            SELECT s.key AS source, count(DISTINCT sr.profile_id) AS profiles,
                   count(DISTINCT sr.profile_id) FILTER (WHERE COALESCE(sr.email_norm::text, '') <> '') AS email_profiles,
                   count(DISTINCT sr.profile_id) FILTER (WHERE COALESCE(sr.phone_e164, '') <> '') AS phone_profiles,
                   count(DISTINCT sr.profile_id) FILTER (WHERE COALESCE(sr.address1, sr.address2, sr.city, sr.postal_code, '') <> '') AS address_profiles,
                   CASE WHEN count(DISTINCT sr.profile_id)=0 THEN 0 ELSE
                     100.0 * count(DISTINCT sr.profile_id) FILTER (WHERE COALESCE(sr.email_norm::text, '') <> '') / count(DISTINCT sr.profile_id) END AS email_pct,
                   CASE WHEN count(DISTINCT sr.profile_id)=0 THEN 0 ELSE
                     100.0 * count(DISTINCT sr.profile_id) FILTER (WHERE COALESCE(sr.phone_e164, '') <> '') / count(DISTINCT sr.profile_id) END AS phone_pct,
                   CASE WHEN count(DISTINCT sr.profile_id)=0 THEN 0 ELSE
                     100.0 * count(DISTINCT sr.profile_id) FILTER (WHERE COALESCE(sr.address1, sr.address2, sr.city, sr.postal_code, '') <> '') / count(DISTINCT sr.profile_id) END AS address_pct
            FROM source_records sr JOIN sources s ON s.id=sr.source_id
            JOIN profiles p ON p.id=sr.profile_id
            WHERE p.merged_into_id IS NULL AND NOT p.is_deleted
            GROUP BY s.key ORDER BY s.key
        """)

    elif name == "data-health":
        charts["pending_resolutions"] = _rows(db, """
            SELECT s.key AS source, count(*) AS pending
            FROM source_records sr JOIN sources s ON s.id=sr.source_id
            WHERE sr.resolved_at IS NULL GROUP BY s.key ORDER BY s.key
        """)
        charts["merges_per_day"] = _rows(db, """
            SELECT merged_at::date AS day, count(*) AS merges
            FROM profile_merges
            WHERE merged_at >= :from_date AND merged_at < :to_exclusive
            GROUP BY 1 ORDER BY 1
        """, params)
        charts["blocklist_hits"] = _rows(db, """
            SELECT b.type, count(DISTINCT sr.id) AS hits
            FROM identifier_blocklist b JOIN source_records sr
              ON (b.type='email' AND sr.email_norm IS NOT NULL
                  AND lower(sr.email_norm::text) LIKE lower(replace(b.value, '*', '%')))
              OR (b.type='phone' AND sr.phone_e164=b.value)
            GROUP BY b.type ORDER BY b.type
        """)
        charts["rejected_rows_by_import"] = _rows(db, """
            SELECT date_trunc('day', i.created_at)::date AS day, s.key AS source,
                   i.record_type, sum(i.rows_rejected)::int AS rejected
            FROM imports i JOIN sources s ON s.id=i.source_id
            WHERE i.created_at >= :from_date AND i.created_at < :to_exclusive
              AND i.rows_rejected > 0
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
        """, params)
        charts["blocklist_review"] = _rows(db, """
            SELECT b.type, count(*) AS awaiting_review
            FROM identifier_blocklist b
            WHERE b.reason='high_cardinality' AND NOT EXISTS (
              SELECT 1 FROM audit_log a WHERE a.action='data_health.blocklist.approve'
                AND a.details->>'blocklist_id'=b.id::text
            ) GROUP BY b.type ORDER BY b.type
        """)
        charts["expiring_enrichment"] = _rows(db, """
            SELECT s.key AS source, ev.attribute_key, count(*) AS profiles
            FROM enrichment_values ev JOIN sources s ON s.id=ev.source_id
            WHERE ev.license_expires_at >= CURRENT_DATE
              AND ev.license_expires_at < CURRENT_DATE + interval '60 days'
            GROUP BY s.key, ev.attribute_key ORDER BY s.key, ev.attribute_key
        """)
        metrics = {
            "pending_resolution_count": {
                "value": int(_scalar(db, "SELECT count(*) FROM source_records WHERE resolved_at IS NULL")),
                "prior": None, "change": None, "change_pct": None,
            },
            "blocklist_review_count": {
                "value": int(_scalar(db, """SELECT count(*) FROM identifier_blocklist b
                    WHERE b.reason='high_cardinality' AND NOT EXISTS (
                      SELECT 1 FROM audit_log a WHERE a.action='data_health.blocklist.approve'
                        AND a.details->>'blocklist_id'=b.id::text)""")),
                "prior": None, "change": None, "change_pct": None,
            },
            "expiring_enrichment_count": {
                "value": int(_scalar(db, """SELECT count(*) FROM enrichment_values
                    WHERE license_expires_at >= CURRENT_DATE
                      AND license_expires_at < CURRENT_DATE + interval '60 days'""")),
                "prior": None, "change": None, "change_pct": None,
            },
        }

    for key, rows in charts.items():
        charts[key] = _safe_rows(rows)
    return {
        "range": {
            "from": start.isoformat(), "to": end.isoformat(),
            "prior_from": prior_start.isoformat(), "prior_to": prior_end.isoformat(),
        },
        "metrics": metrics,
        "charts": charts,
    }


def _dashboard(name: str, from_date: date | None, to_date: date | None, db: Session) -> dict:
    start, end, prior_start, prior_end = _date_range(from_date, to_date)
    key = (name, start, end)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        if cached:
            _cache.pop(key, None)
    result = _build_dashboard(db, name, start, end, prior_start, prior_end)
    with _cache_lock:
        _cache[key] = (time.monotonic(), result)
    return result


def _date_params(from_date: date | None = Query(default=None, alias="from"),
                 to_date: date | None = Query(default=None, alias="to")):
    return from_date, to_date


def _endpoint(name: str):
    def endpoint(
        from_date: date | None = Query(default=None, alias="from"),
        to_date: date | None = Query(default=None, alias="to"),
        user: User = Depends(require_role("viewer")),
        db: Session = Depends(session_scope),
    ):
        return _dashboard(name, from_date, to_date, db)
    endpoint.__name__ = f"get_{name.replace('-', '_')}_dashboard"
    return endpoint


router.add_api_route("/api/dashboards/overview", _endpoint("overview"), methods=["GET"])
router.add_api_route("/api/dashboards/giving", _endpoint("giving"), methods=["GET"])
router.add_api_route("/api/dashboards/retention", _endpoint("retention"), methods=["GET"])
router.add_api_route("/api/dashboards/engagement", _endpoint("engagement"), methods=["GET"])
router.add_api_route("/api/dashboards/sources", _endpoint("sources"), methods=["GET"])
router.add_api_route("/api/dashboards/data-health", _endpoint("data-health"), methods=["GET"])


_CSV_CHARTS = {
    "overview": {"monthly_giving", "donor_status", "top_campaigns"},
    "giving": {"monthly_giving", "new_returning", "by_channel", "by_fund", "top_campaigns",
               "appeal_codes", "gift_size_distribution"},
    "retention": {"retention_by_year", "cohorts", "status_over_time"},
    "engagement": {"events_by_day", "top_event_names", "viewer_to_donor"},
    "sources": {"profiles_by_source", "overlap_matrix", "identifier_coverage"},
    "data-health": {"pending_resolutions", "merges_per_day", "blocklist_hits",
                    "rejected_rows_by_import", "blocklist_review", "expiring_enrichment"},
}


@router.get("/api/dashboards/{dashboard}/{chart}/csv")
def download_chart_csv(
    dashboard: str,
    chart: str,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    user: User = Depends(require_role("analyst")),
    db: Session = Depends(session_scope),
):
    if dashboard not in _CSV_CHARTS or chart not in _CSV_CHARTS[dashboard]:
        raise HTTPException(404, detail="Dashboard chart not found")
    payload = _dashboard(dashboard, from_date, to_date, db)
    rows = payload["charts"][chart]
    output = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    else:
        output.write("")
    db.add(AuditLog(
        user_id=user.id, actor_type="user", action="dashboard.chart.csv",
        entity_type="dashboard_chart", entity_id=f"{dashboard}.{chart}",
        details={"dashboard": dashboard, "chart": chart,
                 "from": payload["range"]["from"], "to": payload["range"]["to"]},
    ))
    db.commit()
    return Response(
        content=output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{dashboard}-{chart}.csv"'},
    )