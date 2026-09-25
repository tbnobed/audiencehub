"""CSV header matching and mapping validation for import record types."""

from __future__ import annotations

import re
from collections.abc import Mapping


RECORD_TYPES = ("contact", "gift", "event", "enrichment", "consent")

COMMON_TARGETS = {
    "external_id", "contact_external_id", "email", "phone", "first_name",
    "last_name", "address1", "address2", "city", "region", "postal_code",
    "country", "amount", "gift_date", "fund", "campaign", "appeal_code",
    "channel", "payment_method", "is_recurring", "recurring_plan_id",
    "name", "occurred_at", "contact_external_id", "status", "captured_at",
}
ALIASES = {
    "external id": "external_id", "record id": "external_id", "contact id": "contact_external_id",
    "contact external id": "contact_external_id", "email": "email",
    "e mail": "email", "email address": "email", "primary email": "email",
    "phone": "phone", "phone number": "phone", "mobile": "phone",
    "first name": "first_name", "firstname": "first_name",
    "last name": "last_name", "lastname": "last_name",
    "address": "address1", "address 1": "address1", "address line 1": "address1",
    "address 2": "address2", "address line 2": "address2",
    "city": "city", "state": "region", "region": "region",
    "zip": "postal_code", "zip code": "postal_code", "postal": "postal_code",
    "postal code": "postal_code", "country": "country",
    "amount": "amount", "gift amount": "amount", "gift amt": "amount",
    "gift date": "gift_date", "date": "gift_date", "received": "gift_date",
    "fund": "fund", "campaign": "campaign", "appeal code": "appeal_code",
    "channel": "channel", "payment method": "payment_method",
    "is recurring": "is_recurring", "recurring": "is_recurring",
    "recurring plan id": "recurring_plan_id", "event": "name",
    "event name": "name", "name": "name", "occurred at": "occurred_at",
    "timestamp": "occurred_at", "event date": "occurred_at",
    "message id": "message_id", "type": "type", "received at": "received_at",
    "context": "context", "properties": "properties",
    "consent channel": "channel", "consent status": "status",
    "status": "status", "captured at": "captured_at",
    "email consent": "attributes.email_consent",
    "consent captured at": "attributes.consent_captured_at",
    "hard bounce": "attributes.hard_bounce",
}
TYPE_TARGETS = {
    "contact": {"external_id", "email", "phone", "first_name", "last_name",
                "address1", "address2", "city", "region", "postal_code", "country"},
    "gift": {"external_id", "contact_external_id", "email", "phone", "amount",
             "gift_date", "fund", "campaign", "appeal_code", "channel",
             "payment_method", "is_recurring", "recurring_plan_id"},
    "event": {"external_id", "contact_external_id", "email", "phone", "name", "occurred_at",
              "message_id", "type", "received_at", "context", "properties"},
    "enrichment": {"external_id", "contact_external_id", "email", "phone"},
    "consent": {"contact_external_id", "email", "phone", "channel", "status", "captured_at"},
}
ENRICHMENT_TYPES = {"text", "number", "boolean", "date", "enum"}


def _header_key(value: str) -> str:
    return re.sub(r"[\W_]+", " ", value.strip().casefold()).strip()


def suggest_mapping(headers: list[str], record_type: str) -> dict[str, object]:
    """Suggest exact semantic fields only; unrelated headers remain unmapped."""
    if record_type not in RECORD_TYPES:
        raise ValueError(f"Unsupported record type: {record_type}")
    mapping: dict[str, object] = {}
    for header in headers:
        target = ALIASES.get(_header_key(header))
        if target in TYPE_TARGETS[record_type] or (
            record_type == "contact" and target and target.startswith("attributes.")
        ):
            mapping[header] = target
    return mapping


def validate_mapping(columns: Mapping[str, object], record_type: str,
                     headers: list[str]) -> dict[str, object]:
    if record_type not in RECORD_TYPES:
        raise ValueError(f"Unsupported record type: {record_type}")
    unknown_headers = set(columns) - set(headers)
    if unknown_headers:
        raise ValueError(f"Mapping contains unknown CSV headers: {', '.join(sorted(unknown_headers))}")
    normalized: dict[str, object] = {}
    enrichment_keys: set[str] = set()
    for header, value in columns.items():
        if isinstance(value, str):
            if value.startswith(("attributes.", "properties.")):
                prefix, key = value.split(".", 1)
                if not key or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
                    raise ValueError(f"Invalid mapped attribute target: {value}")
                if prefix == "properties" and record_type != "event":
                    raise ValueError("properties.* targets are supported only for event imports")
                if prefix == "attributes" and record_type != "contact":
                    raise ValueError("attributes.* targets are supported only for contact imports")
                normalized[header] = value
            elif value in TYPE_TARGETS[record_type]:
                normalized[header] = value
            else:
                raise ValueError(f"Unsupported mapping target for {record_type}: {value}")
        elif isinstance(value, Mapping):
            if record_type != "enrichment" or set(value) != {"enrichment", "data_type"}:
                raise ValueError("Object mapping is supported only for enrichment columns")
            key = str(value["enrichment"])
            data_type = str(value["data_type"])
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key) or data_type not in ENRICHMENT_TYPES:
                raise ValueError("Invalid enrichment key or data_type")
            if key in enrichment_keys:
                raise ValueError(f"Duplicate enrichment key: {key}")
            enrichment_keys.add(key)
            normalized[header] = {"enrichment": key, "data_type": data_type}
        else:
            raise ValueError(f"Invalid mapping for CSV header {header!r}")
    targets = [value for value in normalized.values() if isinstance(value, str)
               and not value.startswith(("attributes.", "properties."))]
    duplicates = {target for target in targets if targets.count(target) > 1}
    if duplicates:
        raise ValueError(f"Multiple columns map to the same target: {', '.join(sorted(duplicates))}")
    if record_type in {"enrichment", "consent"}:
        identifier_count = sum(target in {"contact_external_id", "email", "phone"}
                               for target in targets)
        if identifier_count != 1:
            raise ValueError(f"{record_type} mapping requires exactly one identifier column")
    return normalized