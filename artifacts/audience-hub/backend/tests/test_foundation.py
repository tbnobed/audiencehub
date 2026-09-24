from datetime import timedelta
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from app.auth.deps import require_role
from app.config import Settings
from app.db import engine
from app.jobs import queue
from app.models import Job, now


def test_dev_auth_cannot_start_in_production():
    with pytest.raises(ValueError, match="forbidden"):
        Settings(app_env="production", auth_mode="dev")


@pytest.mark.parametrize("role,minimum,allowed", [
    ("viewer", "viewer", True), ("viewer", "analyst", False),
    ("analyst", "analyst", True), ("analyst", "admin", False),
    ("admin", "admin", True),
])
def test_role_guards(role, minimum, allowed):
    user = type("User", (), {"role": role})()
    if allowed:
        assert require_role(minimum)(user) is user
    else:
        with pytest.raises(HTTPException) as error:
            require_role(minimum)(user)
        assert error.value.status_code == 403


def test_claim_heartbeat_and_stale_requeue():
    with Session(engine) as db:
        job = queue.enqueue(db, "noop", dedupe_key="test-claim", max_attempts=2)
        assert queue.enqueue(db, "noop", dedupe_key="test-claim").id == job.id
        db.flush()
        claimed = queue.claim(db)
        assert claimed.id == job.id
        assert claimed.attempts == 1
        assert queue.heartbeat(db, job.id, {"done": 1, "total": 2})
        db.flush()
        assert job.progress["done"] == 1
        job.heartbeat_at = now() - timedelta(minutes=6)
        db.flush()
        assert queue.requeue_stale(db) == 1
        assert job.status == "queued"
        assert queue.claim(db).id == job.id
        job.heartbeat_at = now() - timedelta(minutes=6)
        db.flush()
        assert queue.requeue_stale(db) == 1
        assert job.status == "failed"
        assert queue.retry(db, job.id).status == "queued"
        db.rollback()