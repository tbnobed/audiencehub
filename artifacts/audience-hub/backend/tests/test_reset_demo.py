"""Reset integration tests run exclusively in the benchmark-owned disposable DB."""
import os
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import date

import pytest

from app.reset_demo import DATA_TABLES, RETAINED_TABLES, reset_demo


@pytest.fixture
def reset_engine():
    """A fresh database avoids autovacuum triggered by preceding scale tests.

    NOWAIT correctly refuses autovacuum's ShareUpdateExclusiveLock, not just
    application writers. Do not disable that safety check or retry the reset.
    """
    if os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1":
        pytest.skip("reset tests require benchmark-owned disposable PostgreSQL")
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["DATABASE_URL"])
    name = "reset_test_" + uuid.uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    isolated_url = url.set(database=name)
    engine = create_engine(isolated_url)
    created = False
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        env = dict(os.environ, DATABASE_URL=isolated_url.render_as_string(hide_password=False))
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[1], env=env, check=True,
            capture_output=True,
        )
        yield engine
    finally:
        engine.dispose()
        if created:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


def test_reset_requires_acknowledgement_and_production_force(monkeypatch, capsys):
    assert set(RETAINED_TABLES) == {
        "users", "sources", "enrichment_attributes", "admin_settings",
        "master_id_registry", "source_connections", "canonical_field_catalog",
    }
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValueError, match="without --yes"):
        reset_demo(None)
    with pytest.raises(ValueError, match="without --force AND --yes"):
        reset_demo(None, yes=True)
    assert "users" in capsys.readouterr().out


def test_pull_reset_inventory_is_explicit_and_disjoint():
    assert {"source_import_receipts", "source_codes", "products", "vod_titles"} <= set(DATA_TABLES)
    assert "source_connections" in RETAINED_TABLES
    assert not set(DATA_TABLES) & set(RETAINED_TABLES)


@pytest.mark.skipif(os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
                    reason="reset tests require benchmark-owned disposable PostgreSQL")
