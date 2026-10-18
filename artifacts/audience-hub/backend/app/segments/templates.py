"""Audited editable audience templates; legacy definitions retained for benchmarks."""
def legacy_template_definitions():
    def trait(field, operator, value=None):
        node = {"type": "trait", "field": field, "operator": operator}
        if value is not None:
            node["value"] = value
        return node
    definitions = [
        ("lapsed-high-value", "Lapsed partners — lifetime giving $500+", "Reconnect with established partners.",
         [trait("donor_status", "in", ["lapsed"]), trait("ltv_total", "gte", 500)]),
        ("new-partners", "New giving partners — last 90 days", "Welcome partners after their first gift.",
         [trait("first_gift_date", "in_last_days", 90)]),
        ("recurring-partners", "Active recurring partners", "Partners with an active recurring gift.",
         [trait("is_recurring_active", "is_true")]),
        ("top-giving", "Top 5% of giving partners", "Top 5% of positive 12-month giving; boundary ties included.",
         [trait("top_5_percent_12m", "is_true")]),
        ("engaged-not-giving", "Engaged partners who have not given", "Three or more video views in 30 days, without a gift.",
         [trait("video_views_30d", "gte", 3), trait("gift_count_total", "eq", 0)]),
        ("lapsing-opted-in", "Email-permitted partners with past giving", "Two or more past gifts, none in the last 365 days.",
         [{"type": "consent", "channel": "email", "status": "opted_in"},
          trait("gift_count_12m", "eq", 0), trait("gift_count_total", "gte", 2)]),
    ]
    return [{"key": key, "name": name, "description": description,
             "definition": {"version": 1, "root": {"op": "and", "rules": rules}}}
            for key, name, description, rules in definitions]


def template_definitions():
    """Audited source-evidence rules, not computed donor/RFM classifications."""
    def a(field, operator="exists", value=None):
        node = {"type": "attribute", "field": field, "operator": operator}
        if value is not None:
            node["value"] = value
        return node

    def group(*rules, op="and", negate=False):
        return {"op": op, "rules": list(rules), "negate": negate}

    def donation(days=None, years=None, **extra):
        node = {"type": "event", "name": "donation", "aggregate": "exists", "operator": "is_true", **extra}
        if days:
            node["window"] = {"kind": "previous_days", "days": days}
        if years:
            node["window"] = {"kind": "previous_years", "years": years}
        return node

    plan = a("tbn_plus_subscription", "in", ["basic", "premium_monthly", "premium_annual"])
    basic = a("tbn_plus_subscription", "eq", "basic")
    account = a("donor_account_number")
    phone = {"type": "profile", "field": "phone", "operator": "exists"}
    rows = [
        ("all-tbn-reference", "All TBN Reference", [{"type": "profile", "field": "master_id", "operator": "exists"}]),
        ("has-active-email", "Has Active Email", [a("has_active_email", "is_true")]),
        ("active-120-days", "Active (120 days)", [group(a("last_seen", "within_previous_days", 120), a("last_contact", "within_previous_days", 120), op="or")]),
        ("tbn-plus-subscription", "TBN Plus Subscription", [plan]),
        ("tbn-plus-free-users", "TBN Plus Free Users", [basic]),
        ("tbn-plus-premium", "TBN Plus Premium", [a("tbn_plus_subscription", "in", ["premium_monthly", "premium_annual"])]),
        ("premium-upgraders", "Premium Upgraders", [a("tbn_plus_subscription_update", "starts_with", "Upgrade")]),
        ("tbn-app-non-donors", "TBN App Non-Donors", [plan, group(account, negate=True), group(donation(720), negate=True)]),
        ("tbn-app-donors", "TBN App Donors", [plan, donation(720)]),
        ("basic-donors", "Basic Donors", [basic, account, a("lifetime_amount", "gt", 0)]),
        ("basic-recurring-donors", "Basic Recurring Donors", [basic, a("has_recurring_donation", "is_true")]),
        ("basic-one-time-donors", "Basic One-Time Donors", [basic, a("rfm_raw_frequency", "eq", 1)]),
        ("rfm-all-donors", "RFM TBN All Donor Reference", [a("donor_account_status"), a("rfm_date")]),
        ("rfm-lapsed-donors", "RFM Lapsed Donors", [a("rfm_recency_score", "eq", 0)]),
        ("partner-names-non-donors", "Partner Names Non-Donors", [account, group(a("rfm_date"), negate=True)]),
        ("donors-past-30-days", "Donors Past 30 Days", [donation(30)]),
        ("living-legacy-prospects-v2", "Living Legacy Prospects V2", [a("donation_likelihood_prob", "gt", .75), a("wealth_score", "gt", 4), a("amt_suggested", "gt", 500), donation(years=5)]),
        ("giving-tuesday-2024", "Giving Tuesday 2024", [donation(window={"kind": "between", "from": "2024-12-03", "to": "2024-12-03"}, where={"properties.source_code": {"operator": "in", "value": ["T4XWDGT", "T4XPCGT", "T4YWDGT", "T4YPCGT"]}})]),
        ("mobile-subscribed", "Mobile Subscribed", [a("mobile_mktg_optin_tbn", "is_true"), phone]),
        ("call-center-sms-optins", "Call Center SMS Opt-ins", [a("tbn_call_center_sms_optin", "is_true"), phone]),
        ("sms-optout-date", "SMS Opt-Out Date Exists", [a("mobile_mktg_optout_date_tbn")]),
        ("vod-optouts", "VOD Optouts", [a("tbn_video_on_demand_updates_opt_status", "is_false")]),
        ("vod-viewed", "Customers with VOD Viewed", [{"type": "event", "name": "vod_viewed", "aggregate": "exists", "operator": "is_true"}]),
    ]
    return [{"key": key, "name": name, "description": "Audited Zeta audience; editable source-evidence rules.",
             "definition": {"version": 1, "root": group(*rules)}} for key, name, rules in rows]