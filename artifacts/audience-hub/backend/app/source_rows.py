"""Shared column matching for warehouse pulls and Zeta exit files."""
import json
import re

from app.imports.mapping import suggest_mapping
from app.source_connectors import RESOURCES, SourceError, TARGET_FEEDS

IDENTITY_KEYS = ("zeta_cdp_id", "zeta_uid", "donor_account_number", "hubspot_record_id", "okta_customer_id")
ZETA_ALIASES = {
    "cdp_id": "zeta_cdp_id", "zeta_profile_id": "zeta_cdp_id",
    "uid": "zeta_uid", "zeta_user_id": "zeta_uid", "user_id": "zeta_uid",
    "hubspot_id": "hubspot_record_id", "okta_id": "okta_customer_id",
    "donor_account": "donor_account_number",
}


def matched_columns(headers, target="people"):
    kind = "contact" if target in {"people", "model_scores", "lookups", "models"} else "event"
    matched = suggest_mapping(headers, kind)
    for name in headers:
        lower = name.lower().lstrip("$").replace("properties.", "")
        if lower in IDENTITY_KEYS or lower in ZETA_ALIASES:
            matched[name] = ZETA_ALIASES.get(lower, lower)
    return matched


def mapped_row(incoming, mapping, *, require_zeta=False):
    target = mapping.get("target_type")
    feed = mapping["feed"]
    columns = matched_columns(list(incoming), target or feed)
    columns.update(mapping.get("columns", {}))
    row = {}
    for key, value in incoming.items():
        destination = columns.get(key) or (key.lower() if key.lower() in {
            "id", "properties", "attributes", "timestamp", "event_timestamp", "event",
            "type", "name", "date", "gift_date", "occurred_at", "external_id",
        } else key)
        if destination in {"properties", "attributes"}:
            if value is None or value == "":
                value = {}
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    raise SourceError("Properties/attributes columns must contain JSON objects.") from None
            if not isinstance(value, dict):
                raise SourceError("Properties/attributes columns must contain JSON objects.")
            row[destination] = {**value, **row.get(destination, {})}
            continue
        if destination.startswith("attributes.") or destination.startswith("properties."):
            parent, field = destination.split(".", 1)
            row.setdefault(parent, {})[field] = value
        else:
            row[destination] = value
    for key in ("properties", "attributes"):
        if isinstance(row.get(key), str):
            try:
                row[key] = json.loads(row[key])
            except ValueError:
                raise SourceError("Properties/attributes columns must contain JSON objects.") from None
    # Preserve nested Zeta identities for event references as well as people.
    for key in ("properties", "attributes"):
        nested = row.get(key, {})
        if isinstance(nested, dict):
            for name, value in nested.items():
                canonical = ZETA_ALIASES.get(name.lower(), name.lower())
                if canonical in IDENTITY_KEYS and canonical not in row:
                    row[canonical] = value
    if row.get("zeta_uid"):
        row["zeta_uid"] = str(row["zeta_uid"]).lower()
    if feed in {"people", "models", "lookups"}:
        if feed == "people" and not row.get("external_id") and (row.get("zeta_cdp_id") or row.get("zeta_uid")):
            row["external_id"] = str(row.get("zeta_cdp_id") or row["zeta_uid"])
        invalid_names = {key: row.pop(key) for key in list(row)
                         if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key)}
        if invalid_names:
            row["_raw_attributes"] = invalid_names
        if require_zeta:
            identity = row.get("zeta_cdp_id") or row.get("zeta_uid")
            if not identity:
                raise SourceError("Zeta people/model/lookup rows require zeta_cdp_id or zeta_uid.")
            row["external_id"] = str(identity)
            if feed in {"models", "lookups"}:
                import hashlib
                evidence = {k: row[k] for k in IDENTITY_KEYS if row.get(k)}
                suffix = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()[:24]
                row["external_id"] = f"{feed}:{identity}:{suffix}"
    elif feed == "events":
        if target in TARGET_FEEDS and TARGET_FEEDS[target] == "events":
            row["name"] = target
        if not row.get("occurred_at"):
            row["occurred_at"] = (row.pop("gift_date", None) or row.pop("date", None)
                                  or row.pop("timestamp", None) or row.pop("event_timestamp", None))
        if require_zeta:
            identity = row.get("zeta_cdp_id") or row.get("zeta_uid") or row.get("contact_external_id")
            if not identity:
                raise SourceError("Zeta event rows require a Zeta profile identity.")
            row["contact_external_id"] = str(identity)
        if not row.get("external_id") and not row.get("id"):
            if row.get("message_id"):
                row["external_id"] = row["message_id"]
                return row
            # Stable content identity for exports without message IDs.
            import hashlib
            from app.source_pulls import json_default
            row["external_id"] = hashlib.sha256(
                json.dumps(row, sort_keys=True, default=json_default).encode()).hexdigest()
    elif feed == "resources":
        row.setdefault("key", row.get("id") or row.get("source_code") or row.get("product_id") or row.get("vod_title_id"))
    return row


def identity_aliases(row, feed):
    """Make real Zeta references attachable without adding an identity key type.

    Source-local aliases point to the same real CDP/UID evidence and hence the
    same master; they are never independent financial identity vertices.
    """
    yield row
    if feed == "people":
        primary = row.get("external_id") or row.get("zeta_cdp_id") or row.get("zeta_uid")
        for value in dict.fromkeys(row.get(key) for key in ("zeta_cdp_id", "zeta_uid")):
            if value and str(value) != str(primary):
                yield {**row, "external_id": str(value)}
