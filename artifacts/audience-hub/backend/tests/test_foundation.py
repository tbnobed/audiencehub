from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.deps import current_user, require_role
from app.config import Settings
from app.db import engine
from app.jobs import queue
from app.models import now


def test_dev_auth_startup_guard_and_stale_configuration():
    values = {
        "database_url": "postgresql+psycopg://localhost/audience_hub",
        "secret_key": "development-secret",
        "fernet_key": "development-fernet-key",
        "pii_hash_pepper": "development-pepper",
        "auth_mode": "dev",
    }
    assert Settings(**values, app_env="development", job_stale_seconds=7).job_stale_seconds == 7
    with pytest.raises(ValueError, match="forbidden"):
        Settings(**values, app_env="production")
    with pytest.raises(ValueError):
        Settings(**values, app_env="development", job_stale_seconds=0)


def _sample_role_app():
    api = FastAPI()

    for minimum in ("viewer", "analyst", "admin"):
        def make_endpoint(required_role):
            def endpoint(user=Depends(require_role(required_role))):
                return {"role": user.role}
            return endpoint

        api.add_api_route(f"/sample/{minimum}", make_endpoint(minimum), methods=["GET"])
    return api


@pytest.mark.parametrize("minimum", ["viewer", "analyst", "admin"])
@pytest.mark.parametrize("role", ["viewer", "analyst", "admin"])
def test_sample_fastapi_route_role_guards(role, minimum):
    api = _sample_role_app()
    api.dependency_overrides[current_user] = lambda: SimpleNamespace(role=role)
    response = TestClient(api).get(f"/sample/{minimum}")
    allowed = {"viewer": 0, "analyst": 1, "admin": 2}[role] >= {
        "viewer": 0, "analyst": 1, "admin": 2
    }[minimum]
    assert response.status_code == (200 if allowed else 403)


def test_api_post_rejects_missing_csrf_token():
    from app.main import app

    response = TestClient(app).post("/api/admin/jobs/noop")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_real_sources_and_import_routes_enforce_roles():
    from app.main import app

    key = f"route_guard_{uuid.uuid4().hex[:12]}"
    with TestClient(app) as client:
        for role in ("viewer", "analyst", "admin"):
            assert client.post("/auth/dev-login", json={"role": role}).status_code == 200
            csrf = client.get("/api/me").json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf}
            assert client.get("/api/sources").status_code == 200
            assert client.get("/api/imports").status_code == (403 if role == "viewer" else 200)
            result = client.post("/api/sources", headers=headers, json={
                "key": key, "name": "Synthetic route guard", "record_types": ["contact"],
            })
            if role == "admin":
                assert result.status_code == 201
                source_id = result.json()["id"]
                assert client.delete(f"/api/sources/{source_id}", headers=headers).status_code == 200
            else:
                assert result.status_code == 403


def test_enqueue_dedupe_queued_job_is_a_noop():
    with Session(engine) as db:
        key = f"test-dedupe-{uuid.uuid4()}"
        job = queue.enqueue(db, "noop", {"seconds": 0}, dedupe_key=key)
        duplicate = queue.enqueue(db, "noop", {"seconds": 20}, dedupe_key=key, priority=1)
        assert duplicate.id == job.id
        assert duplicate.status == "queued"
        assert duplicate.payload == {"seconds": 0}
        assert duplicate.priority == 100
        db.rollback()


def test_claim_and_heartbeat_persist_progress():
    with Session(engine) as db:
        job = queue.enqueue(
            db, "noop", dedupe_key=f"test-heartbeat-{uuid.uuid4()}", priority=-1000000
        )
        claimed = queue.claim(db)
        assert claimed.id == job.id
        assert claimed.attempts == 1
        assert queue.heartbeat(db, job.id, {"done": 1, "total": 2})
        db.flush()
        assert job.progress == {"done": 1, "total": 2}
        db.rollback()


