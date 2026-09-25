from types import SimpleNamespace
import json
import os
import time

import pytest
from fastapi import HTTPException

from app.dashboards import cache


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(cache.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(
        database_url="isolated-cache-test", pii_hash_pepper="test-only"))
    class DB:
        new = dirty = deleted = False
        assigned = False
        version = "10:10:"
        def invalidate(self):
            pass
        def execute(self, sql, params=None):
            return SimpleNamespace(one=lambda: ("0", "0"), scalar_one=lambda: (
                self.assigned if "if_assigned" in str(sql) else self.version))
    return DB(), tmp_path


def test_cache_version_copy_expiration_and_errors(store):
    db, directory = store
    calls = []
    def build():
        calls.append(1)
        return {"nested": {"value": len(calls)}}
    assert cache.cached(db, ("overview",), build)["nested"]["value"] == 1
    hit = cache.cached(db, ("overview",), build)
    hit["nested"]["value"] = -1
    assert cache.cached(db, ("overview",), build)["nested"]["value"] == 1
    db.version = "11:11:"
    assert cache.cached(db, ("overview",), build)["nested"]["value"] == 2
    for path in directory.rglob("*.json"):
        os.utime(path, (time.time()-120, time.time()-120))
    assert cache.cached(db, ("overview",), build)["nested"]["value"] == 3
    def fail():
        raise HTTPException(504, "deadline")
    with pytest.raises(HTTPException):
        cache.cached(db, ("failed",), fail)
    assert cache.cached(db, ("failed",), build)["nested"]["value"] == 4


def test_cache_bounded_and_uncommitted_bypass(store, monkeypatch):
    db, directory = store
    monkeypatch.setattr(cache, "MAX_ENTRIES", 2)
    for i in range(4):
        cache.cached(db, (i,), lambda: {"ok": True})
    assert len(list(directory.rglob("*.json"))) == 2
    db.new = True
    assert cache.cached(db, ("local-write",), lambda: {"private": True}) == {"private": True}
    db.new = False
    db.assigned = True  # includes flushed/raw SQL writes, even with clean ORM state
    assert cache.cached(db, ("flushed",), lambda: {"private": True}) == {"private": True}
    assert all("private" not in json.loads(p.read_text())["result"]
               for p in directory.rglob("*.json"))


def test_oversized_payload_is_not_published(store, monkeypatch):
    db, directory = store
    monkeypatch.setattr(cache, "MAX_BYTES", 64)
    result = {"large": "x" * 100}
    assert cache.cached(db, ("large",), lambda: result) == result
    assert not list(directory.rglob("*.json"))
    assert not list(directory.rglob("*.tmp"))


def test_deadline_is_explicit(store, monkeypatch):
    db, _ = store
    monkeypatch.setattr(cache, "DEADLINE_SECONDS", -1)
    with pytest.raises(HTTPException) as error:
        with cache.deadline(db):
            pass
    assert error.value.status_code == 504


def test_other_process_reuses_payload_and_deduplicates_computation(store):
    import multiprocessing
    db, _ = store
    context = multiprocessing.get_context("fork")
    entered = context.Event()
    output = context.Queue()
    def worker(slow=False):
        def build():
            entered.set()
            if slow:
                time.sleep(2.5)
            return {"built_by": os.getpid()}
        try:
            output.put(cache.cached(db, ("shared",), build))
        except HTTPException as exc:
            output.put({"status": exc.status_code})
    first = context.Process(target=worker, args=(True,))
    first.start()
    assert entered.wait(5)
    second = context.Process(target=worker)
    second.start()
    first.join(8)
    second.join(8)
    assert first.exitcode == second.exitcode == 0
    results = [output.get(timeout=2), output.get(timeout=2)]
    assert results == [{"built_by": first.pid}] * 2
    third = context.Process(target=worker)
    third.start()
    third.join(5)
    assert third.exitcode == 0
    assert output.get(timeout=2) == {"built_by": first.pid}


def test_different_keys_do_not_wait_and_timeout_is_explicit(store, monkeypatch):
    import multiprocessing
    db, _ = store
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()
    def owner():
        def build():
            entered.set()
            assert release.wait(5)
            return {"ok": True}
        cache.cached(db, ("overview", "2025-01"), build)
    child = context.Process(target=owner)
    child.start()
    try:
        assert entered.wait(3)
        monkeypatch.setattr(cache, "DEADLINE_SECONDS", .15)
        for key in [("overview", "2025-02"), ("giving", "2025-01")]:
            assert cache.cached(db, key, lambda: {"other": True}) == {"other": True}
        with pytest.raises(HTTPException) as error:
            cache.cached(db, ("overview", "2025-01"), lambda: pytest.fail("duplicate"))
        assert error.value.status_code == 504
        assert "deadline" in error.value.detail
    finally:
        release.set()
        child.join(5)
    assert child.exitcode == 0


def test_failed_owner_releases_lock_without_publishing(store):
    import multiprocessing
    db, directory = store
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()
    output = context.Queue()
    def owner():
        def fail():
            entered.set()
            assert release.wait(5)
            raise ValueError("failed computation")
        try:
            cache.cached(db, ("failure",), fail)
        except ValueError:
            output.put("failed")
    child = context.Process(target=owner)
    child.start()
    assert entered.wait(3)
    release.set()
    child.join(5)
    assert child.exitcode == 0
    assert output.get(timeout=2) == "failed"
    assert not list(directory.rglob("*.json"))
    assert cache.cached(db, ("failure",), lambda: {"recovered": True}) == {"recovered": True}


