"""Disposable PostgreSQL only; not executed during implementation."""
from datetime import datetime, timezone
from sqlalchemy import text
from app.identity.bulk import resolve_bulk
from app.meta_attribution import capture, attach
from app.ingestion.service import anonymous_key
from test_consent_ingestion_pg import db
from test_master_identity_pg import source, record, pid


def test_app_user_ids_require_private_verified_provenance(db):
    sid = source(db)
    a = record(db, sid, "first", "first@example.org", meta_app_user_id="123:456")
    b = record(db, sid, "second", "second@example.org", meta_app_user_id="123:456")
    resolve_bulk(db)
    assert pid(db, a) != pid(db, b)
    assert db.scalar(text("SELECT count(*) FROM identity_xref WHERE key_type='meta_app_user_id'")) == 0
    db.execute(text("""INSERT INTO meta_verified_keys(source_record_id,meta_app_user_id)
      VALUES(:a,'123:456'),(:b,'123:456')"""), {"a": a, "b": b})
    db.execute(text("UPDATE source_records SET resolved_at=NULL WHERE id=ANY(:ids)"), {"ids": [a, b]})
    resolve_bulk(db)
    assert pid(db, a) == pid(db, b)
    assert db.scalar(text("""SELECT count(*) FROM identity_xref WHERE key_type='meta_app_user_id'
      AND rule='facebook_login_verified'""")) > 0


def test_page_scoped_key_without_provided_contact_does_not_resolve(db):
    sid = source(db)
    a, b = record(db, sid, "first"), record(db, sid, "second")
    db.execute(text("""INSERT INTO meta_verified_keys(source_record_id,meta_psid)
      VALUES(:a,'123:456'),(:b,'123:456')"""), {"a": a, "b": b})
    resolve_bulk(db)
    assert pid(db, a) != pid(db, b)
    assert db.scalar(text("SELECT count(*) FROM identity_xref WHERE key_type='meta_psid'")) == 0


def test_identified_attribution_is_event_only_and_idempotent(db):
    sid = source(db)
    session = "one-anonymous-session"
    rid = record(db, sid, "identify", "person@example.org", identify=True,
                 anonymous_id=anonymous_key(sid, session))
    resolve_bulk(db)
    before = db.scalar(text("SELECT count(*) FROM identifiers"))
    now = datetime.now(timezone.utc)
    capture(db, sid, {"anonymousId": session, "occurred_at": now,
                      "attribution": {"fbclid": "opaque-click", "_fbc": "fb.1.123.click"}})
    attach(db, sid)
    attach(db, sid)
    assert db.scalar(text("SELECT count(*) FROM identifiers")) == before
    assert db.scalar(text("SELECT count(*) FROM events WHERE name='Meta Attribution'")) == 1
    event = db.execute(text("SELECT * FROM events WHERE name='Meta Attribution'")).mappings().one()
    assert event["profile_id"] == pid(db, rid) and event["properties"]["fbclid"] == "opaque-click"
    assert db.scalar(text("SELECT count(*) FROM identity_xref WHERE key_type IN ('fbclid','_fbp','_fbc')")) == 0


def test_graph_profile_inventory_includes_attribution_sessions(db):
    from app.privacy.service import check_inventory
    assert any(row["table_name"] == "meta_attribution_sessions" for row in check_inventory(db))
