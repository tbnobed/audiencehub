"""Focused Step 1 integration tests; requires disposable CONSENT_TEST_DATABASE_URL."""
import hashlib
import json
import re
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.identity.bulk import resolve_bulk
from app.identity.resolver import resolve_batch
from app.identity.master import lookup_bulk, unmerge, resolve_master, backfill_master_ids
from test_consent_ingestion_pg import db


def source(db):
    return db.scalar(text("""
      INSERT INTO sources(key,name,kind,record_types,priority)
      VALUES('master_test','Master test','csv',ARRAY['contact'],1) RETURNING id
    """))


def record(db, sid, ext, email=None, phone=None, **attrs):
    return db.scalar(text("""
      INSERT INTO source_records(source_id,external_id,email_norm,phone_e164,attributes,raw_hash)
      VALUES(:s,:e,:email,:phone,CAST(:a AS jsonb),'test') RETURNING id
    """), {"s": sid, "e": ext, "email": email, "phone": phone, "a": json.dumps(attrs)})


def pid(db, rid):
    return db.scalar(text("SELECT profile_id FROM source_records WHERE id=:r"), {"r": rid})


def master(db, p):
    return db.scalar(text("SELECT master_id FROM profiles WHERE id=:p"), {"p": p})


@pytest.mark.parametrize("resolver", [resolve_bulk, resolve_batch])
def test_household_and_hash_only_and_duplicate_hubspot(db, resolver):
    sid = source(db)
    a = record(db, sid, "a", "shared@example.org", donor_account_number="d1", hubspot_record_id="h1")
    b = record(db, sid, "b", "shared@example.org", donor_account_number="d2")
    assert resolver(db)["merges"] == 0
    assert pid(db, a) != pid(db, b)
    assert db.scalar(text("SELECT count(*) FROM identity_reviews")) == 2
    result = lookup_bulk(db, [{"key_type": "email", "value": "SHARED@example.org"}])[0]
    assert result["status"] == "ambiguous" and result["master_id"] is None
    c = record(db, sid, "c", email_md5=hashlib.md5(b"shared@example.org").hexdigest(),
               donor_account_number="d1", hubspot_record_id="h2")
    resolver(db)
    assert pid(db, c) == pid(db, a)
    assert db.scalar(text("SELECT count(*) FROM identity_xref WHERE status='duplicate_in_source'")) == 2
    # A hash-only record with no conflicting system evidence matches plain email.
    x = record(db, sid, "x", "hash@example.org")
    resolver(db)
    y = record(db, sid, "y", email_sha256=hashlib.sha256(b"hash@example.org").hexdigest())
    resolver(db)
    assert pid(db, x) == pid(db, y)


@pytest.mark.parametrize("resolver", [resolve_bulk, resolve_batch])
def test_merge_chain_and_reversal(db, resolver):
    sid = source(db)
    a = record(db, sid, "a", "old@example.org", donor_account_number="old")
    resolver(db)
    b = record(db, sid, "b", "b@example.org", "+12025550121")
    resolver(db)
    c = record(db, sid, "c", "c@example.org", "+12025550122")
    resolver(db)
    originals = [master(db, pid(db, r)) for r in (a, b, c)]
    record(db, sid, "bridge1", "old@example.org", "+12025550121")
    assert resolver(db)["merges"] == 1
    record(db, sid, "bridge2", "old@example.org", "+12025550122")
    assert resolver(db)["merges"] == 1
    survivor = originals[0]
    assert all(resolve_master(db, m) == survivor for m in originals)
    journals = db.execute(text("SELECT merge_id,sides FROM identity_merge_journal ORDER BY merge_id")).all()
    assert len(journals) == 2
    for _, sides in journals:
        assert all(k in sides["winner"] and k in sides["loser"]
                   for k in ("identifiers", "source_records", "events", "gifts", "profile_traits", "xref"))
    with pytest.raises(ValueError, match="later dependent"):
        unmerge(db, journals[0][0], None)
    result = unmerge(db, journals[1][0], None)
    assert result["retained_master_id"] == survivor
    assert result["new_master_id"] not in originals
    assert re.fullmatch(r"kin_[0-7][0-9A-HJKMNP-TV-Z]{25}", result["new_master_id"])
    assert resolve_master(db, originals[2]) == survivor  # retired never reused
    assert pid(db, c) != pid(db, a)


