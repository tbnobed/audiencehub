"""Six editable partner templates; service owns idempotent persistence."""
def template_definitions():
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