"""Server-only regression definitions. Not executed during implementation.

Use a disposable CONSENT_TEST_DATABASE_URL, never the production dataset.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.identity.addresses import parse_address, refresh_addresses, refresh_households
from app.identity.devices import DEVICE_KEY_TYPES, observe
from app.identity.matching import conflict, link, settings
from app.identity.master import KEY_TYPES, SYSTEM_KEYS, unmerge
from app.identity.bulk import resolve_bulk
from test_consent_ingestion_pg import db
from test_master_identity_pg import source, record, pid, master


def pair(db, **right_keys):
    sid = source(db)
    a = record(db, sid, "left", "first@example.org")
    b = record(db, sid, "right", "second@example.org", **right_keys)
    resolve_bulk(db)
    left, right = sorted((pid(db, a), pid(db, b)))
    db.execute(text("""UPDATE profiles SET first_name='Robert',last_name='Jones',
      address1='123 Main Street',city='Richmond',region='VA',postal_code='23219',country='US'
      WHERE id=ANY(:ids)"""), {"ids": [left, right]})
    db.execute(text("""UPDATE source_records SET first_name='Robert',last_name='Jones',
      address1='123 Main Street',city='Richmond',region='VA',postal_code='23219',country='US'
      WHERE profile_id=ANY(:ids)"""), {"ids": [left, right]})
    return sid, left, right


def test_address_equivalence_units_zip4_and_incomplete_are_conservative():
    base = dict(city="Richmond", region="Virginia", postal_code="23219-1234", country="US")
    a = parse_address(dict(base, address1="123 Main Street", address2="Apt 4"))
    b = parse_address(dict(base, address1="123 MAIN ST", address2="UNIT 4", region="VA"))
    assert a["normalized_key"] == b["normalized_key"] and a["normalized_key"]
    assert a["number"] == "123" and a["zip4"] == "1234"
    c = parse_address(dict(base, address1="123 Main Street", address2="Apt 5"))
    assert c["normalized_key"] != a["normalized_key"]
    assert parse_address(dict(base, address1="", address2=""))["normalized_key"] is None
    assert parse_address(dict(base, address1="PO Box 123", address2=""))["normalized_key"] is None


def test_households_never_merge_people_and_surname_rule_is_configurable(db):
    _, left, right = pair(db)
    original = [master(db, p) for p in (left, right)]
    refresh_addresses(db)
    refresh_households(db)
    assert db.scalar(text("SELECT count(DISTINCT household_id) FROM active_profiles")) == 1
    assert db.scalars(text("SELECT household_size FROM active_profiles")).all() == [2, 2]
    db.execute(text("UPDATE profiles SET last_name='Smith' WHERE id=:id"), {"id": right})
    refresh_households(db)
    assert db.scalar(text("SELECT count(DISTINCT household_id) FROM active_profiles")) == 2
    db.execute(text("UPDATE identity_graph_policy SET household_surname=false"))
    refresh_households(db)
    assert db.scalar(text("SELECT count(DISTINCT household_id) FROM active_profiles")) == 1
    assert [master(db, p) for p in (left, right)] == original
    assert db.scalar(text("SELECT count(*) FROM profile_merges")) == 0


def test_all_system_keys_block_probabilistic_merges(db):
    _, left, right = pair(db)
    lm, rm = master(db, left), master(db, right)
    for kind in SYSTEM_KEYS:
        db.execute(text("""INSERT INTO identity_xref(master_id,key_type,key_value)
          VALUES(:l,:k,'one'),(:r,:k,'two')"""), {"l": lm, "r": rm, "k": kind})
        assert conflict(db, left, right)
        with pytest.raises(ValueError, match="Conflicting system keys"):
            link(db, left, right, .9999, "test_model")
        db.execute(text("DELETE FROM identity_xref WHERE key_type=:k"), {"k": kind})
    assert db.scalar(text("SELECT count(*) FROM profile_merges")) == 0


def test_probabilistic_evidence_and_unmerge_preserve_original_edges(db):
    _, left, right = pair(db)
    before = db.execute(text("""SELECT key_type,key_value,method,confidence,rule
      FROM identity_xref ORDER BY key_type,key_value""")).all()
    merge_id = link(db, left, right, .9999, "test_model")
    assert db.scalar(text("SELECT count(*) FROM active_profiles")) == 1
    assert db.scalar(text("""SELECT count(*) FROM identity_xref
      WHERE method='probabilistic' AND rule='test_model' AND confidence=.9999""")) > 0
    result = unmerge(db, merge_id, None)
    assert result["new_master_id"] != result["retained_master_id"]
    assert db.scalar(text("SELECT count(*) FROM active_profiles")) == 2
    after = db.execute(text("""SELECT key_type,key_value,method,confidence,rule
      FROM identity_xref ORDER BY key_type,key_value""")).all()
    assert after == before


def test_devices_require_authenticated_unique_okta_and_do_not_merge(db):
    sid, left, right = pair(db, okta_customer_id="okta-right")
    db.execute(text("""INSERT INTO identity_xref(master_id,key_type,key_value)
      SELECT master_id,'okta_customer_id','okta-left' FROM profiles WHERE id=:p"""), {"p": left})
    now = datetime.now(timezone.utc)
    observation = dict(occurred_at=now, properties={"okta_customer_id": "okta-left", "web_cookie_id": "cookie-one"})
    assert observe(db, sid, observation) == 0
    assert observe(db, sid, observation, trusted_authenticated=True) == 1
    other = dict(occurred_at=now, properties={"okta_customer_id": "okta-right", "web_cookie_id": "cookie-one"})
    assert observe(db, sid, other, trusted_authenticated=True) == 1
    assert db.scalar(text("SELECT count(*) FROM identity_devices")) == 2
    assert db.scalar(text("SELECT count(*) FROM active_profiles")) == 2
    assert db.scalar(text("SELECT count(*) FROM profile_merges")) == 0
    assert not set(DEVICE_KEY_TYPES) & set(KEY_TYPES)
    stale = dict(observation, occurred_at=now - timedelta(days=100))
    assert observe(db, sid, stale, trusted_authenticated=True) == 0
    assert db.scalar(text("SELECT count(*) FROM identity_xref WHERE key_type='web_cookie_id'")) == 0


def test_household_fields_compile_using_registry_only(db):
    from app.models import Profile
    from app.segments.registry import PROFILE_FIELDS
    assert PROFILE_FIELDS["household_id"] == "text"
    assert PROFILE_FIELDS["household_size"] == "number"
    assert Profile.household_id is not None and Profile.household_size is not None


def test_splink_model_uses_all_comparisons_and_three_blocks():
    model = settings()
    assert len(model["blocking_rules_to_generate_predictions"]) == 3
    assert {x["output_column_name"] for x in model["comparisons"]} == {
        "first_name", "last_name", "address", "dob", "phone"}
    assert "nickname_l=nickname_r" in json.dumps(model)


def test_vendor_results_are_bound_to_current_address_input(db):
    from app.identity.addresses import vendor_result
    _, left, _ = pair(db)
    refresh_addresses(db)
    fingerprint = db.scalar(text("SELECT input_hash FROM identity_addresses WHERE profile_id=:id"), {"id": left})
    payload = dict(number="123", street="MAIN ST", unit="", city="RICHMOND", state="VA", zip5="23219", zip4="1234")
    assert vendor_result(db, left, "stale", "test_cass", payload) == 0
    assert vendor_result(db, left, fingerprint, "test_cass", payload) == 1
    assert db.scalar(text("SELECT parser FROM identity_addresses WHERE profile_id=:id"), {"id": left}) == "vendor"


def test_privacy_inventory_includes_every_graph_profile_reference(db):
    from app.privacy.service import check_inventory
    inventory = check_inventory(db)
    assert {"identity_devices", "identity_addresses", "identity_pairs"}.issubset(
        {r["table_name"] for r in inventory})
