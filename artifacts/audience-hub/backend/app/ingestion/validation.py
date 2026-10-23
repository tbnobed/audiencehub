import ipaddress
import json
import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

MESSAGE_LIMIT = 32 * 1024
BATCH_LIMIT = 1024 * 1024


def depth(value):
    if isinstance(value, dict):
        return 1 + max((depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((depth(v) for v in value), default=0)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite numbers are not supported")
    return 0


def truncate_ip(value):
    try:
        address = ipaddress.ip_address(value)
        return str(ipaddress.ip_network(f"{address}/{24 if address.version == 4 else 48}", strict=False).network_address)
    except ValueError:
        return None


def validate(message, kind=None, received_at=None, ip=None):
    if not isinstance(message, dict):
        raise ValueError("Message must be an object")
    try:
        message_size = len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode())
    except UnicodeError:
        raise ValueError("Message contains invalid Unicode") from None
    if message_size > MESSAGE_LIMIT:
        raise ValueError("Message exceeds 32 KB")
    if depth(message) > 12:
        raise ValueError("Message is too deeply nested")
    kind = kind or message.get("type")
    if kind not in {"identify", "track", "page"}:
        raise ValueError("Unsupported message type")
    result = {"type": kind}
    for field in ("anonymousId", "userId", "messageId"):
        value = message.get(field)
        maximum = 200 if field == "userId" else 512
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > maximum):
            raise ValueError(f"{field} must be a nonempty string of at most {maximum} characters")
        result[field] = value.strip() if value else None
    if not (result["anonymousId"] or result["userId"]):
        raise ValueError("anonymousId or userId is required")
    result["messageId"] = result["messageId"] or str(uuid4())
    for field in ("properties", "traits", "context"):
        value = message.get(field, {})
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        if depth(value) > 5:
            raise ValueError(f"{field} depth exceeds 5")
        result[field] = dict(value)
    for field in ("email", "phone", "firstName", "lastName"):
        if field in result["traits"] and not isinstance(result["traits"][field], str):
            raise ValueError(f"traits.{field} must be a string")
    for container in ("traits", "properties"):
        for field in ("okta_customer_id", "fusionauth_user_id"):
            if field in result[container]:
                value = result[container][field]
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= 512:
                    raise ValueError(f"{container}.{field} must be 1–512 characters")
    from app.identity.normalize import normalize_email, normalize_phone
    for field, normalizer in (("email", normalize_email), ("phone", normalize_phone)):
        if result["traits"].get(field) and not normalizer(result["traits"][field]):
            raise ValueError(f"traits.{field} is invalid")
    name = message.get("event") if kind == "track" else message.get("name", "Page Viewed") if kind == "page" else "Identify"
    if not isinstance(name, str) or not name.strip() or len(name) > 512:
        raise ValueError("Event name must be a nonempty string of at most 512 characters")
    result["name"] = name.strip()
    now = received_at or datetime.now(timezone.utc)
    timestamp = message.get("timestamp")
    try:
        at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if isinstance(timestamp, str) else now
        if timestamp is not None and not isinstance(timestamp, str):
            raise ValueError()
        if at.tzinfo is None:
            raise ValueError()
    except (ValueError, OverflowError):
        raise ValueError("timestamp must be an ISO-8601 timestamp with timezone") from None
    if at > now + timedelta(days=7) or at < now - timedelta(days=730):
        at = now
        result["context"]["timestamp_clamped"] = True
    # Never trust forwarded headers or caller-supplied full IP addresses.
    result["context"].pop("ip", None)
    if ip and (truncated := truncate_ip(ip)):
        result["context"]["ip"] = truncated
    result["occurred_at"] = at
    result["received_at"] = now
    from app.meta_attribution import extract
    result["attribution"] = extract(message)
    return result