def test_immutable_and_lookup_10k(db):
    sid = source(db)
    a = record(db, sid, "a", " First.Last+tag@Example.org ", donor_account_number="001")
    resolve_bulk(db)
    m = master(db, pid(db, a))
    with pytest.raises(DBAPIError, match="immutable"):
        with db.begin_nested():
            db.execute(text("UPDATE profiles SET master_id=kin_new_master_id() WHERE master_id=:m"), {"m": m})
    assert db.scalar(text("SELECT email_norm::text FROM source_records WHERE id=:a"), {"a": a}) == "first.last+tag@example.org"
    start = time.monotonic()
    result = lookup_bulk(db, [{"key_type": "donor_account_number", "value": "001"}] * 10000)
    print("lookup_10k_seconds", time.monotonic() - start)
    assert len(result) == 10000 and all(r["master_id"] == m for r in result)
    assert backfill_master_ids(db) == 0


def test_system_bridge_quarantined(db):
    sid = source(db)
    a = record(db, sid, "a", donor_account_number="d1", okta_customer_id="o1")
    b = record(db, sid, "b", donor_account_number="d2", okta_customer_id="o2")
    resolve_bulk(db)
    bridge = record(db, sid, "bridge", donor_account_number="d1", okta_customer_id="o2")
    resolve_bulk(db)
    assert pid(db, a) != pid(db, b)
    assert pid(db, bridge) is None
    assert db.scalar(text("SELECT count(*) FROM profile_merges")) == 0
    assert db.scalar(text("SELECT count(*) FROM identity_reviews")) >= 1


def test_unmerge_oldest_system_side_and_mutation_guard(db):
    sid = source(db)
    a = record(db, sid, "a", "a@example.org")
    resolve_bulk(db)
    original = master(db, pid(db, a))
    b = record(db, sid, "b", "b@example.org", "+12025550123", donor_account_number="oldest-key")
    db.execute(text("UPDATE source_records SET created_at=now()-interval '2 years' WHERE id=:b"), {"b": b})
    resolve_bulk(db)
    record(db, sid, "bridge", "a@example.org", "+12025550123")
    resolve_bulk(db)
    mid = db.scalar(text("SELECT max(id) FROM profile_merges"))
    with db.begin_nested() as tx:
        db.execute(text("UPDATE source_records SET updated_at=clock_timestamp() WHERE id=:b"), {"b": b})
        with pytest.raises(ValueError, match="Source records changed"):
            unmerge(db, mid, None)
        tx.rollback()
    result = unmerge(db, mid, None)
    assert result["retained_side"] == "loser"
    assert master(db, pid(db, b)) == original
    assert master(db, pid(db, a)) == result["new_master_id"]
    assert result["new_profile_id"] == pid(db, a)
    assert result["retained_profile_id"] == pid(db, b)


def test_resumable_backfill_registry_prevents_reuse(db):
    # Simulate pre-migration rows in this disposable schema only.
    db.execute(text("ALTER TABLE profiles DISABLE TRIGGER kin_master_id"))
    db.execute(text("""
      INSERT INTO profiles(first_seen_at) VALUES(now()-interval '3 years'),(now()-interval '2 years')
    """))
    db.execute(text("ALTER TABLE profiles ENABLE TRIGGER kin_master_id"))
    assert backfill_master_ids(db, batch_size=1) == 1
    db.commit()
    first = db.scalar(text("SELECT master_id FROM profiles WHERE master_id IS NOT NULL"))
    assert backfill_master_ids(db, batch_size=1) == 1
    db.commit()
    assert backfill_master_ids(db, batch_size=1) == 0
    assert db.scalar(text("SELECT count(*) FROM profiles WHERE master_id=:m"), {"m": first}) == 1
    db.execute(text("DELETE FROM profiles WHERE master_id=:m"), {"m": first})
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(text("INSERT INTO profiles(master_id) VALUES(:m)"), {"m": first})


