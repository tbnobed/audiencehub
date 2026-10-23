"""Production-side disposable-Postgres checks. Not run during implementation."""
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.identity.bulk import resolve_bulk
from app.identity.devices import observe, rehome_devices
from app.identity.fusionauth import import_mapping
from app.identity.master import lookup_bulk, SYSTEM_KEYS
from app.ingestion.service import login_profile
from app.segments.compiler import compile_definition
from app.segments.templates import template_definitions
from test_consent_ingestion_pg import db
from test_master_identity_pg import source, record, pid, master


def mapping(tmp_path, content, name="mapping.csv"):
    path = tmp_path / name
    path.write_text("okta_customer_id,fusionauth_user_id\n" + content)
    return path


def test_existing_person_mapping_is_idempotent_and_preserves_all_audiences(db, tmp_path):
    sid = source(db)
    rid = record(db, sid, "legacy", okta_customer_id="old-1", tbn_plus_subscription="basic")
    resolve_bulk(db)
    from app.traits.engine import recompute_traits
    recompute_traits(db)
    person, original_master = pid(db, rid), master(db, pid(db, rid))
    before = {t["key"]: set(db.scalars(compile_definition(db, t["definition"])))
              for t in template_definitions()}
    total = db.scalar(text("SELECT count(*) FROM profiles"))
    path = mapping(tmp_path, "old-1,new-1\nmissing,new-2\n")
    result = import_mapping(db, path, tmp_path / "report.csv")
    assert result == {"linked": 1, "unmatched": 1}
    assert "unmatched" in (tmp_path / "report.csv").read_text()
    assert (tmp_path / "report.csv").stat().st_mode & 0o777 == 0o600
    assert db.scalar(text("SELECT count(*) FROM profiles")) == total
    assert lookup_bulk(db, [{"key_type": "fusionauth_user_id", "value": "new-1"}])[0]["master_id"] == original_master
    assert master(db, person) == original_master
    assert import_mapping(db, path, tmp_path / "again.csv") == {"already_linked": 1, "unmatched": 1}
    after = {t["key"]: set(db.scalars(compile_definition(db, t["definition"])))
             for t in template_definitions()}
    assert after == before
    assert db.scalar(text("SELECT count(*) FROM profile_merges")) == 0


def test_whole_file_conflicts_are_reported_without_first_row_winning(db, tmp_path):
    sid = source(db)
    record(db, sid, "one", okta_customer_id="old-1")
    record(db, sid, "two", okta_customer_id="old-2")
    resolve_bulk(db)
    path = mapping(tmp_path, "old-1,shared\nold-2,shared\n")
    assert import_mapping(db, path, tmp_path / "conflicts.csv") == {"conflict": 2}
    assert db.scalar(text("SELECT count(*) FROM identity_xref WHERE key_type='fusionauth_user_id'")) == 0


def test_suppressed_and_invalid_mapping_rows_never_attach(db, tmp_path):
    from app.keyring import pii_hashes
    sid = source(db)
    record(db, sid, "one", okta_customer_id="old-1")
    resolve_bulk(db)
    db.execute(text("""INSERT INTO suppressions(type,value_hash,reason)
      VALUES('fusionauth_user_id',:hash,'test')"""), {"hash": pii_hashes("new-1")[0]})
    path = mapping(tmp_path, "old-1,new-1\nold-1,\n")
    assert import_mapping(db, path, tmp_path / "blocked.csv") == {"suppressed": 1, "invalid": 1}
    assert db.scalar(text("SELECT count(*) FROM identity_xref WHERE key_type='fusionauth_user_id'")) == 0


def test_fusionauth_resolves_contacts_and_links_devices_and_viewing_without_okta(db):
    assert "fusionauth_user_id" in SYSTEM_KEYS
    sid = source(db)
    a = record(db, sid, "first", fusionauth_user_id="fusion-1")
    b = record(db, sid, "second", fusionauth_user_id="fusion-1")
    resolve_bulk(db)
    assert pid(db, a) == pid(db, b)
    person = pid(db, a)
    event = {"occurred_at": datetime.now(timezone.utc),
             "properties": {"fusionauth_user_id": "fusion-1", "app_install_id": "install-1"}}
    assert observe(db, sid, event) == 0  # Public SDK is NOT authenticated device evidence.
    assert observe(db, sid, event, trusted_authenticated=True) == 1
    assert login_profile(db, {"fusionauth_user_id": "fusion-1"}) == person
    assert login_profile(db, {"fusionauth_user_id": "fusion-1", "okta_customer_id": "unknown"}) is None
    rehome_devices(db)
    assert db.scalar(text("SELECT profile_id FROM identity_devices")) == person
    definition = {"version": 1, "root": {"op": "and", "rules": [
        {"type": "attribute", "field": "okta_customer_id", "operator": "exists"}]}}
    assert set(db.scalars(compile_definition(db, definition))) == {person}


def test_database_rejects_one_fusionauth_value_on_two_people(db):
    sid = source(db)
    a, b = record(db, sid, "a"), record(db, sid, "b")
    resolve_bulk(db)
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text("""INSERT INTO identity_xref(master_id,key_type,key_value)
          VALUES(:a,'fusionauth_user_id','collision'),(:b,'fusionauth_user_id','collision')"""),
                   {"a": master(db, pid(db, a)), "b": master(db, pid(db, b))})
        db.execute(text("SET CONSTRAINTS fusionauth_one_person IMMEDIATE"))


def test_viewing_backfill_attaches_after_mapping_without_new_people(db, tmp_path):
    from app.ingestion.service import backfill
    sid = source(db)
    record(db, sid, "legacy", okta_customer_id="old-1")
    resolve_bulk(db)
    db.execute(text("""INSERT INTO events(source_id,type,name,properties,occurred_at,message_id)
      VALUES(:s,'track','vod_viewed',CAST(:props AS jsonb),now(),'view-fusion')"""),
               {"s": sid, "props": json.dumps({"fusionauth_user_id": "new-1"})})
    path = mapping(tmp_path, "old-1,new-1\n")
    import_mapping(db, path, tmp_path / "mapped.csv")
    before = db.scalar(text("SELECT count(*) FROM profiles"))
    backfill(db, sid)
    assert db.scalar(text("SELECT profile_id FROM events WHERE message_id='view-fusion'")) is not None
    assert db.scalar(text("SELECT count(*) FROM profiles")) == before
