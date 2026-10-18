"""Opt-in identity-only 500k fixture. Never uses or mutates the live database."""
import os
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.auth.deps import current_user
from app.db import session_scope
from app.identity.api import router
from app.models import User
from test_consent_ingestion_pg import db

pytestmark = pytest.mark.skipif(os.getenv("KINSHIP_MASTER_SCALE") != "1",
                                reason="explicit disposable 500k identity fixture opt-in required")


def test_10k_lookup_real_api_at_500k(db):
    started = time.perf_counter()
    db.execute(text("""
      INSERT INTO profiles(first_seen_at,last_seen_at)
      SELECT now(),now() FROM generate_series(1,500000);
      INSERT INTO identity_xref(master_id,key_type,key_value,source_id)
      SELECT master_id,'donor_account_number',id::text,0 FROM profiles;
      ANALYZE profiles; ANALYZE identity_xref; ANALYZE master_id_aliases
    """))
    user = User(subject="scale-test", email="scale@example.org", name="Scale test",
                role="analyst", is_active=True)
    db.add(user)
    db.commit()
    fixture_seconds = time.perf_counter() - started
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="isolated-test-only")
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[session_scope] = lambda: db

    @app.get("/test-session")
    def test_session(request: Request):
        request.session["csrf"] = "scale-csrf"
        return {}

    client = TestClient(app)
    client.get("/test-session")
    body = {"items": [{"key_type": "donor_account_number", "value": str(i * 49 + 1)}
                      for i in range(10000)]}
    started = time.perf_counter()
    response = client.post("/api/identity/lookup/bulk", json=body,
                           headers={"X-CSRF-Token": "scale-csrf"})
    seconds = time.perf_counter() - started
    assert response.status_code == 200, response.text[:1000]
    assert len(response.json()["items"]) == 10000
    assert all(i["status"] == "matched" for i in response.json()["items"])
    assert db.scalar(text("SELECT count(*) FROM audit_log WHERE action='identity.lookup'")) == 1
    print(f"\n500k fixture_seconds={fixture_seconds:.3f}; 10k API+audit+JSON_seconds={seconds:.3f}")
    assert seconds < 2