def test_coalesces_during_real_commits_and_later_traits_invalidate(store):
    """Real PG writers change snapshots while independent API processes join."""
    if os.getenv("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        pytest.skip("Requires disposable PostgreSQL benchmark harness")
    import multiprocessing
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    url = os.environ["DATABASE_URL"]
    assert "benchmark@/benchmark?host=/tmp/kinship-bench-" in url
    context = multiprocessing.get_context("fork")
    entered, stop, changed = context.Event(), context.Event(), context.Event()
    outputs, builds = context.Queue(), context.Queue()
    engine = create_engine(url)
    with engine.begin() as conn:
        pid = conn.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
    def worker(slow=False):
        local = create_engine(url)
        with Session(local) as db:
            def build():
                builds.put(os.getpid())
                value = db.execute(text("SELECT count(*) FROM profile_traits WHERE profile_id=:id"),
                                   {"id": pid}).scalar_one()
                entered.set()
                if slow:
                    time.sleep(3)
                return {"built_by": os.getpid(), "traits": value}
            outputs.put(cache.cached(db, ("real-commits",), build))
        local.dispose()
    def writer():
        local = create_engine(url)
        while not stop.is_set():
            with local.begin() as conn:
                conn.execute(text("UPDATE profiles SET last_seen_at=now() WHERE id=:id"), {"id": pid})
            changed.set()
            stop.wait(.03)
        local.dispose()
    children = []
    background = context.Process(target=writer)
    try:
        first = context.Process(target=worker, args=(True,))
        children.append(first)
        first.start()
        assert entered.wait(5)
        background.start()
        assert changed.wait(5)
        for _ in range(3):
            child = context.Process(target=worker)
            children.append(child)
            child.start()
        for child in children:
            child.join(8)
            assert child.exitcode == 0
        results = [outputs.get(timeout=2) for _ in children]
        assert results == [{"built_by": first.pid, "traits": 0}] * 4
        assert builds.get(timeout=2) == first.pid
        from queue import Empty
        with pytest.raises(Empty):
            builds.get(timeout=.1)
        stop.set()
        background.join(5)
        assert background.exitcode == 0
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO profile_traits(profile_id) VALUES (:id)"), {"id": pid})
        later = context.Process(target=worker)
        children.append(later)
        later.start()
        later.join(5)
        assert later.exitcode == 0
        assert outputs.get(timeout=2) == {"built_by": later.pid, "traits": 1}
        assert builds.get(timeout=2) == later.pid
    finally:
        stop.set()
        if background.pid is not None:
            background.join(5)
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM profile_traits WHERE profile_id=:id"), {"id": pid})
            conn.execute(text("DELETE FROM profiles WHERE id=:id"), {"id": pid})
        engine.dispose()


def test_shell_health_counts_does_not_wait_for_overview_process(tmp_path, monkeypatch):
    """The real /api/shell handler must not queue behind a cold Overview build."""
    if os.getenv("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        pytest.skip("Requires disposable PostgreSQL benchmark harness")
    import multiprocessing
    from datetime import date
    from unittest.mock import patch
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.dashboards import api

    url = os.environ["DATABASE_URL"]
    assert "benchmark@/benchmark?host=/tmp/kinship-bench-" in url
    monkeypatch.setattr(cache.tempfile, "gettempdir", lambda: str(tmp_path))
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()
    output = context.Queue()

    def overview_process():
        engine = create_engine(url)
        original = api._build_dashboard
        def slow_build(*args, **kwargs):
            # _dashboard has already acquired its logical-key computation lock.
            entered.set()
            assert release.wait(10)
            return original(*args, **kwargs)
        try:
            with Session(engine) as db, patch.object(api, "_build_dashboard", slow_build):
                result = api._dashboard("overview", date(2025, 1, 1), date(2025, 1, 31), db)
                output.put(result["range"]["from"])
        finally:
            engine.dispose()

    owner = context.Process(target=overview_process)
    engine = create_engine(url)
    owner.start()
    try:
        assert entered.wait(5)
        # Much shorter than the held Overview build; a shared lock would time out.
        monkeypatch.setattr(cache, "DEADLINE_SECONDS", .75)
        with Session(engine) as db:
            result = api.shell_summary(user=SimpleNamespace(role="viewer"), db=db)
        assert result == {
            "imports_running": None, "active_import": None,
            "open_issues": 0, "issue_counts": {"unresolved": 0, "review": 0},
        }
        assert owner.is_alive() and not release.is_set()
        # A completed health payload exists while Overview still owns its lock.
        assert len(list(tmp_path.rglob("*.lock"))) == 2
        assert len(list(tmp_path.rglob("*.json"))) == 1
    finally:
        release.set()
        owner.join(12)
        if owner.is_alive():
            owner.terminate()
            owner.join(5)
        engine.dispose()
    assert owner.exitcode == 0
    assert output.get(timeout=2) == "2025-01-01"