def test_concurrent_sessions_claim_distinct_jobs():
    with Session(engine) as setup:
        prefix = f"test-concurrent-{uuid.uuid4()}"
        first_job = queue.enqueue(setup, "noop", dedupe_key=f"{prefix}-1", priority=-1000000)
        second_job = queue.enqueue(setup, "noop", dedupe_key=f"{prefix}-2", priority=-999999)
        first_id, second_id = first_job.id, second_job.id
        setup.commit()

    with Session(engine) as first_session, Session(engine) as second_session:
        first_claim = queue.claim(first_session)
        second_claim = queue.claim(second_session)
        assert first_claim.id == first_id
        assert second_claim.id == second_id
        assert first_claim.id != second_claim.id
        first_session.commit()
        second_session.commit()

        queue.succeed(first_session, first_id)
        queue.succeed(second_session, second_id)
        first_session.commit()
        second_session.commit()


def test_failure_backoff_then_failed_at_max_attempts():
    with Session(engine) as db:
        job = queue.enqueue(
            db, "noop", dedupe_key=f"test-backoff-{uuid.uuid4()}",
            priority=-1000000, max_attempts=3,
        )
        for attempt, delay in ((1, 20), (2, 40)):
            claimed = queue.claim(db)
            assert claimed.id == job.id
            assert claimed.attempts == attempt
            before_fail = now()
            queue.fail(db, job.id, "synthetic handler failure")
            db.flush()
            assert job.status == "queued"
            assert timedelta(seconds=delay - 1) <= job.run_after - before_fail
            assert job.run_after - before_fail <= timedelta(seconds=delay + 1)
            job.run_after = now()
            db.flush()

        claimed = queue.claim(db)
        assert claimed.id == job.id
        assert claimed.attempts == 3
        queue.fail(db, job.id, "synthetic handler failure")
        db.flush()
        assert job.status == "failed"
        assert job.finished_at is not None
        db.rollback()


def test_stale_requeue_and_stale_max_attempts_failure():
    with Session(engine) as db:
        stale = queue.enqueue(
            db, "noop", dedupe_key=f"test-stale-{uuid.uuid4()}", max_attempts=3
        )
        stale.status = "running"
        stale.attempts = 1
        stale.heartbeat_at = now() - timedelta(seconds=30)
        terminal = queue.enqueue(
            db, "noop", dedupe_key=f"test-stale-terminal-{uuid.uuid4()}", max_attempts=2
        )
        terminal.status = "running"
        terminal.attempts = 2
        terminal.heartbeat_at = now() - timedelta(seconds=30)
        db.flush()

        assert queue.requeue_stale(
            db, stale_after=timedelta(seconds=10), job_ids=[stale.id, terminal.id]
        ) == 2
        assert stale.status == "queued"
        assert stale.heartbeat_at is None
        assert stale.error == "Worker heartbeat expired"
        assert terminal.status == "failed"
        assert terminal.finished_at is not None
        db.rollback()


@pytest.mark.skipif(
    not os.environ.get("AH_WORKER_RECOVERY_TEST_DATABASE_URL"),
    reason="requires an explicitly provisioned, isolated worker recovery test database",
)
def test_worker_subprocess_kill_restart_recovers_synthetic_sleep_job():
    backend = Path(__file__).resolve().parents[1]
    script = backend.parent / "scripts" / "manual_worker_recovery.py"
    environment = os.environ.copy()
    environment.update(
        DATABASE_URL=environment["AH_WORKER_RECOVERY_TEST_DATABASE_URL"],
        APP_ENV="development",
        AUTH_MODE="dev",
        JOB_STALE_SECONDS="1",
    )
    result = subprocess.run(
        [
            sys.executable, str(script),
            "--stale-seconds", "1",
            "--sleep-seconds", "2",
            "--timeout-seconds", "15",
        ],
        cwd=backend,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert "succeeded; attempts=2" in result.stdout