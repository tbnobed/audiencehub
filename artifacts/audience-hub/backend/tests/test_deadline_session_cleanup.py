"""Full ASGI dependency teardown against the disposable PostgreSQL harness."""
import os

import pytest
from fastapi import APIRouter, Depends, HTTPException
from fastapi.testclient import TestClient
from psycopg.errors import QueryCanceled
from sqlalchemy import create_engine, event, text

from app import db as database
from app.dashboards import cache


@pytest.fixture
def lifecycle(monkeypatch):
    if os.getenv("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        pytest.skip("Requires disposable PostgreSQL benchmark harness")
    url = os.environ["DATABASE_URL"]
    assert "benchmark@/benchmark?host=/tmp/kinship-bench-" in url
    from app.main import app

    engine = create_engine(url, pool_size=1, max_overflow=0)
    monkeypatch.setattr(database, "engine", engine)
    router = APIRouter()
    state = {"invalidated": 0}

    @event.listens_for(engine, "invalidate")
    def invalidated(*args):
        state["invalidated"] += 1

    @router.get("/__test_cleanup/healthy")
    def healthy(db=Depends(database.session_scope)):
        row = db.execute(text(
            "SELECT pg_backend_pid(), current_setting('statement_timeout'), "
            "current_setting('lock_timeout')"
        )).one()
        return {"pid": row[0], "statement": row[1], "lock": row[2]}

    @router.get("/__test_cleanup/rollback/{status}")
    def rollback(status: int, db=Depends(database.session_scope)):
        state["pid"] = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
        db.execute(text("SET LOCAL statement_timeout='1ms'"))
        db.execute(text("SET LOCAL lock_timeout='1ms'"))
        state["cancel_rollback"] = True
        if status == 200:
            return {"ok": True}
        if status == 504:
            # Match production: Python deadline check fails first; the driver's
            # rollback then raises QueryCanceled during dependency teardown.
            token = cache._deadline.set(0)
            try:
                cache.check_deadline(db)
            finally:
                cache._deadline.reset(token)
        raise HTTPException(status, "original failure")

    @router.get("/__test_cleanup/deadline/{kind}")
    def deadline(kind: str, db=Depends(database.session_scope)):
        state["pid"] = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
        with cache.deadline(db):
            if kind == "sql":
                db.execute(text("SELECT pg_sleep(2)"))
            else:
                token = cache._deadline.set(0)
                try:
                    cache.check_deadline(db)
                finally:
                    cache._deadline.reset(token)
        return {"unexpected": True}

    @router.get("/__test_cleanup/write")
    def write(db=Depends(database.session_scope)):
        db.execute(text("CREATE TEMP TABLE cleanup_write(value integer)"))
        db.execute(text("INSERT INTO cleanup_write VALUES (7)"))
        db.execute(text("SET LOCAL statement_timeout='5s'"))
        db.execute(text("SET LOCAL lock_timeout='3s'"))
        with cache.deadline(db):
            assert db.execute(text("SELECT value FROM cleanup_write")).scalar_one() == 7
        assert db.execute(text("SHOW statement_timeout")).scalar_one() == "5s"
        assert db.execute(text("SHOW lock_timeout")).scalar_one() == "3s"
        db.rollback()
        # A hidden commit inside deadline would have persisted this table.
        assert db.execute(text("SELECT to_regclass('pg_temp.cleanup_write')")).scalar_one() is None
        return {"ok": True}

    @router.get("/__test_cleanup/commit")
    def commit(db=Depends(database.session_scope)):
        db.execute(text("SELECT 1"))
        db.commit()
        return {"unexpected": True}

    original_rollback = engine.dialect.do_rollback

    def rollback_with_cancellation(connection):
        if state.pop("cancel_rollback", False):
            raise QueryCanceled("canceling statement due to statement timeout")
        return original_rollback(connection)

    monkeypatch.setattr(engine.dialect, "do_rollback", rollback_with_cancellation)
    app.include_router(router)
    added = list(app.router.routes[-len(router.routes):])
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, engine, state
    finally:
        for route in added:
            app.router.routes.remove(route)
        engine.dispose()


@pytest.mark.parametrize("status", [504, 409])
def test_rollback_cancellation_preserves_primary_http_error(lifecycle, status, caplog):
    client, engine, state = lifecycle
    baseline = client.get("/__test_cleanup/healthy").json()
    response = client.get(f"/__test_cleanup/rollback/{status}")
    assert response.status_code == status
    message = response.json()["error"]["message"]
    assert ("Dashboard computation exceeded its deadline" in message
            if status == 504 else message == "original failure")
    assert state["invalidated"] == 1
    assert "Database cleanup failed" in caplog.text
    following = client.get("/__test_cleanup/healthy")
    assert following.status_code == 200
    assert following.json()["pid"] != state["pid"]
    assert following.json()["statement"] == baseline["statement"]
    assert following.json()["lock"] == baseline["lock"]
    assert engine.pool.checkedout() == 0


def test_cleanup_failure_without_primary_error_is_not_suppressed(lifecycle):
    client, engine, state = lifecycle
    assert client.get("/__test_cleanup/rollback/200").status_code == 500
    assert state["invalidated"] == 1
    assert client.get("/__test_cleanup/healthy").status_code == 200
    assert engine.pool.checkedout() == 0


@pytest.mark.parametrize("kind", ["sql", "python"])
def test_deadline_invalidates_before_rollback_and_next_request_is_clean(lifecycle, monkeypatch, kind):
    client, engine, state = lifecycle
    monkeypatch.setattr(cache, "DEADLINE_SECONDS", .25)
    baseline = client.get("/__test_cleanup/healthy").json()
    assert client.get(f"/__test_cleanup/deadline/{kind}").status_code == 504
    assert state["invalidated"] == 1
    following = client.get("/__test_cleanup/healthy").json()
    assert following["pid"] != state["pid"]
    assert following["statement"] == baseline["statement"]
    assert following["lock"] == baseline["lock"]
    assert engine.pool.checkedout() == 0
    assert cache._deadline.get() is None


def test_success_restores_settings_without_committing_writes(lifecycle):
    client, _, _ = lifecycle
    assert client.get("/__test_cleanup/write").status_code == 200


def test_commit_failure_is_not_suppressed(lifecycle, monkeypatch):
    client, engine, _ = lifecycle

    def failed_commit(connection):
        raise QueryCanceled("commit canceled")

    monkeypatch.setattr(engine.dialect, "do_commit", failed_commit)
    assert client.get("/__test_cleanup/commit").status_code == 500
    assert client.get("/__test_cleanup/healthy").status_code == 200


def test_tiny_remaining_budget_does_not_install_tiny_sql_timeout():
    class NoSQL:
        def execute(self, *args, **kwargs):
            pytest.fail("Tiny remaining budget must not issue SQL")

    token = cache._deadline.set(cache.time.monotonic() + .01)
    try:
        with pytest.raises(HTTPException) as error:
            cache.check_deadline(NoSQL())
        assert error.value.status_code == 504
    finally:
        cache._deadline.reset(token)