def test_reset_preserves_config_and_refuses_running_jobs(reset_engine):
    from sqlalchemy import text
    engine = reset_engine

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE admin_settings SET fiscal_year_start_month=7,
                dashboard_default_preset='custom',
                dashboard_default_from='2020-01-01',
                dashboard_default_to='2024-12-31'
            WHERE id=1
        """))
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
            assert conn.execute(text(f"SELECT count(*) FROM public.{table}")).scalar_one() == (23 if table == "segments" else 0), table
        assert conn.scalar(text("""SELECT count(*) FROM segments
            WHERE status='active' AND refresh_schedule='daily' AND created_by=:owner"""),
            {"owner": user}) == 23
        assert conn.execute(text("SELECT subject FROM users WHERE id=:id"),
                            {"id": user}).scalar_one() == "reset-test"
        assert conn.execute(text("SELECT settings FROM sources WHERE id=:id"),
                            {"id": source}).scalar_one() == {"mapping": {"email": "email"}}
        assert conn.execute(text("""
            SELECT label FROM enrichment_attributes
            WHERE source_id=:source AND key='reset_test_attribute'
        """), {"source": source}).scalar_one() == "Reset Test"
        assert conn.execute(text("""
            SELECT fiscal_year_start_month, dashboard_default_preset,
                   dashboard_default_from, dashboard_default_to
            FROM admin_settings WHERE id=1
        """)).one() == (7, "custom", date(2020, 1, 1), date(2024, 12, 31))


def test_reset_refuses_busy_dataset_without_deleting_rows(reset_engine):
    from sqlalchemy import text
    with reset_engine.begin() as conn:
        profile = conn.scalar(text("INSERT INTO profiles DEFAULT VALUES RETURNING id"))
    with reset_engine.connect() as blocker:
        # This is the actual lock mode observed on the flaky suite's
        # autovacuum worker. A real worker/read lock must also remain protected.
        blocker.execute(text("LOCK TABLE source_records IN SHARE UPDATE EXCLUSIVE MODE"))
        with pytest.raises(RuntimeError, match="dataset table is busy"):
            reset_demo(reset_engine, yes=True)
        assert blocker.scalar(text("SELECT count(*) FROM profiles WHERE id=:id"),
                              {"id": profile}) == 1
        blocker.rollback()
    reset_demo(reset_engine, yes=True)
    with reset_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM profiles")) == 0


def seed_pull_reset_data(engine):
    from sqlalchemy import text
    from app.activations.security import encrypt
    ciphertext = encrypt({"private_key": "test-only-key", "user": "test-reader"})
    with engine.begin() as conn:
        source_id = conn.scalar(text("""INSERT INTO sources(key,name,kind,record_types)
            VALUES ('reset-pull','Reset pull','snowflake',ARRAY['contact','resource']) RETURNING id"""))
        conn.execute(text("""INSERT INTO source_connections(source_id,credentials,config,state)
            VALUES (:id,:secret,'{"mappings":[{"id":"people","feed":"people","table":"PEOPLE"}]}',
            CAST(:state AS jsonb))"""),
                     {"id": source_id, "secret": ciphertext,
                      "state": '{"cursors":{"people":"123"},"pending":{"batch_id":123,"manifests":[]}}'})
        import_id = conn.scalar(text("""INSERT INTO imports(source_id,filename,file_path,record_type,status)
            VALUES (:id,'reset.csv','/not-read/reset.csv','contact','completed') RETURNING id"""),
                                {"id": source_id})
        conn.execute(text("""INSERT INTO source_import_receipts(source_id,digest,record_type,import_id,completed)
            VALUES (:source,'test-digest','contact',:import,true)"""),
                     {"source": source_id, "import": import_id})
        for table in ("source_codes", "products", "vod_titles"):
            conn.execute(text(f"INSERT INTO {table}(key,name) VALUES ('reset-reference','Reset reference')"))
        conn.execute(text("""INSERT INTO gifts(source_id,external_id,amount,gift_date,attributes)
            VALUES (:source,'reset-gift',10,'2026-01-01','{"source_code":"reset-reference"}')"""),
                     {"source": source_id})
        conn.execute(text("""INSERT INTO events(source_id,type,name,occurred_at,properties)
            VALUES (:source,'track','vod_viewed','2026-01-01',
                    '{"vod_title_id":"reset-reference","product_id":"reset-reference"}')"""),
                     {"source": source_id})
    return source_id, ciphertext


def test_reset_clears_pull_receipts_resources_and_state_preserves_encrypted_config(reset_engine):
    from sqlalchemy import text
    from app.activations.security import decrypt
    source_id, ciphertext = seed_pull_reset_data(reset_engine)
    reset_demo(reset_engine, yes=True)
    with reset_engine.connect() as conn:
        for table in ("imports", "source_import_receipts", "gifts", "events", "source_codes", "products", "vod_titles"):
            assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        row = conn.execute(text("SELECT credentials,config,state FROM source_connections WHERE source_id=:id"),
                           {"id": source_id}).mappings().one()
        assert bytes(row["credentials"]) == ciphertext
        assert decrypt(row["credentials"])["private_key"] == "test-only-key"
        assert row["config"] == {"mappings": [{"id": "people", "feed": "people", "table": "PEOPLE"}]}
        assert row["state"] == {}
        assert conn.scalar(text("SELECT count(*) FROM canonical_field_catalog")) > 0


def test_reset_busy_connection_table_leaves_receipts_and_state_untouched(reset_engine):
    from sqlalchemy import text
    source_id, _ = seed_pull_reset_data(reset_engine)
    with reset_engine.connect() as blocker:
        blocker.execute(text("LOCK TABLE source_connections IN ACCESS SHARE MODE"))
        with pytest.raises(RuntimeError, match="dataset table is busy"):
            reset_demo(reset_engine, yes=True)
        assert blocker.scalar(text("SELECT count(*) FROM source_import_receipts")) == 1
        assert blocker.scalar(text("SELECT state->'cursors'->>'people' FROM source_connections WHERE source_id=:id"),
                              {"id": source_id}) == "123"
        assert blocker.scalar(text("SELECT count(*) FROM gifts")) == 1
        blocker.rollback()


def test_reset_refuses_cli_source_mutex_without_deleting(reset_engine):
    from sqlalchemy import text
    source_id, _ = seed_pull_reset_data(reset_engine)
    with reset_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as blocker:
        blocker.execute(text("SELECT pg_advisory_lock(hashtext(:key))"), {"key": f"source-pull:{source_id}"})
        try:
            with pytest.raises(RuntimeError, match="source pull is busy"):
                reset_demo(reset_engine, yes=True)
            assert blocker.scalar(text("SELECT count(*) FROM source_import_receipts")) == 1
            assert blocker.scalar(text("SELECT state ? 'pending' FROM source_connections WHERE source_id=:id"),
                                  {"id": source_id}) is True
        finally:
            blocker.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": f"source-pull:{source_id}"})