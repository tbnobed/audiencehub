"""Reset integration tests run exclusively in the benchmark-owned disposable DB."""
import os

import pytest

from app.reset_demo import DATA_TABLES, RETAINED_TABLES, reset_demo


def test_reset_requires_acknowledgement_and_production_force(monkeypatch, capsys):
    assert set(RETAINED_TABLES) == {
        "users", "sources", "enrichment_attributes", "admin_settings"
    }
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValueError, match="without --yes"):
        reset_demo(None)
    with pytest.raises(ValueError, match="without --force AND --yes"):
        reset_demo(None, yes=True)
    assert "users" in capsys.readouterr().out


@pytest.mark.skipif(os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
                    reason="reset tests require benchmark-owned disposable PostgreSQL")
def test_reset_preserves_config_and_refuses_running_jobs():
    from sqlalchemy import text
    from app.db import engine

    with engine.begin() as conn:
        user = conn.execute(text("""
            INSERT INTO users (subject, email, name, role)
            VALUES ('reset-test', 'reset@example.org', 'Reset', 'admin') RETURNING id
        """)).scalar_one()
        source = conn.execute(text("""
            INSERT INTO sources (key, name, kind, record_types, settings)
            VALUES ('reset-test', 'Reset', 'csv', ARRAY['contact'], '{"mapping":{"email":"email"}}')
            RETURNING id
        """)).scalar_one()
        conn.execute(text("""
            INSERT INTO enrichment_attributes (source_id, key, label, data_type)
            VALUES (:source, 'reset_test_attribute', 'Reset Test', 'text')
        """), {"source": source})
        segment = conn.execute(text("""
            INSERT INTO segments (name, definition, created_by)
            VALUES ('Reset segment', '{"rule":"all"}', :user) RETURNING id
        """), {"user": user}).scalar_one()
        destination = conn.execute(text("""
            INSERT INTO destinations (name, type) VALUES ('Reset destination', 'csv')
            RETURNING id
        """)).scalar_one()
        activation = conn.execute(text("""
            INSERT INTO activations (segment_id, destination_id, created_by)
            VALUES (:segment, :destination, :user) RETURNING id
        """), {"segment": segment, "destination": destination, "user": user}).scalar_one()
        conn.execute(text("""
            INSERT INTO activation_runs (activation_id, status)
            VALUES (:activation, 'succeeded')
        """), {"activation": activation})
        profile = conn.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id")).scalar_one()
        conn.execute(text("""
            INSERT INTO source_records (source_id, external_id, profile_id, raw_hash)
            VALUES (:source, 'contact-1', :profile, 'hash')
        """), {"source": source, "profile": profile})
        conn.execute(text("""
            INSERT INTO segment_membership (segment_id, profile_id) VALUES (:segment, :profile)
        """), {"segment": segment, "profile": profile})
        conn.execute(text("""
            INSERT INTO audit_log (user_id, actor_type, action)
            VALUES (:user, 'system', 'reset.test')
        """), {"user": user})
        job = conn.execute(text("""
            INSERT INTO jobs (type, status) VALUES ('test', 'running') RETURNING id
        """)).scalar_one()

    with pytest.raises(RuntimeError, match="running job"):
        reset_demo(engine, yes=True)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM profiles WHERE id=:id"),
                            {"id": profile}).scalar_one() == 1
        conn.execute(text("UPDATE jobs SET status='queued' WHERE id=:id"), {"id": job})

    reset_demo(engine, yes=True)
    with engine.connect() as conn:
        for table in DATA_TABLES:
            assert conn.execute(text(f"SELECT count(*) FROM public.{table}")).scalar_one() == 0, table
        assert conn.execute(text("SELECT subject FROM users WHERE id=:id"),
                            {"id": user}).scalar_one() == "reset-test"
        assert conn.execute(text("SELECT settings FROM sources WHERE id=:id"),
                            {"id": source}).scalar_one() == {"mapping": {"email": "email"}}
        assert conn.execute(text("""
            SELECT label FROM enrichment_attributes
            WHERE source_id=:source AND key='reset_test_attribute'
        """), {"source": source}).scalar_one() == "Reset Test"
        assert conn.execute(text("SELECT fiscal_year_start_month FROM admin_settings WHERE id=1")
                            ).scalar_one() == 1