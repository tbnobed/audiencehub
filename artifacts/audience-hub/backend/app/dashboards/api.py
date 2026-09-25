"""Dashboard requests read only published aggregates; raw scans live in rollups.py."""
import csv
import io
from datetime import date, timedelta, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.db import session_scope
from app.models import AuditLog, User

router = APIRouter(tags=["dashboards"])


def invalidate_cache():
    """Invalidation is now the atomic generation publication in traits.recompute."""


def _date_range(from_date, to_date):
    end = to_date or date.today()
    start = from_date or end.replace(day=1)
    if end < start:
        raise HTTPException(422, detail="'to' must be on or after 'from'")
    if (end-start).days > 3660:
        raise HTTPException(422, detail="Date range cannot exceed 10 years")
    prior_end = start-timedelta(days=1)
    return start, end, prior_end-(end-start), prior_end


def _rows(db, sql, params=None):
    return [dict(r) for r in db.execute(text(sql), params or {}).mappings()]


def _scalar(db, sql, params=None):
    return db.execute(text(sql), params or {}).scalar_one()


def _safe_rows(rows):
    return [{k: v.isoformat() if isinstance(v, (date, datetime)) else
             float(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in rows]


def _kpi(db, key):
    return _scalar(db, "SELECT value FROM dashboard_kpis WHERE key=:key", {"key": key})


def _email_opted_in_count(db):
    return int(_kpi(db, "email_opted_in"))


def _build_dashboard(db, name, start, end, prior_start, prior_end):
    if name in ("overview", "overview-primary"):
        from app.dashboards.overview import build_overview
        return build_overview(db, start, end, prior_start, prior_end)
    params = {"s": start, "e": end}
    charts, metrics = {}, {}
    if name == "giving":
        charts["monthly_giving"] = _rows(db, """
          SELECT date_trunc('month',day)::date AS month,sum(gift_amount) AS amount,
            sum(gift_count)::bigint AS gifts FROM dashboard_daily
          WHERE day BETWEEN :s AND :e GROUP BY 1 ORDER BY 1""", params)
        charts["new_returning"] = _rows(db, """
          WITH m AS (SELECT DISTINCT p.id,p.days[1] AS first_date,p.donors,
            date_trunc('month',day)::date AS month FROM dashboard_donor_patterns p,
            unnest(p.days) day WHERE day BETWEEN :s AND :e)
          SELECT month,COALESCE(sum(donors) FILTER(WHERE first_date>=month),0)::bigint AS new_donors,
            COALESCE(sum(donors) FILTER(WHERE first_date<month),0)::bigint AS returning_donors
          FROM m GROUP BY month ORDER BY month""", params)
        for chart, field in (("by_channel", "channel"), ("by_fund", "fund"),
                             ("top_campaigns", "campaign")):
            charts[chart] = _rows(db, f"""
              SELECT COALESCE({field},'(unspecified)') AS {field},
                sum(gift_count)::bigint AS gifts,sum(gift_amount) AS amount
              FROM dashboard_daily WHERE day BETWEEN :s AND :e
              GROUP BY 1 ORDER BY amount DESC,gifts DESC
              {"LIMIT 10" if field == "campaign" else ""}""", params)
        charts["appeal_codes"] = _rows(db, """
          SELECT COALESCE(appeal_code,'(unspecified)') AS appeal_code,
            sum(gift_count)::bigint AS responses,sum(gift_amount) AS amount
          FROM dashboard_giving_daily WHERE day BETWEEN :s AND :e GROUP BY 1 ORDER BY amount DESC""", params)
        charts["gift_size_distribution"] = _rows(db, """
          SELECT bucket,sum(gift_count)::bigint AS gifts,sum(gift_amount) AS amount
          FROM dashboard_giving_daily WHERE day BETWEEN :s AND :e
          GROUP BY bucket,sort_order ORDER BY sort_order""", params)
    elif name == "retention":
        charts["retention_by_year"] = [r for r in _kpi(db, "retention_by_year")
                                       if start.year <= r["year"] <= end.year]
        charts["cohorts"] = [r for r in _kpi(db, "cohorts")
                             if start.year <= r["first_year"] <= end.year
                             and r["first_year"]+r["years_after_first"] <= end.year]
        charts["status_over_time"] = _rows(db, """
          SELECT month,donor_status AS status,profile_count AS profiles,ltv_sum,giving_12m_sum
          FROM trait_snapshots WHERE month>=date_trunc('month',CAST(:s AS date))
            AND month<=:e ORDER BY month,donor_status""", params)
    elif name == "engagement":
        charts["events_by_day"] = _rows(db, """
          SELECT day,source,sum(events)::bigint AS events FROM dashboard_event_daily
          WHERE day BETWEEN :s AND :e GROUP BY 1,2 ORDER BY 1,2""", params)
        charts["top_event_names"] = _rows(db, """
          SELECT name,type,source,sum(events)::bigint AS events FROM dashboard_event_daily
          WHERE day BETWEEN :s AND :e GROUP BY 1,2,3 ORDER BY events DESC LIMIT 20""", params)
        charts["viewer_to_donor"] = _rows(db, """
          SELECT date_trunc('month',day)::date AS month,sum(conversions)::bigint AS conversions
          FROM dashboard_conversion_daily WHERE day BETWEEN :s AND :e GROUP BY 1 ORDER BY 1""", params)
    elif name == "sources":
        charts = {k: _kpi(db, k) for k in ("profiles_by_source", "overlap_matrix", "identifier_coverage")}
    elif name == "data-health":
        charts = {k: _kpi(db, k) for k in ("pending_resolutions", "blocklist_hits")}
        charts["merges_per_day"] = _rows(db, """
          SELECT merged_at::date AS day,count(*) AS merges FROM profile_merges
          WHERE merged_at>=:s AND merged_at<CAST(:e AS date)+1 GROUP BY 1 ORDER BY 1""", params)
        charts["rejected_rows_by_import"] = _rows(db, """
          SELECT i.created_at::date AS day,s.key AS source,i.record_type,
            sum(i.rows_rejected)::int AS rejected FROM imports i JOIN sources s ON s.id=i.source_id
          WHERE i.created_at>=:s AND i.created_at<CAST(:e AS date)+1 AND i.rows_rejected>0
          GROUP BY 1,2,3 ORDER BY 1,2,3""", params)
        charts["blocklist_review"] = _rows(db, """
          SELECT b.type,count(*) AS awaiting_review FROM identifier_blocklist b
          WHERE b.reason='high_cardinality' AND NOT EXISTS(SELECT 1 FROM audit_log a
            WHERE a.action='data_health.blocklist.approve' AND a.details->>'blocklist_id'=b.id::text)
          GROUP BY b.type ORDER BY b.type""")
        charts["expiring_enrichment"] = _rows(db, """
          SELECT s.key AS source,ev.attribute_key,count(*) AS profiles
          FROM enrichment_values ev JOIN sources s ON s.id=ev.source_id
          WHERE ev.license_expires_at>=CURRENT_DATE
            AND ev.license_expires_at<CURRENT_DATE+interval '60 days'
          GROUP BY 1,2 ORDER BY 1,2""")
        for key, value in (
            ("pending_resolution_count", sum(r["pending"] for r in charts["pending_resolutions"])),
            ("blocklist_review_count", sum(r["awaiting_review"] for r in charts["blocklist_review"])),
            ("expiring_enrichment_count", sum(r["profiles"] for r in charts["expiring_enrichment"]))):
            metrics[key] = {"value": value, "prior": None, "change": None, "change_pct": None}
    return {"range": {"from": start.isoformat(), "to": end.isoformat(),
                      "prior_from": prior_start.isoformat(), "prior_to": prior_end.isoformat()},
            "metrics": metrics, "charts": {k: _safe_rows(v) for k, v in charts.items()}}


def build_cache_key(db, key):
    name, start, end = key
    if isinstance(start, str):
        start, end = date.fromisoformat(start), date.fromisoformat(end)
    return _build_dashboard(db, name, *_date_range(start, end))


def _dashboard(name, from_date, to_date, db):
    from app.dashboards.cache import cached
    start, end, _, _ = _date_range(from_date, to_date)
    key = (name, start, end)
    return cached(db, key, lambda: build_cache_key(db, key))


def _endpoint(name):
    def endpoint(from_date: date | None = Query(None, alias="from"),
                 to_date: date | None = Query(None, alias="to"),
                 user: User = Depends(require_role("viewer")),
                 db: Session = Depends(session_scope)):
        result = _dashboard(name, from_date, to_date, db)
        if name == "overview":
            from app.dashboards.overview import attention
            result["overview"]["attention"] = attention(db, user.role, result["overview"]["stats"]["lapsing"]["value"])
        return result
    endpoint.__name__ = "get_"+name.replace("-", "_")
    return endpoint


for _name in ("overview", "giving", "retention", "engagement", "sources", "data-health"):
    router.add_api_route("/api/dashboards/"+_name, _endpoint(_name), methods=["GET"])
router.add_api_route("/api/dashboards/overview/primary", _endpoint("overview-primary"), methods=["GET"])


def _card_endpoint(card):
    def endpoint(from_date: date | None = Query(None, alias="from"),
                 to_date: date | None = Query(None, alias="to"),
                 user: User = Depends(require_role("viewer")),
                 db: Session = Depends(session_scope)):
        from app.dashboards.cache import generation
        generation(db)
        if card in ("needs-attention", "attention"):
            from app.dashboards.overview import attention
            return {"attention": attention(db, user.role, int(_kpi(db, "lapsing")))}
        if card == "email":
            return {"value": _email_opted_in_count(db)}
        result = _dashboard("overview", from_date, to_date, db)
        value = result["overview"]
        if card == "kpis":
            return {"range": result["range"], "kpis": value["kpis"], "stats": value["stats"]}
        if card == "giving-by-month":
            return {"range": result["range"], "giving_by_month": {
                k: value[k] for k in ("monthly_giving", "top_two_month_share")}}
        if card == "campaigns":
            return {"top_campaigns": {k: value[k] for k in ("campaigns", "campaigns_href")}}
        return {"partner_status": value["partner_status"]}
    endpoint.__name__ = "overview_"+card.replace("-", "_")
    return endpoint


for _card in ("kpis", "giving-by-month", "needs-attention", "campaigns", "partner-status", "email", "attention"):
    router.add_api_route("/api/dashboards/overview/"+_card, _card_endpoint(_card), methods=["GET"])


@router.get("/api/shell")
def shell_summary(user: User = Depends(require_role("viewer")), db: Session = Depends(session_scope)):
    return _shell_summary(user, db)


def _shell_summary(user, db):
    from app.dashboards.overview import health_counts
    health = health_counts(db, user.role)
    result = {"imports_running": None, "active_import": None,
              "open_issues": sum(health.values()), "issue_counts": health}
    if user.role == "viewer":
        return result
    running = _rows(db, """
      SELECT i.id,i.filename AS name,i.rows_total AS total,i.rows_ok+i.rows_rejected AS done,
        extract(epoch FROM(CURRENT_TIMESTAMP-i.started_at)) AS elapsed_seconds FROM imports i
      WHERE i.status='running' AND EXISTS(SELECT 1 FROM jobs j WHERE j.type='import.run'
        AND j.payload->>'import_id'=i.id::text AND j.status='running')
      ORDER BY i.started_at,i.id""")
    result["imports_running"] = len(running)
    if running:
        row = running[0]
        elapsed = float(row.pop("elapsed_seconds") or 0)
        row.update(percent=min(100, 100*row["done"]/row["total"]) if row["total"] else None,
                   rows_per_second=row["done"]/elapsed if elapsed > 0 else None,
                   speed_basis="committed_rows_since_import_started", href=f"/imports?import_id={row['id']}")
        result["active_import"] = row
    return result


_CSV_CHARTS = {
    "overview": {"monthly_giving", "donor_status", "top_campaigns"},
    "giving": {"monthly_giving", "new_returning", "by_channel", "by_fund", "top_campaigns", "appeal_codes", "gift_size_distribution"},
    "retention": {"retention_by_year", "cohorts", "status_over_time"},
    "engagement": {"events_by_day", "top_event_names", "viewer_to_donor"},
    "sources": {"profiles_by_source", "overlap_matrix", "identifier_coverage"},
    "data-health": {"pending_resolutions", "merges_per_day", "blocklist_hits", "rejected_rows_by_import", "blocklist_review", "expiring_enrichment"},
}


@router.get("/api/dashboards/{dashboard}/{chart}/csv")
def download_chart_csv(dashboard: str, chart: str,
                       from_date: date | None = Query(None, alias="from"),
                       to_date: date | None = Query(None, alias="to"),
                       user: User = Depends(require_role("analyst")), db: Session = Depends(session_scope)):
    if dashboard not in _CSV_CHARTS or chart not in _CSV_CHARTS[dashboard]:
        raise HTTPException(404, detail="Dashboard chart not found")
    payload = _dashboard(dashboard, from_date, to_date, db)
    rows = payload["charts"][chart]
    output = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    db.add(AuditLog(user_id=user.id, actor_type="user", action="dashboard.chart.csv",
                    entity_type="dashboard_chart", entity_id=f"{dashboard}.{chart}",
                    details={"dashboard": dashboard, "chart": chart, **payload["range"]}))
    db.commit()
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{dashboard}-{chart}.csv"'})