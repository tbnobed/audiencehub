"""Shared cache regressions against an explicitly disposable PostgreSQL schema."""
import hashlib
import json
import time
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from test_consent_ingestion_pg import db  # isolated, migrated schema fixture
from app.dashboards import cache
from app.dashboards.api import _dashboard
from app.dashboards.rollups import refresh_dashboard_rollups


def test_uninitialized_is_explicit_503(db):
    with pytest.raises(HTTPException) as exc:
        _dashboard("overview", date(2025, 1, 1), date(2025, 1, 31), db)
    assert exc.value.status_code == 503
    assert "traits.recompute" in exc.value.detail


def test_shared_cache_and_immediate_stale_while_refresh_scheduled(db, monkeypatch):
    refresh_dashboard_rollups(db)
    db.commit()
    start, end = date(2025, 1, 1), date(2025, 1, 31)
    original = _dashboard("overview", start, end, db)
    assert db.scalar(text("SELECT count(*) FROM dashboard_cache")) == 1
    db.rollback()
    # A distinct Session uses the durable payload, without calling its builder.
    from app.dashboards import api
    monkeypatch.setattr(api, "build_cache_key", lambda *a: pytest.fail("warm cache rebuilt"))
    with Session(db.get_bind()) as another_worker:
        assert _dashboard("overview", start, end, another_worker) == original
    pending = []
    monkeypatch.setattr(cache._executor, "submit", lambda *a: pending.append(a))
    refresh_dashboard_rollups(db)
    db.commit()
    started = time.perf_counter()
    assert _dashboard("overview", start, end, db) == original
    assert time.perf_counter()-started < .3
    assert len(pending) == 1
    assert pending[0][0] is cache._refresh


def test_background_refresh_replaces_old_generation(db):
    refresh_dashboard_rollups(db)
    db.commit()
    key = ("overview", date(2025, 1, 1), date(2025, 1, 31))
    _dashboard(*key, db)
    old = db.scalar(text("SELECT generation FROM dashboard_cache"))
    db.execute(text("INSERT INTO profiles DEFAULT VALUES"))
    refresh_dashboard_rollups(db)
    db.commit()
    version = cache.generation(db)
    assert old != version
    digest = hashlib.sha256(json.dumps(key, default=str).encode()).hexdigest()
    cache._refresh(digest, key, version, db.get_bind())
    db.rollback()
    result = _dashboard(*key, db)
    assert result["overview"]["stats"]["profiles"]["value"] == 1
    assert db.scalar(text("SELECT generation FROM dashboard_cache")) == version


def test_failed_background_refresh_preserves_stale_payload(db, monkeypatch):
    refresh_dashboard_rollups(db)
    db.commit()
    key = ("overview", date(2025, 1, 1), date(2025, 1, 31))
    original = _dashboard(*key, db)
    db.execute(text("UPDATE dashboard_cache SET computed_at=now()-interval '1 hour'"))
    db.commit()
    from app.dashboards import api
    def fail(*args):
        raise RuntimeError("deliberate refresh failure")
    monkeypatch.setattr(api, "build_cache_key", fail)
    digest = hashlib.sha256(json.dumps(key, default=str).encode()).hexdigest()
    cache._refresh(digest, key, cache.generation(db), db.get_bind())
    assert db.scalar(text("SELECT payload FROM dashboard_cache")) == original


def test_obsolete_generation_cannot_be_published_after_refresh(db):
    refresh_dashboard_rollups(db)
    db.commit()
    old_version = cache.generation(db)
    refresh_dashboard_rollups(db)
    db.commit()
    assert cache._publish(db, "late-old-builder", {"stale": True}, old_version) is False
    assert db.scalar(text("SELECT count(*) FROM dashboard_cache")) == 0


def test_cache_publication_does_not_wait_for_running_rollup_refresh(db):
    refresh_dashboard_rollups(db)
    db.commit()
    version = cache.generation(db)
    with Session(db.get_bind()) as refresh:
        refresh.execute(text("SELECT pg_advisory_xact_lock(hashtext('dashboard-rollups'))"))
        started = time.perf_counter()
        assert cache._publish(db, "blocked", {}, version) is False
        assert time.perf_counter()-started < .3