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
        version = "10:10:"
        def execute(self, sql, params=None):
            return SimpleNamespace(scalar_one=lambda: (
                False if "if_assigned" in str(sql) else self.version))
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
    assert all("private" not in json.loads(p.read_text()) for p in directory.rglob("*.json"))


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
    assert {"status": 503} in results
    assert {"built_by": first.pid} in results
    third = context.Process(target=worker)
    third.start()
    third.join(5)
    assert third.exitcode == 0
    assert output.get(timeout=2) == {"built_by": first.pid}