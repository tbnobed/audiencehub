import random

import pytest
from email_validator import EmailNotValidError, validate_email

from app.config import Settings
from app.identity import normalize as normalization
from app.imports import validation as import_validation
from app.identity.normalize import (
    normalize_anonymous_id,
    normalize_email,
    normalize_external_id,
    normalize_name,
    normalize_phone,
    normalize_postal_code,
    normalize_user_id,
)


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql://localhost/audience_hub",
        secret_key="test",
        fernet_key="test",
        pii_hash_pepper="test",
        auth_mode="dev",
        gmail_style_domains="gmail.com,googlemail.com",
        default_phone_region="US",
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  User.Name+Tag@GMAIL.COM  ", "username@gmail.com"),
        (" First.Last+campaign@googlemail.com ", "firstlast@gmail.com"),
        (" Person.Name+tag@example.org ", "person.name@example.org"),
        ("person@example.org", "person@example.org"),
        ("not-an-email", None),
        ("missing-domain@", None),
        ("", None),
    ],
)
def test_email_normalization(raw, expected, settings):
    assert normalize_email(raw, settings) == expected


def test_configured_gmail_style_domains(settings):
    settings.gmail_style_domains = "gmail.com,googlemail.com,example.org"
    assert normalize_email("A.B+tag@example.org", settings) == "ab@example.org"


def test_reserved_seed_gmail_domain_is_valid_and_normalized(settings):
    settings.gmail_style_domains = "gmail.com,googlemail.com,gmail.test"
    assert normalize_email("  A.B+seed@GMAIL.TEST  ", settings) == "ab@gmail.test"
    assert normalize_email("a.b@other.test", settings) is None


def test_ordinary_email_skips_full_validator_and_reuses_domain_cache(monkeypatch):
    normalization._validated_domain.cache_clear()
    original = normalization.validate_email

    def unexpected_full_validation(*args, **kwargs):
        raise AssertionError("ordinary ASCII emails must not use validate_email")

    monkeypatch.setattr(normalization, "validate_email", unexpected_full_validation)
    assert normalization.validated_email("One.Tag@example.org") == "One.Tag@example.org"
    assert normalization.validated_email("Two.Tag@EXAMPLE.ORG") == "Two.Tag@example.org"
    assert normalization._validated_domain.cache_info().hits >= 1
    monkeypatch.setattr(normalization, "validate_email", original)
    assert normalization.validated_email("üser@example.org") == "üser@example.org"


def test_seeded_ten_thousand_address_fast_slow_equivalence(settings):
    """Never silently accept an address the full validator rejects."""
    rng = random.Random(94017)
    local_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-."
    valid_domains = (
        "example.org", "EXAMPLE.ORG", "gmail.com", "GoogleMail.com",
        "gmail.test", "sub.example.org", "mail-2.example.co.uk",
    )
    edge_domains = (
        "other.test", "example.invalid", "example.local", "localhost",
        "example.123", "example.1com", "-bad.example.org",
        "bad-.example.org", "a..example.org", "foo--bar.example.org",
        "xn--bcher-kva.example", "bücher.example", "example..org",
        "a" * 64 + ".org", "x." * 125 + "org",
    )
    edge_locals = (
        "a..b", ".a", "a.", "a" * 65, "a" * 64, "a+b",
        "with space", "üser", "a@b", '"quoted"', "user",
    )

    addresses = []
    for _ in range(10_000):
        local = "".join(rng.choices(local_chars, k=rng.randrange(1, 32)))
        domain = rng.choice(valid_domains if rng.randrange(5) else edge_domains)
        if rng.randrange(6) == 0:
            local = rng.choice(edge_locals)
        addresses.append(f"{local}@{domain}")
    # Guarantee each deterministic edge is checked, not merely sampled.
    addresses[:len(edge_domains)] = [f"user@{domain}" for domain in edge_domains]
    addresses[len(edge_domains):len(edge_domains) + len(edge_locals)] = [
        f"{local}@example.org" for local in edge_locals
    ]
    for raw in addresses:
        try:
            expected = validate_email(
                raw, check_deliverability=False,
                test_environment=raw.lower().endswith("@gmail.test"),
            ).normalized
        except EmailNotValidError:
            expected = None
        assert normalization.validated_email(raw) == expected, raw
        # The existing identity and CSV import callers differ only in their
        # lower/casefold choice and provider-specific canonicalization.
        assert (normalize_email(raw, settings) is None) == (expected is None), raw


def test_mapped_row_exposes_single_canonical_email_for_import(monkeypatch):
    calls = []
    original = import_validation.normalize_email

    def counting_normalize(raw):
        calls.append(raw)
        return original(raw)

    monkeypatch.setattr(import_validation, "normalize_email", counting_normalize)
    mapped = import_validation.map_and_validate_row(
        {"Email": " First.Last+newsletter@GMAIL.COM "},
        {"Email": "email"}, "contact",
    )
    assert calls == ["First.Last+newsletter@GMAIL.COM"]
    assert mapped["values"]["email_raw"] == calls[0]
    assert mapped["values"]["email_norm"] == "firstlast@gmail.com"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("(214) 748-3647", "+12147483647"),
        ("214.748.3647", "+12147483647"),
        ("+1 214 748 3647", "+12147483647"),
        ("2147483647 x12", "+12147483647"),
        ("123", None),
        ("(000) 000-0000", None),
        ("+44 20 7946 0958", "+442079460958"),
    ],
)
def test_phone_normalization(raw, expected, settings):
    assert normalize_phone(raw, settings) == expected


def test_identifier_names_postal_normalization():
    assert normalize_external_id("crm", "  contact-7 ") == "crm:contact-7"
    assert normalize_external_id("crm", "  ") is None
    assert normalize_anonymous_id("  anon-7  ") == "anon-7"
    assert normalize_anonymous_id("x" * 201) is None
    assert normalize_user_id("web", " visitor-7 ") == "web:visitor-7"
    assert normalize_user_id("web", "x" * 201) is None
    assert normalize_name("  JANE\tDOE  ") == "Jane Doe"
    assert normalize_name("mArY McDonald") == "mArY McDonald"
    assert normalize_name("  ") is None
    assert normalize_postal_code("12345-6789", "US") == "12345"
    assert normalize_postal_code("SW1A 1AA", "GB") == "SW1A 1AA"