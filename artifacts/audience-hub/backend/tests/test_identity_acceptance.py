from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "identity_acceptance.py"
SPEC = spec_from_file_location("identity_acceptance", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
identity_acceptance = module_from_spec(SPEC)
SPEC.loader.exec_module(identity_acceptance)


def test_split_patterns_use_raw_crm_and_normalized_identifier_evidence():
    junk_with_esp = identity_acceptance._split_pattern(
        "noemail@example.com",
        "+12125551234",
        None,
        None,
        {"esp-normalized@example.org"},
        True,
    )
    assert junk_with_esp == (
        "CRM raw email is junk; normalized CRM email absent; raw CRM phone present "
        "but normalized CRM phone absent; "
        "ESP has a distinct normalized email"
    )

    malformed_phone = identity_acceptance._split_pattern(
        "person@example.org",
        "(000) 000-0000",
        "person@example.org",
        None,
        {"alternate@example.org"},
        True,
    )
    assert malformed_phone == (
        "CRM raw phone is malformed; normalized CRM phone is absent; "
        "CRM raw email syntactically valid; normalized CRM email present; "
        "ESP has a distinct normalized email"
    )

    junk_without_esp = identity_acceptance._split_pattern(
        "noemail@example.com",
        "+12125551234",
        None,
        None,
        set(),
        False,
    )
    assert junk_without_esp == (
        "CRM raw email is junk; normalized CRM email absent; raw CRM phone present "
        "but normalized CRM phone absent; "
        "no matched active ESP record"
    )

    malformed_email = identity_acceptance._split_pattern(
        "not-an-email",
        "+12125551234",
        None,
        None,
        {"alternate@example.org"},
        True,
    )
    assert malformed_email == (
        "CRM raw email is malformed; normalized CRM email absent; raw CRM phone present "
        "but normalized CRM phone absent; "
        "ESP has a distinct normalized email"
    )

    assert len({
        junk_with_esp, malformed_phone, junk_without_esp, malformed_email,
    }) == 4


def test_identifier_masking_never_exposes_full_values():
    masked_email = identity_acceptance._mask_identifier("email", "donor@example.org")
    masked_phone = identity_acceptance._mask_identifier("phone", "+12125551234")

    assert masked_email == "email [masked]"
    assert "donor" not in masked_email
    assert masked_phone == "phone ending 1234"
    assert "+12125551234" not in masked_phone


def test_raw_identifier_classifiers_distinguish_seed_junk_and_malformed_values():
    assert identity_acceptance._raw_email_kind("noemail@example.com") == "junk"
    assert identity_acceptance._raw_email_kind("not-an-email") == "malformed"
    assert identity_acceptance._raw_email_kind("valid@example.org") == "syntactically valid"
    assert identity_acceptance._raw_email_kind(None) == "unavailable"
    assert identity_acceptance._raw_phone_is_malformed("(000) 000-0000")
    assert not identity_acceptance._raw_phone_is_malformed("+12125551234")