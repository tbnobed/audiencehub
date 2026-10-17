"""Published, resolved-profile overview aggregates. See docs/OVERVIEW_API.md."""
from calendar import monthrange, month_name
from datetime import date, timedelta

from sqlalchemy import text


def rows(db, sql, params=None):
    from app.dashboards.cache import check_deadline
    check_deadline(db)
    return [dict(r) for r in db.execute(text(sql), params or {}).mappings()]


def month_shift(day, offset):
    year, month = divmod(day.year * 12 + day.month - 1 + offset, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def retention_window(end):
    """Adjacent calendar-year windows, (end - 24m, end - 12m], (end - 12m, end]."""
    boundary = month_shift(end, -12)
    return {"from": (boundary + timedelta(days=1)).isoformat(), "to": end.isoformat(),
            "prior_from": (month_shift(end, -24) + timedelta(days=1)).isoformat(),
            "prior_to": boundary.isoformat()}


def delta(value, prior):
    return {"value": value, "prior": prior, "change": value - prior,
            "change_pct": (value - prior) / prior * 100 if prior else None,
            "delta_label": "—" if value == prior == 0 else "New" if prior == 0 else None}


GIFTS = ("SELECT day AS gift_date, gift_amount AS amount, gift_count, campaign "
         "FROM dashboard_daily WHERE active")


def overview_facts(db, windows, cutoffs):
    """One gift scan for all KPI windows and historical classifications.

    Aggregate at profile grain before counting partners; never materialize g.* or
    rerun a full-history retention/status query for each sparkline point.
    """
    params, columns, totals = {}, [], []
    for i, (start, end) in enumerate(windows):
        bounds = retention_window(end)
        params.update({f"s{i}": start, f"e{i}": end,
                       f"rs{i}": date.fromisoformat(bounds["from"]),
                       f"ys{i}": date.fromisoformat(bounds["prior_from"]),
                       f"ye{i}": date.fromisoformat(bounds["prior_to"])})
        current = f"gift_date BETWEEN :s{i} AND :e{i}"
        previous = f"gift_date BETWEEN :ys{i} AND :ye{i}"
        columns.extend([
            f"sum(0) FILTER(WHERE {current}) AS amount{i}",
            f"sum(gift_count) FILTER(WHERE {current}) AS gifts{i}",
            f"bool_or({previous}) AS base{i}",
            f"bool_or(gift_date BETWEEN :rs{i} AND :e{i}) AS recent{i}",
        ])
        totals.extend([
            f"COALESCE(sum(amount{i}),0) AS amount{i}",
            f"COALESCE(sum(gifts{i}),0) AS gifts{i}",
            f"COALESCE(sum(weight) FILTER(WHERE gifts{i}>0),0) AS partners{i}",
            f"COALESCE(sum(weight) FILTER(WHERE base{i}),0) AS base{i}",
            f"COALESCE(sum(weight) FILTER(WHERE base{i} AND recent{i}),0) AS retained{i}",
        ])
    for i, end in enumerate(cutoffs):
        params[f"cut{i}"] = end
        columns.extend([
            f"min(gift_date) FILTER(WHERE gift_date<=:cut{i}) AS first{i}",
            # Two latest gift dates, including same-date gifts, are needed for
            # reactivation. Dates tied by id have the same classification.
            f"(array_agg(gift_date ORDER BY gift_date DESC) "
            f"FILTER(WHERE gift_date<=:cut{i}))[1:2] AS latest{i}",
            f"(array_agg(gift_count ORDER BY gift_date DESC) "
            f"FILTER(WHERE gift_date<=:cut{i}))[1] AS latest_count{i}",
            f"bool_or(is_recurring AND gift_date>CAST(:cut{i} AS date)-365 "
            f"AND gift_date<=:cut{i}) AS recurring{i}",
        ])
        status = f"""CASE WHEN first{i} IS NULL THEN 'prospect'
            WHEN CAST(:cut{i} AS date)-latest{i}[1]<=365
              AND first{i}>=CAST(:cut{i} AS date)-365 THEN 'new'
            WHEN CAST(:cut{i} AS date)-latest{i}[1]<=365
              AND latest_count{i}=1 AND latest{i}[1]-latest{i}[2]>730 THEN 'reactivated'
            WHEN CAST(:cut{i} AS date)-latest{i}[1]<=365 THEN 'active'
            WHEN CAST(:cut{i} AS date)-latest{i}[1]<=730 THEN 'lapsing'
            ELSE 'lapsed' END"""
        totals.extend(f"COALESCE(sum(weight) FILTER(WHERE ({status})='{s}'),0) AS {s}{i}"
                      for s in ("new", "reactivated", "active", "lapsing", "lapsed"))
        totals.append(f"COALESCE(sum(weight) FILTER(WHERE recurring{i}),0) AS recurring{i}")
    params["max_date"] = max([e for _, e in windows] + list(cutoffs))
    r = rows(db, f"""
        WITH facts AS (
          SELECT g.profile_id,max(donors) AS weight, {','.join(columns)}
          FROM (SELECT p.id AS profile_id,p.donors,d.gift_date,d.gift_count,d.is_recurring
                FROM dashboard_donor_patterns p CROSS JOIN LATERAL
                unnest(p.days,p.gift_counts,p.recurring_flags)
                AS d(gift_date,gift_count,is_recurring)) g
          WHERE gift_date<=:max_date GROUP BY g.profile_id
        ) SELECT {','.join(totals)} FROM facts
    """, params)[0]
    money = rows(db, f"""
      SELECT {','.join(
          f"COALESCE(sum(gift_amount) FILTER(WHERE day BETWEEN :s{i} AND :e{i}),0) AS amount{i}, "
          f"COALESCE(sum(gift_count) FILTER(WHERE day BETWEEN :s{i} AND :e{i}),0) AS gifts{i}"
          for i in range(len(windows)))}
      FROM dashboard_daily WHERE active
    """, params)[0]
    r.update(money)
    periods = {}
    for i, window in enumerate(windows):
        periods[window] = {
            "giving": float(r[f"amount{i}"]),
            "average_gift": float(r[f"amount{i}"]/r[f"gifts{i}"]) if r[f"gifts{i}"] else 0,
            "active_partners": int(r[f"partners{i}"]),
            "retention_base": int(r[f"base{i}"]), "retained": int(r[f"retained{i}"]),
            "retention_yoy": float(100*r[f"retained{i}"]/r[f"base{i}"]) if r[f"base{i}"] else 0,
        }
    return periods, {
        end: {s: int(r[f"{s}{i}"]) for s in ("new", "reactivated", "active", "lapsing", "lapsed")}
        for i, end in enumerate(cutoffs)
    }, {end: int(r[f"recurring{i}"]) for i, end in enumerate(cutoffs)}


def build_overview(db, start, end, prior_start, prior_end, *, include_email=True):
    from app.dashboards.api import _email_opted_in_count, _kpi
    windows = [(start, end), (prior_start, prior_end)]
    windows += [(m, min(month_shift(m, 1)-timedelta(days=1), end))
                for m in (month_shift(end.replace(day=1), offset) for offset in range(-11, 1))]
    windows += [(end-timedelta(days=364), end), (prior_end-timedelta(days=364), prior_end)]
    last_month = end.replace(day=1)-timedelta(days=1)
    periods, classifications, recurring_counts = overview_facts(
        db, list(dict.fromkeys(windows)), list(dict.fromkeys((end, last_month, prior_end))))
    profiles = int(rows(db, "SELECT value AS n FROM dashboard_kpis WHERE key='profiles'")[0]["n"])
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
          sum(gift_count) AS gifts, sum(amount) AS amount,
          sum(gift_count) FILTER(WHERE gift_date BETWEEN :s AND :e) AS current_gifts,
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
                                prior_denominator=prior["retention_base"],
                                window=retention_window(end),
                                prior_window=retention_window(prior_end),
                                definition="Of partners who gave in the preceding 12 months, the share "
                                           "who also gave in the trailing 12 months ending on the range end.")
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
                       "gifts": int(amounts["gifts"])})
        cursor = stop + timedelta(days=1)
    # Status cards are current traits, never reconstructed at the selected range end.
    counts = {r["status"]: r["count"] for r in _kpi(db, "partner_status")}
    counts["prospect"] = counts.get("prospect", profiles-sum(counts.values()))
    traits_as_of = rows(db, "SELECT as_of FROM dashboard_kpis WHERE key='partner_status'")[0]["as_of"].isoformat()
    opted = _email_opted_in_count(db) if include_email else None
    givers = sum(v for k, v in counts.items() if k != "prospect")
    status = [{"status": k, "count": counts.get(k, 0),
               "share": 100 * counts.get(k, 0)/givers if givers else 0}
              for k in ("active", "new", "reactivated", "lapsing", "lapsed")]
    campaigns = [{"campaign": r["campaign"], "gifts": int(r["current_gifts"]),
                  "amount": r["current_amount"],
                  "average_gift": r["current_amount"]/r["current_gifts"]}
                 for r in chart_rows if r["is_campaign"] and r["current_gifts"]]
    campaigns = sorted(campaigns, key=lambda r: (-r["amount"], -r["gifts"], r["campaign"]))[:5]
    for c in campaigns:
        c.update(amount=float(c["amount"]), average_gift=float(c["average_gift"]))
        c["share"] = 100*c["amount"]/current["giving"] if current["giving"] else 0
    def current_only(value):
        return {"value": value, "prior": None, "change": None, "change_pct": None, "delta_label": "—"}
    status_meta = {"basis": "current_profile_traits", "as_of": traits_as_of, "label": "as of today"}
    stats = {"profiles": {"value": profiles},
             "recurring_partners": {**current_only(int(_kpi(db, "recurring_partners"))), **status_meta},
             "lapsing": {**current_only(int(_kpi(db, "lapsing"))), **status_meta}}
    if include_email:
        stats["email_opted_in"] = {"value": opted, "percentage": 100*opted/profiles if profiles else 0}
    rolling = period(db, end-timedelta(days=364), end)
    prior_rolling = period(db, prior_end-timedelta(days=364), prior_end)
    prior_givers = sum(r["count"] for r in statuses(db, prior_end) if r["status"] != "prospect")
    metrics = dict(kpis)
    metrics.update(active_profiles=current_only(profiles), donors=delta(givers, prior_givers),
                   active_donors_12m=delta(rolling["active_partners"], prior_rolling["active_partners"]),
                   giving_12m=delta(rolling["giving"], prior_rolling["giving"]),
                   avg_gift=kpis["average_gift"], recurring_donors=stats["recurring_partners"])
    if include_email:
        metrics["email_opted_in"] = current_only(opted)
    # Distinct calendar months across all years, using exactly the plotted totals.
    calendar_totals = {}
    for bucket in paired:
        month = int(bucket["month"][5:7])
        calendar_totals[month] = calendar_totals.get(month, 0) + bucket["amount"]
    top_months = [{"month": m, "name": month_name[m], "amount": amount}
                  for m, amount in sorted(calendar_totals.items(), key=lambda x: (-x[1], x[0]))[:2]]
    chart_total = sum(bucket["amount"] for bucket in paired)
    return {"range": {"from": start.isoformat(), "to": end.isoformat(),
                      "prior_from": prior_start.isoformat(), "prior_to": prior_end.isoformat()},
            "metrics": metrics,
            "charts": {"monthly_giving": paired,
                       "donor_status": [{"status": s["status"], "profiles": s["count"]} for s in status]
                                       + [{"status": "prospect", "profiles": counts.get("prospect", 0)}],
                       "top_campaigns": campaigns},
            "overview": {"kpis": kpis, "stats": stats, "monthly_giving": paired,
                          "top_two_month_share": 100*sum(x["amount"] for x in top_months)/chart_total if chart_total else None,
                          "top_two_months": top_months,
                         "campaigns": campaigns, "campaigns_href": "/?tab=giving",
                          "partner_status": {"givers": givers, "statuses": status, **status_meta,
                                            "prospects": counts.get("prospect", 0)},
                         "attention": []}}


