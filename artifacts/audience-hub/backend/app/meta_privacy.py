"""Fail-closed Meta data policy: never ship or select on viewing information."""
import re
import math
from sqlalchemy import text

IDENTITY_FIELDS = {"master_id", "email", "phone", "phone_e164", "first_name", "last_name",
                   "city", "region", "postal_code", "country", "id"}
GIVING_FIELDS = {
    "donor_status", "lifetime_amount", "lifetime_count", "last_donation_at",
    "last_gift_at", "last_gift_amount", "gift_count_12m", "gift_amount_12m",
    "rfm_recency", "rfm_frequency", "rfm_monetary", "rfm_score",
    "rfm_raw_recency", "rfm_raw_frequency", "rfm_raw_monetary",
    "has_recurring_donation", "last_donation_amount", "first_donation_date",
    "last_donation_date", "top_5_percent_12m", "household_id", "household_size",
}
SAFE_EVENTS = {"donation", "donation_success", "donation_return", "signup", "sign_up",
               "account_created", "meta_lead", "consent_changed", "email_clicked"}
UNSAFE = re.compile(r"vod|video|view(?:ed|ing)?|watch|stream|playback|episode|program|title|content", re.I)


def safe_rule(db, definition, seen=None):
    seen = set() if seen is None else seen

    def visit(node):
        if not isinstance(node, dict):
            raise ValueError("Meta audience rules must be structured rules.")
        kind = node.get("type")
        if "rules" in node or "op" in node:
            for child in node.get("rules", []):
                visit(child)
        elif kind == "segment":
            identifier = node.get("segment_id")
            if type(identifier) is not int or identifier in seen or len(seen) >= 50:
                raise ValueError("Meta audience references cannot be cyclic or unavailable.")
            seen.add(identifier)
            nested = db.scalar(text("SELECT definition FROM segments WHERE id=:id"), {"id": identifier})
            if nested is None:
                raise ValueError("Referenced Meta audience is unavailable.")
            safe_rule(db, nested, seen)
            seen.remove(identifier)
        elif kind == "consent":
            if node.get("channel") not in {"email", "ads_personalization", "sms", "phone", "mail"}:
                raise ValueError("Unsupported Meta consent rule.")
        elif kind in {"profile", "trait", "attribute"}:
            field = node.get("field", "")
            # No last_seen/activity, subscription/plan, enrichment/model scores
            # or source-membership proxies for viewing behavior.
            if field not in GIVING_FIELDS and not (kind == "profile" and field in IDENTITY_FIELDS):
                raise ValueError("Meta audiences allow only identity, giving, signup and consent-safe engagement rules.")
        elif kind in {"gift", "event"}:
            if kind == "event" and str(node.get("name", "")).strip().lower().replace(" ", "_") not in SAFE_EVENTS:
                raise ValueError("Meta event rules require an explicitly allowed non-viewing event name.")
            allowed = {"amount", "occurred_at", "source_code", "fund", "status", "type"}
            for key, value in node.get("where", {}).items():
                if key not in allowed or UNSAFE.search(str(value)):
                    raise ValueError("Meta event/gift filters cannot include viewing or resource attributes.")
        else:
            raise ValueError("Meta audiences cannot use source, enrichment, viewing or unclassified rules.")
    visit(definition.get("root", {}))


def safe_mapping(mapping):
    for target, spec in mapping.items():
        if (not isinstance(spec, dict) or spec.get("kind") != "profile"
                or spec.get("key") not in IDENTITY_FIELDS or UNSAFE.search(target)):
            raise ValueError("Meta exports allow only approved partner identity fields; no viewing attributes or constants.")


def safe_capi(payload):
    """Called at the Graph client boundary for any /events POST, including future callers."""
    if (not isinstance(payload, dict) or set(payload) - {"data", "test_event_code"}
            or not isinstance(payload.get("data"), list) or not 1 <= len(payload["data"]) <= 1000):
        raise ValueError("Unsupported Meta conversion payload.")
    if "test_event_code" in payload and not re.fullmatch(r"TEST[0-9]{1,20}", str(payload["test_event_code"])):
        raise ValueError("Invalid Meta test event code.")
    allowed_names = {"Donate", "CompleteRegistration", "Lead", "Contact"}
    for event in payload["data"]:
        if (not isinstance(event, dict) or event.get("event_name") not in allowed_names or
                set(event) - {"event_name", "event_time", "event_id", "action_source", "user_data", "custom_data"}):
            raise ValueError("Only giving, signup and consent-safe conversion events may be sent to Meta.")
        if (not isinstance(event.get("user_data"), dict) or not event["user_data"]
                or set(event["user_data"]) - {"em", "ph", "external_id", "fbp", "fbc"}):
            raise ValueError("Unsupported Meta conversion identity field.")
        for key, value in event.get("user_data", {}).items():
            values = value if isinstance(value, list) else [value]
            pattern = r"[0-9a-f]{64}" if key in {"em", "ph", "external_id"} else r"fb\.[0-9]+\.[0-9]+\.[A-Za-z0-9._~-]{1,512}"
            if not values or len(values) > 1000 or any(not isinstance(v, str) or not re.fullmatch(pattern, v) for v in values):
                raise ValueError("Meta conversion identities must use approved hashes or click-cookie formats.")
        if event.get("action_source") not in {"website", "app", "physical_store", "system_generated", "other"}:
            raise ValueError("Unsupported Meta conversion action source.")
        if type(event.get("event_time")) is not int or event["event_time"] <= 0:
            raise ValueError("Invalid Meta conversion event time.")
        if "event_id" in event and not re.fullmatch(r"[0-9a-fA-F-]{16,64}", str(event["event_id"])):
            raise ValueError("Meta conversion event IDs must be opaque, not content identifiers.")
        custom = event.get("custom_data", {})
        if not isinstance(custom, dict) or set(custom) - {"value", "currency"}:
            raise ValueError("Meta custom data may contain only monetary value and currency.")
        if "value" in custom and (type(custom["value"]) not in (int, float)
                                  or not math.isfinite(custom["value"]) or custom["value"] < 0):
            raise ValueError("Invalid Meta conversion amount.")
        if "currency" in custom and not re.fullmatch(r"[A-Z]{3}", str(custom["currency"])):
            raise ValueError("Invalid Meta conversion currency.")
