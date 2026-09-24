import pytest

from app.config import Settings
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