def current_status_cards(db, payload):
    """Keep current-trait cards consistent even while date charts use SWR.

    Only tiny published KPIs are read here; never scan live traits/gifts to
    refresh a request. One statement sees one complete published generation.
    """
    published = {r["key"]: r for r in rows(db, """
      SELECT key,value,as_of FROM dashboard_kpis
      WHERE key IN ('partner_status','lapsing','recurring_partners','profiles')
    """)}
    counts = {r["status"]: r["count"] for r in published["partner_status"]["value"]}
    profiles = int(published["profiles"]["value"])
    prospects = counts.get("prospect", profiles-sum(counts.values()))
    givers = sum(v for k, v in counts.items() if k != "prospect")
    meta = {"basis": "current_profile_traits", "label": "as of today",
            "as_of": published["partner_status"]["as_of"].isoformat()}
    status = [{"status": key, "count": counts.get(key, 0),
               "share": 100*counts.get(key, 0)/givers if givers else 0}
              for key in ("active", "new", "reactivated", "lapsing", "lapsed")]
    overview = payload["overview"]
    overview["partner_status"] = {"givers": givers, "statuses": status, "prospects": prospects, **meta}
    for key in ("lapsing", "recurring_partners"):
        overview["stats"][key] = {
            "value": int(published[key]["value"]), "prior": None, "change": None,
            "change_pct": None, "delta_label": "—", **meta}
    payload["charts"]["donor_status"] = [{"status": r["status"], "profiles": r["count"]} for r in status]
    payload["charts"]["donor_status"].append({"status": "prospect", "profiles": prospects})
    payload["metrics"]["recurring_donors"] = overview["stats"]["recurring_partners"]
    return payload


def health_counts(db, role="admin"):
    from app.dashboards.health import health_counts as count_issues
    return count_issues(db, role)


def attention(db, role, lapsing):
    from app.dashboards.health import attention as issue_attention
    return issue_attention(db, role, lapsing)