def test_privacy_removes_reversal_crosswalk_and_suppresses_hash_replay(db, tmp_path):
    from app.models import User
    from app.privacy.service import create_deletion, approve_deletion, execute_deletion
    from test_consent_ingestion_pg import ingest
    admins = [User(subject=f"identity-admin-{i}", email=f"admin{i}@example.org", name="Admin",
                   role="admin", is_active=True) for i in range(2)]
    db.add_all(admins)
    db.flush()
    sid = source(db)
    record(db, sid, "a", "erase@example.org")
    resolve_bulk(db)
    record(db, sid, "b", "other@example.org", "+12025550123", donor_account_number="erase-donor")
    resolve_bulk(db)
    record(db, sid, "bridge", "erase@example.org", "+12025550123")
    resolve_bulk(db)
    mid = db.scalar(text("SELECT max(id) FROM profile_merges"))
    request = create_deletion(db, "email", "erase@example.org", admins[0].id)
    approve_deletion(db, request["id"], admins[1].id)
    execute_deletion(db, request["id"])
    assert db.scalar(text("SELECT count(*) FROM identity_merge_journal")) == 0
    assert db.scalar(text("SELECT count(*) FROM identity_xref")) == 0
    assert db.scalar(text("SELECT count(*) FROM master_id_aliases")) == 0
    assert db.scalar(text("SELECT count(*) FROM master_id_registry")) >= 2
    with pytest.raises(LookupError):
        unmerge(db, mid, None)
    hashed = hashlib.sha256(b"other@example.org").hexdigest()
    imported_source, imported_rows = ingest(db, tmp_path, f"external_id,email_sha256\nreplay,{hashed}\n")
    assert not imported_rows
    assert db.scalar(text("SELECT rows_rejected FROM imports WHERE source_id=:s"),
                     {"s": imported_source.id}) == 1


