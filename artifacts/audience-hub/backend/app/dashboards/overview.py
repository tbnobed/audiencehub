"""Live, resolved-profile overview aggregates. See docs/OVERVIEW_API.md."""
from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy import text


def rows(db, sql, params=None):
    return [dict(r) for r in db.execute(text(sql), params or {}).mappings()]


def month_shift(day, offset):
    year, month = divmod(day.year * 12 + day.month - 1 + offset, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def delta(value, prior):
    return {"value": value, "prior": prior, "change": value - prior,
            "change_pct": (value - prior) / prior * 100 if prior else None,
            "delta_label": "—" if value == prior == 0 else "New" if prior == 0 else None}


GIFTS = "SELECT g.* FROM gifts g JOIN active_profiles p ON p.id=g.profile_id"


def period(db, start, end):
    r = rows(db, f"""
        WITH resolved AS ({GIFTS}), current_partners AS (
          SELECT DISTINCT profile_id FROM resolved WHERE gift_date BETWEEN :s AND :e
        ), year_ago AS (
          SELECT DISTINCT profile_id FROM resolved WHERE gift_date BETWEEN :ys AND :ye
        )
        SELECT COALESCE(sum(amount),0) AS giving, count(DISTINCT profile_id) AS active_partners,
          COALESCE(avg(amount),0) AS average_gift,
          (SELECT count(*) FROM year_ago) AS retention_base,
          (SELECT count(*) FROM year_ago JOIN current_partners USING(profile_id)) AS retained
        FROM resolved WHERE gift_date BETWEEN :s AND :e
    """, {"s": start, "e": end, "ys": month_shift(start, -12), "ye": month_shift(end, -12)})[0]
    return {"giving": float(r["giving"]), "active_partners": r["active_partners"],
            "average_gift": float(r["average_gift"]),
            "retention_yoy": 100 * r["retained"] / r["retention_base"] if r["retention_base"] else 0,
            "retention_base": r["retention_base"], "retained": r["retained"]}


def statuses(db, end):
    return rows(db, f"""
        WITH resolved AS ({GIFTS}), history AS (
          SELECT profile_id, gift_date,
            lag(gift_date) OVER(PARTITION BY profile_id ORDER BY gift_date, id) AS previous,
            min(gift_date) OVER(PARTITION BY profile_id) AS first,
            row_number() OVER(PARTITION BY profile_id ORDER BY gift_date DESC, id DESC) AS rn
          FROM resolved WHERE gift_date <= :e
        ), classified AS (
          SELECT p.id, CASE WHEN h.gift_date IS NULL THEN 'prospect'
            WHEN CAST(:e AS date)-h.gift_date <= 365 AND h.first >= CAST(:e AS date)-365 THEN 'new'
            WHEN CAST(:e AS date)-h.gift_date <= 365 AND h.gift_date-h.previous > 730 THEN 'reactivated'
            WHEN CAST(:e AS date)-h.gift_date <= 365 THEN 'active'
            WHEN CAST(:e AS date)-h.gift_date <= 730 THEN 'lapsing'
            ELSE 'lapsed' END AS status
          FROM active_profiles p LEFT JOIN history h ON h.profile_id=p.id AND h.rn=1
        ) SELECT status, count(*) AS count FROM classified GROUP BY status
    """, {"e": end})


def recurring(db, end):
    return rows(db, f"""SELECT count(DISTINCT profile_id) AS n FROM ({GIFTS}) g
        WHERE is_recurring AND gift_date > CAST(:e AS date)-365 AND gift_date <= :e""",
                {"e": end})[0]["n"]


def build_overview(db, start, end, prior_start, prior_end):
    from app.dashboards.api import _email_opted_in_count
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
        amounts = rows(db, f"""SELECT
          COALESCE(sum(amount) FILTER(WHERE gift_date BETWEEN :s AND :e),0) AS amount,
          count(*) FILTER(WHERE gift_date BETWEEN :s AND :e) AS gifts,
          COALESCE(sum(amount) FILTER(WHERE gift_date BETWEEN :ps AND :pe),0) AS prior_amount
          FROM ({GIFTS}) g WHERE gift_date BETWEEN :ps AND :e
        """, {"s": cursor, "e": stop, "ps": ps, "pe": pe})[0]
        paired.append({"month": cursor.replace(day=1).isoformat(), "from": cursor.isoformat(),
                       "to": stop.isoformat(), "prior_from": ps.isoformat(), "prior_to": pe.isoformat(),
                       "amount": float(amounts["amount"]), "prior_amount": float(amounts["prior_amount"]),
                       "gifts": amounts["gifts"]})
        cursor = stop + timedelta(days=1)
    counts = {r["status"]: r["count"] for r in statuses(db, end)}
    last_month = end.replace(day=1)-timedelta(days=1)
    old_counts = {r["status"]: r["count"] for r in statuses(db, last_month)}
    profiles = rows(db, "SELECT count(*) AS n FROM active_profiles")[0]["n"]
    opted = _email_opted_in_count(db)
    givers = sum(v for k, v in counts.items() if k != "prospect")
    status = [{"status": k, "count": counts.get(k, 0),
               "share": 100 * counts.get(k, 0)/givers if givers else 0}
              for k in ("active", "new", "reactivated", "lapsing", "lapsed")]
    campaigns = rows(db, f"""SELECT COALESCE(campaign,'(unspecified)') AS campaign,
        count(*) AS gifts, avg(amount) AS average_gift, sum(amount) AS amount
        FROM ({GIFTS}) g WHERE gift_date BETWEEN :s AND :e
        GROUP BY 1 ORDER BY amount DESC, gifts DESC, campaign LIMIT 5""", {"s": start, "e": end})
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
                         "attention": attention(db, "viewer", counts.get("lapsing", 0))}}


def health_counts(db):
    return rows(db, """SELECT
      (SELECT count(*) FROM source_records WHERE resolved_at IS NULL) AS unresolved,
      (SELECT count(*) FROM identifier_blocklist b WHERE reason='high_cardinality'
         AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.action='data_health.blocklist.approve'
                         AND a.details->>'blocklist_id'=b.id::text)) AS review""")[0]


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