"""Normalization functions for values used by deterministic identity matching."""

import re
from functools import lru_cache

import phonenumbers
from email_validator import EmailNotValidError, validate_email
from email_validator.syntax import validate_email_domain_name

from app.config import Settings, get_settings

# Restrict the fast path to unquoted ASCII dot-atoms and ordinary ASCII DNS
# labels. In particular, punycode, Unicode, and unusual syntax must go through
# the full validator so its IDNA/SMTPUTF8 normalization remains authoritative.
_ORDINARY_EMAIL = re.compile(
    r"([A-Za-z0-9_!#$%&'*+/=?^`{|}~-]+(?:\.[A-Za-z0-9_!#$%&'*+/=?^`{|}~-]+)*)"
    r"@((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+"
    r"[A-Za-z](?:[A-Za-z0-9-]*[A-Za-z])?)",
    re.ASCII,
)


@lru_cache(maxsize=100_000)
def _validated_domain(domain: str, test_environment: bool) -> str | None:
    """Cache validator-approved domains, including negative results."""
    try:
        return validate_email_domain_name(
            domain, test_environment=test_environment
        )["domain"]
    except EmailNotValidError:
        return None


def validated_email(value: str) -> str | None:
    """Validate once and return email-validator's normalized address.

    This shared helper does not apply provider-specific identity rules; callers
    may use lower() or casefold() as their existing semantics require.
    """
    raw = value.strip()
    if not raw:
        return None
    test_environment = raw.lower().endswith("@gmail.test")
    ordinary = _ORDINARY_EMAIL.fullmatch(raw)
    if ordinary is not None:
        local, domain = ordinary.groups()
        # Length checks apply to the original address, before stripping tags.
        # Leave punycode and R-LDH labels to the full IDNA validator.
        if (len(local) <= 64 and len(raw) <= 254
                and len(domain) <= 253
                and not any(label.lower().startswith("xn--") or
                            (len(label) >= 4 and label[2:4] == "--")
                            for label in domain.split("."))):
            normalized_domain = _validated_domain(domain.lower(), test_environment)
            if normalized_domain is not None:
                return f"{local}@{normalized_domain}"
            return None
    try:
        return validate_email(
            raw, check_deliverability=False,
            test_environment=test_environment,
        ).normalized
    except EmailNotValidError:
        return None


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def normalize_email(value: object, settings: Settings | None = None) -> str | None:
    """Return a canonical matching email, or None when it is not valid."""
    if value is None:
        return None
    email = validated_email(str(value))
    if email is None:
        return None

    email = email.lower()
    local, domain = email.rsplit("@", 1)
    configured = _settings(settings).gmail_style_domains
    gmail_domains = {
        item.strip().lower()
        for item in (configured.split(",") if isinstance(configured, str) else configured)
        if item and item.strip()
    }
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain in gmail_domains or domain == "gmail.com":
        local = local.split("+", 1)[0].replace(".", "")
    else:
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def normalize_phone(value: object, settings: Settings | None = None) -> str | None:
    """Return a valid phone number as E.164; extensions are intentionally omitted."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    region = _settings(settings).default_phone_region
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_external_id(source_key: str, external_id: object) -> str | None:
    if external_id is None:
        return None
    value = str(external_id).strip()
    return f"{source_key}:{value}" if value else None


def normalize_anonymous_id(value: object) -> str | None:
    return _trimmed_identifier(value)


def normalize_user_id(source_key: str, value: object) -> str | None:
    normalized = _trimmed_identifier(value)
    return f"{source_key}:{normalized}" if normalized is not None else None


def _trimmed_identifier(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or len(normalized) > 200:
        return None
    return normalized


def normalize_name(value: object) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    if not normalized:
        return None
    if normalized.isupper() or normalized.islower():
        normalized = normalized.title()
    return normalized


def normalize_postal_code(value: object, country: str | None = None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    if not normalized:
        return None
    if country and country.strip().lower() in {"us", "usa", "united states", "united states of america"}:
        digits = "".join(character for character in normalized if character.isdigit())
        return digits[:5] or None
    return normalized