def test_api_access_csrf_and_audit_privacy(db):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware
    from app.auth.deps import current_user
    from app.db import session_scope
    from app.identity.api import router
    from app.models import User
    sid = source(db)
    r = record(db, sid, "a", "private@example.org")
    resolve_bulk(db)
    m = master(db, pid(db, r))
    user = User(subject="api-identity-user", email="api@example.org", name="API", role="viewer", is_active=True)
    db.add(user)
    db.flush()
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="identity-test")
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[session_scope] = lambda: db

    @app.get("/session")
    def setup(request: Request):
        request.session["csrf"] = "identity-csrf"
        return {}

    client = TestClient(app)
    assert client.get("/api/identity/lookup?key_type=email&value=private@example.org").status_code == 403
    assert client.get(f"/api/identity/master/{m}").status_code == 403
    user.role = "analyst"
    assert client.get("/api/identity/lookup?key_type=email&value=private@example.org").status_code == 200
    payload = {"items": [{"key_type": "email", "value": "private@example.org"}]}
    assert client.post("/api/identity/lookup/bulk", json=payload).status_code == 403
    client.get("/session")
    headers = {"X-CSRF-Token": "identity-csrf"}
    assert client.post("/api/identity/lookup/bulk", json=payload, headers=headers).status_code == 200
    assert client.post("/api/identity/merges/999/unmerge", headers=headers).status_code == 403
    user.role = "admin"
    assert client.post("/api/identity/merges/999/unmerge", headers=headers).status_code == 404
    record(db, sid, "b", "another@example.org", "+12025550124")
    resolve_bulk(db)
    record(db, sid, "bridge", "private@example.org", "+12025550124")
    resolve_bulk(db)
    mid = db.scalar(text("SELECT max(id) FROM profile_merges"))
    details = client.get(f"/api/identity/master/{m}")
    assert details.status_code == 200, details.text
    assert details.json()["merge_history"][0]["reversible"]
    response = client.post(f"/api/identity/merges/{mid}/unmerge", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["new_profile_id"] != response.json()["retained_profile_id"]
    assert client.post("/api/identity/lookup/bulk", json={"items": payload["items"] * 10001},
                       headers=headers).status_code == 422
    logs = db.scalars(text("SELECT details::text FROM audit_log WHERE action='identity.lookup'")).all()
    assert len(logs) == 2 and all("private@example.org" not in row and m not in row for row in logs)


def test_journal_moves_gifts_events_and_rejects_changed_consent(db):
    sid = source(db)
    a = record(db, sid, "a", "first@example.org", donor_account_number="first")
    resolve_bulk(db)
    b = record(db, sid, "b", "second@example.org", "+12025550123")
    resolve_bulk(db)
    before_b = pid(db, b)
    gift = db.scalar(text("""
      INSERT INTO gifts(source_id,external_id,profile_id,source_record_id,amount,gift_date)
      VALUES(:s,'gift',:p,:r,50,current_date) RETURNING id
    """), {"s": sid, "p": before_b, "r": b})
    event = db.scalar(text("""
      INSERT INTO events(source_id,profile_id,type,name,occurred_at)
      VALUES(:s,:p,'track','vod_viewed',now()) RETURNING id
    """), {"s": sid, "p": before_b})
    db.execute(text("""
      INSERT INTO consents(profile_id,source_id,channel,status) VALUES(:p,:s,'email','opted_out')
    """), {"p": before_b, "s": sid})
    record(db, sid, "bridge", "first@example.org", "+12025550123")
    resolve_bulk(db)
    mid = db.scalar(text("SELECT max(id) FROM profile_merges"))
    assert db.scalar(text("SELECT profile_id FROM gifts WHERE id=:id"), {"id": gift}) == pid(db, a)
    with db.begin_nested() as tx:
        db.execute(text("UPDATE consents SET captured_at=clock_timestamp(),evidence=CAST(:e AS jsonb)"),
                   {"e": '{"manual":true}'})
        with pytest.raises(ValueError, match="Consent or enrichment changed"):
            unmerge(db, mid, None)
        tx.rollback()
    result = unmerge(db, mid, None)
    assert db.scalar(text("SELECT profile_id FROM gifts WHERE id=:id"), {"id": gift}) == result["new_profile_id"]
    assert db.scalar(text("SELECT profile_id FROM events WHERE id=:id"), {"id": event}) == result["new_profile_id"]
    assert db.scalar(text("SELECT count(*) FROM consents WHERE status='opted_out'")) == 2


def test_shared_contact_transaction_attachment_is_ambiguous(db):
    from app.identity.attachments import attach_transactions
    sid = source(db)
    record(db, sid, "a", "household@example.org", donor_account_number="d1")
    record(db, sid, "b", "household@example.org", donor_account_number="d2")
    resolve_bulk(db)
    imported = db.scalar(text("""
      INSERT INTO imports(source_id,filename,file_path,record_type)
      VALUES(:s,'gift.csv','/isolated/gift.csv','gift') RETURNING id
    """), {"s": sid})
    r = record(db, sid, "gift", "household@example.org")
    db.execute(text("UPDATE source_records SET last_import_id=:i WHERE id=:r"), {"i": imported, "r": r})
    db.execute(text("""
      INSERT INTO gifts(source_id,source_record_id,external_id,amount,gift_date)
      VALUES(:s,:r,'gift',10,current_date)
    """), {"s": sid, "r": r})
    attach_transactions(db)
    assert db.scalar(text("SELECT profile_id FROM gifts WHERE external_id='gift'")) is None


def test_merge_journal_atomic_rollback_and_import_lock(db, monkeypatch):
    from sqlalchemy.orm import Session
    from app.identity import master as module
    from app.identity.locking import acquire_import_lock, IdentityResolutionDeferred
    sid = source(db)
    a = record(db, sid, "a", "atomic@example.org")
    resolve_bulk(db)
    b = record(db, sid, "b", "other@example.org", "+12025550124")
    resolve_bulk(db)
    record(db, sid, "bridge", "atomic@example.org", "+12025550124")
    original = module.journal_merge

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected before move")

    with db.begin_nested() as tx:
        monkeypatch.setattr(module, "journal_merge", crash)
        with pytest.raises(RuntimeError, match="injected"):
            resolve_bulk(db)
        tx.rollback()
    monkeypatch.setattr(module, "journal_merge", original)
    assert pid(db, a) != pid(db, b)
    assert db.scalar(text("SELECT count(*) FROM identity_merge_journal")) == 0
    assert db.scalar(text("SELECT count(*) FROM master_id_aliases")) == 0
    resolve_bulk(db)
    mid = db.scalar(text("SELECT max(id) FROM profile_merges"))
    db.commit()
    with Session(db.get_bind()) as importing:
        acquire_import_lock(importing)
        with pytest.raises(IdentityResolutionDeferred):
            unmerge(db, mid, None)
        db.rollback()
        importing.rollback()
    assert unmerge(db, mid, None)["new_master_id"]


def test_alias_chain_really_traverses_multiple_retirements(db):
    sid = source(db)
    a = record(db, sid, "a", "a@example.org")
    resolve_bulk(db)
    b = record(db, sid, "b", "b@example.org", "+12025550123")
    resolve_bulk(db)
    c = record(db, sid, "c", "c@example.org", "+12025550124")
    resolve_bulk(db)
    ma, mb, mc = (master(db, pid(db, row)) for row in (a, b, c))
    record(db, sid, "bridge-bc", "b@example.org", "+12025550124")
    resolve_bulk(db)
    record(db, sid, "bridge-ab", "a@example.org", "+12025550123")
    resolve_bulk(db)
    assert db.scalar(text("SELECT survivor_id FROM master_id_aliases WHERE retired_id=:m"), {"m": mc}) == mb
    assert db.scalar(text("SELECT survivor_id FROM master_id_aliases WHERE retired_id=:m"), {"m": mb}) == ma
    assert resolve_master(db, mc) == ma
    assert lookup_bulk(db, [{"key_type": "email", "value": "c@example.org"}])[0]["master_id"] == ma


def test_real_application_registers_identity_routes_with_session_auth(db):
    """Exercise production routing/middleware, with only its DB replaced."""
    from base64 import b64encode
    from itsdangerous import TimestampSigner
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.db import session_scope
    from app.main import app
    from app.models import User
    sid = source(db)
    a = record(db, sid, "a", "real-route@example.org")
    resolve_bulk(db)
    original = master(db, pid(db, a))
    record(db, sid, "b", "real-other@example.org", "+12025550125")
    resolve_bulk(db)
    record(db, sid, "bridge", "real-route@example.org", "+12025550125")
    resolve_bulk(db)
    merge_id = db.scalar(text("SELECT max(id) FROM profile_merges"))
    admin = User(subject="real-session", email="real-admin@example.org", name="Admin",
                 role="admin", is_active=True)
    db.add(admin)
    db.commit()
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[session_scope] = lambda: db
    try:
        # No lifespan/context startup: never starts app jobs or touches app.db.engine.
        client = TestClient(app)
        assert client.get(f"/api/identity/master/{original}").status_code == 401
        session = {"uid": admin.id, "issued": int(time.time()), "touched": int(time.time()),
                   "csrf": "real-csrf"}
        cookie = TimestampSigner(get_settings().secret_key).sign(
            b64encode(json.dumps(session).encode())).decode()
        client.cookies.set("ah_session", cookie)
        single = client.get("/api/identity/lookup", params={"key_type": "email", "value": "real-route@example.org"})
        assert single.status_code == 200, single.text
        assert single.json()["master_id"] == original
        assert single.headers["cache-control"] == "no-store"
        assert client.get(f"/api/identity/master/{original}").status_code == 200
        assert client.get("/api/identity/health").status_code == 200
        headers = {"X-CSRF-Token": "real-csrf"}
        response = client.post("/api/identity/lookup/bulk", headers=headers,
                               json={"items": [{"key_type": "email", "value": "real-route@example.org"}]})
        assert response.status_code == 200, response.text
        response = client.post(f"/api/identity/merges/{merge_id}/unmerge", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["new_master_id"] != original
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
