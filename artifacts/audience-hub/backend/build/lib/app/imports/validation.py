"""Pure CSV row conversion and validation used by previews, dry runs and jobs."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import phonenumbers

from app.identity.normalize import validated_email
from app.imports.mapping import RECORD_TYPES

IDENTIFIERS = {"contact_external_id", "external_id", "email", "phone"}
ALLOWED_CHANNELS = {"email", "sms", "phone", "mail", "ads_personalization"}
ALLOWED_CONSENTS = {"opted_in", "opted_out", "unknown"}


def normalize_email(value: str) -> str | None:
    email = validated_email(value)
    if email is None:
        return None
    email = email.casefold()
    local, domain = email.rsplit("@", 1)
    # gmail.test is a reserved, non-deliverable domain used by the seed data.
    gmail_domains = {
        item.strip().casefold() for item in
        os.getenv("GMAIL_STYLE_DOMAINS", "gmail.com,googlemail.com,gmail.test").split(",")
        if item.strip()
    }
    if domain in gmail_domains:
        local = local.split("+", 1)[0].replace(".", "")
        if domain == "googlemail.com":
            domain = "gmail.com"
    else:
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def normalize_phone(value: str, region: str = "US") -> str | None:
    try:
        parsed = phonenumbers.parse(value.strip(), region)
    except (phonenumbers.NumberParseException, ValueError):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164) \
        if phonenumbers.is_valid_number(parsed) else None


def _parse_date(value: str, configured_format: str | None) -> tuple[date, str | None]:
    value = value.strip()
    if configured_format:
        normalized_format = (
            configured_format.replace("YYYY", "%Y")
            .replace("MM", "%m")
            .replace("DD", "%d")
        )
        try:
            return datetime.strptime(value, normalized_format).date(), None
        except ValueError:
            pass

    fallback_formats = ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y")
    for format_string in fallback_formats:
        try:
            parsed = datetime.strptime(value, format_string).date()
        except ValueError:
            continue
        if format_string == "%Y-%m-%d":
            return parsed, "date_fallback_format" if configured_format else None
        if format_string == "%m/%d/%Y":
            month, day = (int(part) for part in value.split("/")[:2])
            if month != day and 1 <= month <= 12 and 1 <= day <= 12:
                return parsed, "date_ambiguous"
        return parsed, "date_fallback_format"
    raise ValueError("expected YYYY-MM-DD, MM/DD/YYYY, or Mon DD, YYYY")


def _date(value: str, configured_format: str | None) -> date:
    """Parse a date while retaining the established value-only helper API."""
    return _parse_date(value, configured_format)[0]


def _date_warning(header: str, category: str) -> str:
    if category == "date_ambiguous":
        return f"{header}: date_ambiguous: numeric date could be interpreted in more than one order"
    if category == "date_clamped":
        return f"{header}: date_clamped: invalid day was clamped to the end of its month"
    return f"{header}: date_fallback_format: parsed with a fallback date format"


def _datetime(value: str, configured_format: str | None) -> datetime:
    value = value.strip()
    if configured_format:
        parsed = datetime.strptime(value, configured_format)
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _convert(value: str, data_type: str, options: dict[str, Any]) -> Any:
    value = value.strip()
    if data_type in {"text", "enum"}:
        return value
    if data_type == "number":
        number = Decimal(value.replace(",", "").replace("$", ""))
        if not number.is_finite():
            raise ValueError("must be a finite number")
        return number
    if data_type == "boolean":
        normalized = value.casefold()
        if normalized in {"true", "yes", "y", "1", "on"}:
            return True
        if normalized in {"false", "no", "n", "0", "off"}:
            return False
        raise ValueError("must be true/false, yes/no, or 1/0")
    if data_type == "date":
        return _date(value, options.get("date_format"))
    raise ValueError(f"unsupported data type: {data_type}")


def _required_fields(record_type: str, mapped: dict[str, Any]) -> list[str]:
    if record_type == "gift":
        return [field for field in ("amount", "gift_date") if mapped.get(field) is None]
    if record_type == "event":
        return [field for field in ("name", "occurred_at") if not mapped.get(field)]
    if record_type == "enrichment":
        return [] if any(mapped.get(key) for key in IDENTIFIERS) else ["identifier"]
    if record_type == "consent":
        return [field for field in ("channel", "status", "captured_at") if not mapped.get(field)] + (
            [] if any(mapped.get(key) for key in IDENTIFIERS) else ["identifier"])
    return []


def map_and_validate_row(row: dict[str, str], columns: dict[str, Any], record_type: str,
                         options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return mapped values, normalized identifiers, errors, and warnings."""
    options = options or {}
    options.setdefault("phone_region", os.getenv("DEFAULT_PHONE_REGION", "US"))
    if record_type not in RECORD_TYPES:
        raise ValueError(f"Unsupported record type: {record_type}")
    values: dict[str, Any] = {}
    errors: list[str] = []
    warnings: list[str] = []
    enrichments: dict[str, tuple[str, Any]] = {}
    for header, target in columns.items():
        raw_value = (row.get(header) or "").strip()
        if not raw_value:
            continue
        try:
            if isinstance(target, dict):
                data_type = target["data_type"]
                if data_type == "date":
                    converted, date_warning = _parse_date(
                        raw_value, options.get("date_format")
                    )
                    if date_warning:
                        warnings.append(_date_warning(header, date_warning))
                else:
                    converted = _convert(raw_value, data_type, options)
                enrichments[target["enrichment"]] = (data_type, converted)
            elif target in {"properties.payload", "properties"}:
                properties = json.loads(raw_value)
                if not isinstance(properties, dict):
                    raise ValueError("must be a JSON object")
                values["properties"] = properties
            elif target == "context":
                context = json.loads(raw_value)
                if not isinstance(context, dict):
                    raise ValueError("must be a JSON object")
                values[target] = context
            elif target.startswith("attributes."):
                values.setdefault("attributes", {})[target.split(".", 1)[1]] = raw_value
            elif target.startswith("properties."):
                values.setdefault("properties", {})[target.split(".", 1)[1]] = raw_value
            elif target in {"amount"}:
                values[target] = Decimal(raw_value.replace(",", "").replace("$", ""))
                if not values[target].is_finite() or values[target] < 0 or values[target] >= Decimal("1000000000000"):
                    raise ValueError("must be a non-negative finite amount")
                if values[target].as_tuple().exponent < -2:
                    raise ValueError("must have no more than two decimal places")
            elif target == "gift_date":
                values[target], date_warning = _parse_date(
                    raw_value, options.get("date_format")
                )
                if date_warning:
                    warnings.append(_date_warning(header, date_warning))
            elif target in {"occurred_at", "received_at"}:
                values[target] = _datetime(raw_value, options.get("datetime_format"))
            elif target == "captured_at":
                values[target] = _datetime(raw_value, options.get("datetime_format"))
            elif target == "is_recurring":
                values[target] = _convert(raw_value, "boolean", options)
            elif target in {"first_name", "last_name"}:
                cleaned = " ".join(raw_value.split())
                values[target] = cleaned.title() if cleaned.isupper() or cleaned.islower() else cleaned
            elif target == "postal_code":
                postal = " ".join(raw_value.split())
                values[target] = postal[:5] if options.get("phone_region", "US") == "US" else postal
            else:
                values[target] = raw_value
        except (ValueError, InvalidOperation, TypeError, KeyError) as exc:
            errors.append(f"{header}: {exc}")
    for field in _required_fields(record_type, values):
        errors.append(f"Required field missing: {field}")
    if record_type == "contact" and not any(
        values.get(key) for key in ("external_id", "email", "phone", "first_name", "last_name",
                                    "address1", "city", "region", "postal_code", "country")
    ):
        errors.append("Row contains no mapped contact values")
    if record_type == "consent":
        if values.get("channel") and values["channel"] not in ALLOWED_CHANNELS:
            errors.append("channel must be one of " + ", ".join(sorted(ALLOWED_CHANNELS)))
        if values.get("status") and values["status"] not in ALLOWED_CONSENTS:
            errors.append("status must be one of " + ", ".join(sorted(ALLOWED_CONSENTS)))
    if values.get("email"):
        raw_email = values["email"]
        values["email_raw"] = raw_email
        values["email_norm"] = normalize_email(raw_email)
        if not values["email_norm"]:
            warnings.append("Invalid email; excluded from identity matching")
    if values.get("phone"):
        raw_phone = values["phone"]
        values["phone_e164"] = normalize_phone(raw_phone, options.get("phone_region", "US"))
        if not values["phone_e164"]:
            warnings.append("Invalid phone; excluded from identity matching")
    if "enrichments" not in values:
        values["enrichments"] = enrichments
    else:
        values["enrichments"] = enrichments
    raw_json = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    values["raw_hash"] = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    values["raw_json"] = raw_json
    return {"values": values, "errors": errors, "warnings": warnings}