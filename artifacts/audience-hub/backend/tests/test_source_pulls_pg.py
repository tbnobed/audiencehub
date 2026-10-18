"""Opt-in isolated-schema COPY integration tests; no remote services."""
import json
from datetime import date
from types import SimpleNamespace
from sqlalchemy import text

from test_consent_ingestion_pg import db
from app import source_pulls as pulls
from app.models import Source
from app.identity.bulk import resolve_bulk
from app.identity.attachments import attach_transactions
from app.traits.engine import recompute_traits


def setup_source(db, monkeypatch):
    source = Source(key="tbn", name="TBN test", kind="snowflake", record_types=["contact", "event", "resource"])
    db.add(source)
    db.flush()
    config = {"mappings": [{"id": "people", "feed": "people", "table": "PEOPLE", "cursor_column": "updated_at"}]}
    db.execute(text("INSERT INTO source_connections(source_id,credentials,config) VALUES(:s,:c,CAST(:cfg AS jsonb))"),
               {"s": source.id, "c": b"mock-not-a-real-key", "cfg": json.dumps(config)})
    db.commit()
    monkeypatch.setattr(pulls, "engine", db.get_bind())
    return source.id


def test_copy_donations_returns_replay_and_identity_only_contacts(db, monkeypatch):
    sid = setup_source(db, monkeypatch)
    resource = pulls.spool(iter([{"key": "S1", "name": "Appeal"}]), "resources", "tbn", "source_codes")
    contact = pulls.spool(iter([{"id": "cdp-1", "email": "donor@example.org"}]), "people", "tbn")
    events = pulls.spool(iter([
        {"id": "d1", "user_id": "cdp-1", "event": "donation", "timestamp": "2026-01-02T00:00:00Z",
         "amount": 100, "fund": "Mission", "source_code": "S1"},
        {"id": "r1", "user_id": "cdp-1", "event": "donation_return", "timestamp": "2026-01-03T00:00:00Z",
         "amount": 25, "fund": "Mission", "source_code": "S1"}]), "events", "tbn")
    manifests = [resource, contact, *events.pop("children"), events]
    monkeypatch.setattr(pulls, "collect", lambda *args: (manifests, {"people": "2026-01-03"}))
    pulls.run_source(sid)
    assert db.scalar(text("SELECT count(*) FROM profiles")) == 0
    resolve_bulk(db)
    attach_transactions(db)
    db.commit()
    recompute_traits(db, as_of=date(2026, 1, 4))
    db.commit()
    assert db.scalar(text("SELECT count(*) FROM profiles")) == 1
    assert db.scalar(text("SELECT count(*) FROM gifts WHERE profile_id IS NOT NULL")) == 2
    assert db.scalar(text("SELECT sum(amount) FROM gifts")) == 75
    trait = db.execute(text("SELECT gift_count_total,ltv_total,last_gift_date FROM profile_traits")).one()
    assert tuple(trait) == (1, 75, date(2026, 1, 2))
    pulls.run_source(sid)
    assert db.scalar(text("SELECT count(*) FROM gifts")) == 2
    assert db.scalar(text("SELECT count(*) FROM events")) == 2
    assert db.scalar(text("SELECT count(*) FROM source_import_receipts WHERE NOT completed")) == 0


def test_partial_import_recovery_keeps_cursor_and_same_receipts(db, monkeypatch):
    sid = setup_source(db, monkeypatch)
    first = pulls.spool(iter([{"id": "cdp-1", "email": "first@example.org"}]), "people", "tbn")
    second = pulls.spool(iter([{"id": "cdp-2", "email": "second@example.org"}]), "people", "tbn")
    monkeypatch.setattr(pulls, "collect", lambda *args: ([first, second], {"people": 12}))
    real_import = pulls.run_import
    calls = []
    def interrupt(import_id, **kwargs):
        calls.append(import_id)
        result = real_import(import_id, **kwargs)
        if len(calls) == 2:
            raise RuntimeError("Simulated worker loss after COPY committed")
        return result
    monkeypatch.setattr(pulls, "run_import", interrupt)
    import pytest
    with pytest.raises(RuntimeError):
        pulls.run_source(sid)
    state = db.scalar(text("SELECT state FROM source_connections WHERE source_id=:s"), {"s": sid})
    assert state.get("cursors", {}) == {} and "pending" in state
    assert db.scalar(text("SELECT count(*) FROM source_import_receipts")) == 2
    assert db.scalar(text("SELECT count(*) FROM profiles")) == 0
    monkeypatch.setattr(pulls, "run_import", real_import)
    monkeypatch.setattr(pulls, "collect", lambda *args: pytest.fail("Recovery must use the immutable pending spools"))
    pulls.run_source(sid)
    db.expire_all()
    state = db.scalar(text("SELECT state FROM source_connections WHERE source_id=:s"), {"s": sid})
    assert state["cursors"] == {"people": 12} and "pending" not in state
    assert db.scalar(text("SELECT count(*) FROM imports")) == 2
    assert db.scalar(text("SELECT count(*) FROM source_records")) == 2
    assert db.scalar(text("SELECT count(*) FROM jobs WHERE type='identity.resolve_batch'")) == 1


def test_pull_history_persists_safe_failure_and_source_isolation(db, monkeypatch):
    from app.models import Job
    from app.source_api import pull_history
    sid = setup_source(db, monkeypatch)
    for i in range(55):
        db.add(Job(type="source.pull", status="failed" if i == 54 else "succeeded",
                   payload={"source_id": sid, "feed": "people", "private_key": "never-return"},
                   error="SELECT secret FROM people WHERE email='never-return'" if i == 54 else None,
                   attempts=3 if i == 54 else 1))
    db.add(Job(type="source.pull", status="failed", payload={"source_id": sid + 1, "feed": "events"}))
    db.add(Job(type="noop", status="succeeded", payload={"source_id": sid}))
    db.commit()
    result = pull_history(sid, 50, SimpleNamespace(role="admin"), db)
    assert len(result["items"]) == 50
    assert result["items"][0]["status"] == "failed"
    assert result["items"][0]["attempts"] == 3
    assert all(item["feed"] == "people" for item in result["items"])
    assert result["items"][0]["counts"] == {
        "imports_total": 0, "imports_completed": 0, "rows_total": 0, "rows_ok": 0, "rows_rejected": 0,
    }
    encoded = json.dumps(result)
    assert "never-return" not in encoded and "SELECT" not in encoded
