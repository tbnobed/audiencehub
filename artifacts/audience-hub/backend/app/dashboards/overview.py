"""Live, resolved-profile overview aggregates. See docs/OVERVIEW_API.md."""
from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy import text


def rows(db, sql, params=None):
    from app.dashboards.cache import check_deadline
    check_deadline(db)
    return [dict(r) for r in db.execute(text(sql), params or {}).mappings()]


def month_shift(day, offset):
    year, month = divmod(day.year * 12 + day.month - 1 + offset, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def delta(value, prior):
    return {"value": value, "prior": prior, "change": value - prior,
            "change_pct": (value - prior) / prior * 100 if prior else None,
            "delta_label": "—" if value == prior == 0 else "New" if prior == 0 else None}


GIFTS = ("SELECT g.gift_date, g.amount, g.campaign FROM gifts g "
         "JOIN active_profiles p ON p.id=g.profile_id")


def overview_facts(db, windows, cutoffs):
    """One gift scan for all KPI windows and historical classifications.

    Aggregate at profile grain before counting partners; never materialize g.* or
    rerun a full-history retention/status query for each sparkline point.
    """
    params, columns, totals = {}, [], []
    for i, (start, end) in enumerate(windows):
        params.update({f"s{i}": start, f"e{i}": end,
                       f"ys{i}": month_shift(start, -12), f"ye{i}": month_shift(end, -12)})
        current = f"gift_date BETWEEN :s{i} AND :e{i}"
        previous = f"gift_date BETWEEN :ys{i} AND :ye{i}"
        columns.extend([
            f"sum(amount) FILTER(WHERE {current}) AS amount{i}",
            f"count(*) FILTER(WHERE {current}) AS gifts{i}",
            f"bool_or({previous}) AS base{i}",
        ])
        totals.extend([
            f"COALESCE(sum(amount{i}),0) AS amount{i}",
            f"COALESCE(sum(gifts{i}),0) AS gifts{i}",
            f"count(*) FILTER(WHERE gifts{i}>0) AS partners{i}",
            f"count(*) FILTER(WHERE base{i}) AS base{i}",
            f"count(*) FILTER(WHERE base{i} AND gifts{i}>0) AS retained{i}",
        ])
    for i, end in enumerate(cutoffs):
        params[f"cut{i}"] = end
        columns.extend([
            f"min(gift_date) FILTER(WHERE gift_date<=:cut{i}) AS first{i}",
            # Two latest gift dates, including same-date gifts, are needed for
            # reactivation. Dates tied by id have the same classification.
            f"(array_agg(gift_date ORDER BY gift_date DESC) "
            f"FILTER(WHERE gift_date<=:cut{i}))[1:2] AS latest{i}",
            f"bool_or(is_recurring AND gift_date>CAST(:cut{i} AS date)-365 "
            f"AND gift_date<=:cut{i}) AS recurring{i}",
        ])
        status = f"""CASE WHEN first{i} IS NULL THEN 'prospect'
            WHEN CAST(:cut{i} AS date)-latest{i}[1]<=365
              AND first{i}>=CAST(:cut{i} AS date)-365 THEN 'new'
            WHEN CAST(:cut{i} AS date)-latest{i}[1]<=365
              AND latest{i}[1]-latest{i}[2]>730 THEN 'reactivated'
            WHEN CAST(:cut{i} AS date)-latest{i}[1]<=365 THEN 'active'
            WHEN CAST(:cut{i} AS date)-latest{i}[1]<=730 THEN 'lapsing'
            ELSE 'lapsed' END"""
        totals.extend(f"count(*) FILTER(WHERE ({status})='{s}') AS {s}{i}"
                      for s in ("new", "reactivated", "active", "lapsing", "lapsed"))
        totals.append(f"count(*) FILTER(WHERE recurring{i}) AS recurring{i}")
    params["max_date"] = max([e for _, e in windows] + list(cutoffs))
    r = rows(db, f"""
        WITH facts AS (
          SELECT g.profile_id, {','.join(columns)}
          FROM gifts g JOIN active_profiles p ON p.id=g.profile_id
          WHERE gift_date<=:max_date GROUP BY g.profile_id
        ) SELECT {','.join(totals)} FROM facts
    """, params)[0]
    periods = {}
    for i, window in enumerate(windows):
        periods[window] = {
            "giving": float(r[f"amount{i}"]),
            "average_gift": float(r[f"amount{i}"]/r[f"gifts{i}"]) if r[f"gifts{i}"] else 0,
            "active_partners": r[f"partners{i}"],
            "retention_base": r[f"base{i}"], "retained": r[f"retained{i}"],
            "retention_yoy": 100*r[f"retained{i}"]/r[f"base{i}"] if r[f"base{i}"] else 0,
        }
    return periods, {
        end: {s: r[f"{s}{i}"] for s in ("new", "reactivated", "active", "lapsing", "lapsed")}
        for i, end in enumerate(cutoffs)
    }, {end: r[f"recurring{i}"] for i, end in enumerate(cutoffs)}


def build_overview(db, start, end, prior_start, prior_end):
    from app.dashboards.api import _email_opted_in_count
    windows = [(start, end), (prior_start, prior_end)]
    windows += [(m, min(month_shift(m, 1)-timedelta(days=1), end))
                for m in (month_shift(end.replace(day=1), offset) for offset in range(-11, 1))]
    windows += [(end-timedelta(days=364), end), (prior_end-timedelta(days=364), prior_end)]
    last_month = end.replace(day=1)-timedelta(days=1)
    periods, classifications, recurring_counts = overview_facts(
        db, list(dict.fromkeys(windows)), list(dict.fromkeys((end, last_month, prior_end))))
    profiles = rows(db, "SELECT count(*) AS n FROM active_profiles")[0]["n"]
    for classification in classifications.values():
        classification["prospect"] = profiles-sum(classification.values())
    def period(db, s, e):
        return periods[s, e]
    def statuses(db, e):
        return [{"status": k, "count": v} for k, v in classifications[e].items()]
    def recurring(db, e):
        return recurring_counts[e]
    # A second, date-bounded scan serves both chart series and campaign totals.
    chart_rows = rows(db, f"""
        SELECT gift_date, COALESCE(campaign,'(unspecified)') AS campaign,
          grouping(gift_date) AS is_campaign,
          count(*) AS gifts, sum(amount) AS amount,
          count(*) FILTER(WHERE gift_date BETWEEN :s AND :e) AS current_gifts,
          sum(amount) FILTER(WHERE gift_date BETWEEN :s AND :e) AS current_amount
        FROM ({GIFTS}) g WHERE gift_date BETWEEN :ps AND :e
        GROUP BY GROUPING SETS ((gift_date),(COALESCE(campaign,'(unspecified)')))
    """, {"s": start, "e": end, "ps": prior_start})
    daily = [r for r in chart_rows if not r["is_campaign"]]
    current, prior = period(db, start, end), period(db, prior_start, prior_end)
    keys = ("giving", "active_partners", "retention_yoy", "average_gift")
    kpis = {k: delta(current[k], prior[k]) for k in keys}
    for k in keys:
        kpis[k]["sparkline"] = []
    for offset in range(-11, 1):
        m = month_shift(end.replace(day=1), offset)
        values = period(db, m, min(month_shift(m, 1)-timedelta(days=1), end))
        for k in keys:
            kpis[k]["sparkline"].append({"month": m.isoformat(), "value": values[k]})
    kpis["retention_yoy"].update(denominator=current["retention_base"], retained=current["retained"],
                                prior_denominator=prior["retention_base"])
    paired = []
    cursor = start
    while cursor <= end:
        stop = min(month_shift(cursor.replace(day=1), 1)-timedelta(days=1), end)
        ps = prior_start + (cursor-start)
        pe = ps + (stop-cursor)
        amounts = {
            "amount": sum(r["amount"] for r in daily if cursor <= r["gift_date"] <= stop),
            "gifts": sum(r["gifts"] for r in daily if cursor <= r["gift_date"] <= stop),
            "prior_amount": sum(r["amount"] for r in daily if ps <= r["gift_date"] <= pe),
        }
        paired.append({"month": cursor.replace(day=1).isoformat(), "from": cursor.isoformat(),
                       "to": stop.isoformat(), "prior_from": ps.isoformat(), "prior_to": pe.isoformat(),
                       "amount": float(amounts["amount"]), "prior_amount": float(amounts["prior_amount"]),
                       "gifts": amounts["gifts"]})
        cursor = stop + timedelta(days=1)
    counts = {r["status"]: r["count"] for r in statuses(db, end)}
    last_month = end.replace(day=1)-timedelta(days=1)
    old_counts = {r["status"]: r["count"] for r in statuses(db, last_month)}
    opted = _email_opted_in_count(db)
    givers = sum(v for k, v in counts.items() if k != "prospect")
    status = [{"status": k, "count": counts.get(k, 0),
               "share": 100 * counts.get(k, 0)/givers if givers else 0}
              for k in ("active", "new", "reactivated", "lapsing", "lapsed")]
    campaigns = [{"campaign": r["campaign"], "gifts": r["current_gifts"],
                  "amount": r["current_amount"],
                  "average_gift": r["current_amount"]/r["current_gifts"]}
                 for r in chart_rows if r["is_campaign"] and r["current_gifts"]]
    campaigns = sorted(campaigns, key=lambda r: (-r["amount"], -r["gifts"], r["campaign"]))[:5]
    for c in campaigns:
        c.update(amount=float(c["amount"]), average_gift=float(c["average_gift"]))
        c["share"] = 100*c["amount"]/current["giving"] if current["giving"] else 0
    stats = {"profiles": {"value": profiles},
             "recurring_partners": delta(recurring(db, end), recurring(db, last_month)),
             "email_opted_in": {"value": opted, "percentage": 100*opted/profiles if profiles else 0},
             "lapsing": delta(counts.get("lapsing", 0), old_counts.get("lapsing", 0))}
    rolling = period(db, end-timedelta(days=364), end)
    prior_rolling = period(db, prior_end-timedelta(days=364), prior_end)
    prior_givers = sum(r["count"] for r in statuses(db, prior_end) if r["status"] != "prospect")
    def current_only(value):
        return {"value": value, "prior": None, "change": None, "change_pct": None, "delta_label": "—"}
    metrics = dict(kpis)
    metrics.update(active_profiles=current_only(profiles), donors=delta(givers, prior_givers),
                   active_donors_12m=delta(rolling["active_partners"], prior_rolling["active_partners"]),
                   giving_12m=delta(rolling["giving"], prior_rolling["giving"]),
                   avg_gift=kpis["average_gift"], recurring_donors=stats["recurring_partners"],
                   email_opted_in=current_only(opted))
    return {"range": {"from": start.isoformat(), "to": end.isoformat(),
                      "prior_from": prior_start.isoformat(), "prior_to": prior_end.isoformat()},
            "metrics": metrics,
            "charts": {"monthly_giving": paired,
                       "donor_status": [{"status": s["status"], "profiles": s["count"]} for s in status]
                                       + [{"status": "prospect", "profiles": counts.get("prospect", 0)}],
                       "top_campaigns": campaigns},
            "overview": {"kpis": kpis, "stats": stats, "monthly_giving": paired,
                         "top_two_month_share": 100*sum(sorted((x["amount"] for x in paired), reverse=True)[:2])/current["giving"] if current["giving"] else None,
                         "campaigns": campaigns, "campaigns_href": "/?tab=giving",
                         "partner_status": {"givers": givers, "statuses": status,
                                            "prospects": counts.get("prospect", 0)},
                         "attention": []}}


def health_counts(db):
    from app.dashboards.cache import cached
    return cached(db, ("health-counts",), lambda: rows(db, """SELECT
      (SELECT count(*) FROM source_records WHERE resolved_at IS NULL) AS unresolved,
      (SELECT count(*) FROM identifier_blocklist b WHERE reason='high_cardinality'
         AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.action='data_health.blocklist.approve'
                         AND a.details->>'blocklist_id'=b.id::text)) AS review""")[0])


def attention(db, role, lapsing):
    health = health_counts(db)
    items = []
    def add(severity, title, explanation, href):
        items.append({"severity": severity, "title": title, "explanation": explanation, "href": href})
    if role != "viewer":
        failed = rows(db, "SELECT id FROM imports WHERE status='failed' ORDER BY id DESC LIMIT 1")
        if failed:
            add("error", "Import needs review", "A failed import needs investigation.", f"/imports?import_id={failed[0]['id']}")
    if health["unresolved"]:
        add("warning", "Unresolved source records", f'{health["unresolved"]:,} records await identity resolution.', "/data-health")
    if health["review"]:
        add("warning", "Identifiers need review", f'{health["review"]:,} high-cardinality identifiers await approval.', "/data-health")
    if lapsing:
        add("warning", "Partners are lapsing", f"{lapsing:,} partners last gave 366–730 days ago.", "/segments")
    if role == "admin":
        failed_jobs = rows(db, "SELECT count(*) AS n FROM jobs WHERE status='failed'")[0]["n"]
        if failed_jobs:
            add("error", "Failed background jobs", f"{failed_jobs:,} jobs need investigation.", "/system")
    ranks = {"error": 0, "warning": 1, "notice": 2, "healthy": 3}
    return sorted(items, key=lambda x: ranks[x["severity"]])[:5]