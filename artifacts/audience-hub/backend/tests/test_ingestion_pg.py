import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.db import session_scope
from app.ingestion.api import router
from app.ingestion.jobs import resolve
from app.ingestion.service import ingest
from app.ingestion.validation import validate
from app.models import Source
from test_consent_ingestion_pg import db
from test_privacy_pg import people, assert_all_tables_erased


def source(db, key="web"):
    result = Source(key=key, name="Ingestion test", kind="event_api", record_types=["event"],
                    write_key_hash=hashlib.sha256(f"wk_{key}".encode()).hexdigest())
    db.add(result); db.flush()
    return result


def client(db):
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[session_scope] = lambda: db
    return TestClient(app)


def test_api_auth_batch_limits_cors_and_idempotency(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ingest_cors_origins", "https://site.example")
    s = source(db); db.commit(); c = client(db)
    msg = {"anonymousId": "device", "event": "Viewed", "messageId": "stable"}
    assert c.post("/v1/track", json=msg).status_code == 401
    assert c.post("/v1/track", headers={"X-Write-Key": "bad"}, json=msg).status_code == 401
    headers = {"X-Write-Key": "wk_web"}
    assert c.post("/v1/track", headers=headers, json=msg).status_code == 202
    assert c.post("/v1/track", headers=headers, json={**msg, "timestamp": "2024-01-01T00:00:00Z"}).json()["accepted"] == 1
    assert db.scalar(text("SELECT count(*) FROM events")) == 1
    assert c.post("/v1/batch", headers=headers, json={"batch": [{"type": "track", **msg}, {}, {"type": "page", "userId": "u"}]}).json() == {
        "accepted": 2, "rejected": [{"index": 1, "reason": "Unsupported message type"}]}
    assert c.post("/v1/batch", headers=headers, json={"batch": [msg] * 501}).status_code == 400
    assert c.post("/v1/batch", headers=headers, content="x" * (1024 * 1024 + 1)).status_code == 413
    assert c.post("/v1/track", headers=headers, content="x" * (32 * 1024 + 1)).status_code == 413
    assert c.post("/v1/track", headers={**headers, "Origin": "https://evil.example"}, json=msg).status_code == 403
    allowed = c.options("/v1/batch", headers={"Origin": "https://site.example"})
    assert allowed.status_code == 204 and allowed.headers["Access-Control-Allow-Origin"] == "https://site.example"
    encoded = base64.b64encode(b"wk_web:").decode()
    assert c.post("/v1/track", headers={"Authorization": f"Basic {encoded}"}, json=msg).status_code == 202
    assert c.post("/v1/batch", json={"write_key": "wk_web", "batch": [{"type": "track", **msg}]}).status_code == 202
    assert db.scalar(text("SELECT count(*) FROM ingestion_receipts")) == 2
    assert "device" not in json.dumps(list(db.execute(text("SELECT payload FROM jobs")).scalars()))


def test_identify_resolver_backfill_and_source_isolation(db):
    s, other = source(db), source(db, "other")
    def send(s, message, kind):
        return ingest(db, s, [(0, validate(message, kind))])
    send(s, {"anonymousId": "device", "messageId": "before"}, "page")
    send(other, {"anonymousId": "device", "messageId": "other"}, "page")
    send(s, {"anonymousId": "device", "userId": "person", "messageId": "identify",
             "traits": {"email": "known@example.org", "firstName": "Known"}}, "identify")
    db.commit()
    resolve(db, s.id); db.commit()
    pid = db.scalar(text("SELECT id FROM profiles WHERE email='known@example.org'"))
    assert pid
    assert db.scalar(text("SELECT profile_id FROM events WHERE message_id='before'")) == pid
    assert db.scalar(text("SELECT profile_id FROM events WHERE message_id='other'")) is None
    send(s, {"userId": "person", "messageId": "after", "event": "Watch"}, "track")
    db.commit(); resolve(db, s.id); db.commit()
    assert db.scalar(text("SELECT profile_id FROM events WHERE message_id='after'")) == pid
    assert db.scalar(text("SELECT count(*) FROM trait_dirty_profiles WHERE profile_id=:id"), {"id": pid}) == 1


@pytest.mark.parametrize("resolved", [False, True])
def test_privacy_deletes_pending_and_attached_anon_history_and_blocks_retries(db, resolved):
    from app.privacy.service import create_deletion, approve_deletion, execute_deletion
    admins, _, _ = people(db)
    s = source(db)
    messages = [
        validate({"anonymousId": "erased-device", "messageId": "before"}, "page"),
        validate({"anonymousId": "erased-device", "userId": "erased-user", "messageId": "identify",
                  "traits": {"email": "ingestion-erase@example.org"}}, "identify"),
    ]
    ingest(db, s, list(enumerate(messages))); db.commit()
    if resolved:
        resolve(db, s.id); db.commit()
    ids = list(db.scalars(text("SELECT id FROM profiles WHERE email='ingestion-erase@example.org'")))
    deletion = create_deletion(db, "email", "ingestion-erase@example.org", admins[0].id)
    approve_deletion(db, deletion["id"], admins[1].id)
    execute_deletion(db, deletion["id"]); db.commit()
    assert_all_tables_erased(db, ids)
    assert not db.scalar(text("SELECT count(*) FROM events"))
    assert not db.scalar(text("SELECT count(*) FROM source_records WHERE source_id=:id"), {"id": s.id})
    for message in [
        validate({"anonymousId": "erased-device", "messageId": "new-page"}, "page"),
        validate({"userId": "erased-user", "messageId": "new-user"}, "page"),
        validate({"anonymousId": "new-device", "traits": {"email": "ingestion-erase@example.org"}}, "identify"),
    ]:
        result = ingest(db, s, [(0, message)])
        assert result["accepted"] == 0 and result["rejected"][0]["reason"] == "Identity is suppressed"
    assert "erased-device" not in json.dumps(list(db.execute(text("SELECT payload FROM jobs")).scalars()))
    assert db.scalar(text("SELECT count(*) FROM ingestion_receipts")) == 2


def test_concurrent_retry_receipt_is_atomic(db):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.orm import Session
    s = source(db); db.commit()
    source_id, bind = s.id, db.get_bind()
    def deliver(_):
        with Session(bind) as session:
            selected = session.get(Source, source_id)
            result = ingest(session, selected, [(0, validate({
                "anonymousId": "concurrent", "messageId": "same-across-workers"}, "page"))])
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(r["accepted"] == 1 for r in pool.map(deliver, range(8)))
    assert db.scalar(text("SELECT count(*) FROM events")) == 1
    assert db.scalar(text("SELECT count(*) FROM jobs WHERE type='ingestion.resolve'")) == 1


def test_write_key_rotation_tail_and_roles(db):
    from app.auth.deps import current_user
    from app.sources import router as sources_router
    admins, _, _ = people(db)
    s = source(db)
    app = FastAPI(); app.include_router(router); app.include_router(sources_router)
    app.dependency_overrides[session_scope] = lambda: db
    app.dependency_overrides[current_user] = lambda: admins[0]
    c = TestClient(app)
    url = f"/api/sources/{s.id}"
    rotated = c.post(url + "/write-key")
    assert rotated.status_code == 200 and rotated.headers["cache-control"] == "no-store"
    key = rotated.json()["write_key"]
    assert key.startswith("wk_live_")
    public = c.get(url).json()
    assert public["has_write_key"] and key not in json.dumps(public) and "write_key_hash" not in public
    assert db.scalar(text("SELECT write_key_hash FROM sources WHERE id=:id"), {"id": s.id}) == hashlib.sha256(key.encode()).hexdigest()
    assert c.post("/v1/page", headers={"X-Write-Key": "wk_web"}, json={"userId": "u"}).status_code == 401
    assert c.post("/v1/page", headers={"X-Write-Key": key}, json={"userId": "private-user", "properties": {"email": "private@example.org"}}).status_code == 202
    tail = c.get(url + "/events").json()
    assert len(tail["items"]) == 1 and "private-user" not in json.dumps(tail) and "private@example.org" not in json.dumps(tail)
    admins[0].role = "viewer"; db.flush()
    assert c.get(url + "/events").status_code == 403
    assert c.post(url + "/write-key").status_code == 403
    admins[0].role = "analyst"; db.flush()
    assert c.get(url + "/events").status_code == 200
    assert c.post(url + "/write-key").status_code == 403
