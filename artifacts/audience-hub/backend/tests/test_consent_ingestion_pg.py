"""Self-contained PG tests: explicitly supply a disposable CONSENT_TEST_DATABASE_URL.

Each test creates and drops only its own random schema; never uses app.db.engine.
Run: CONSENT_TEST_DATABASE_URL=postgresql+psycopg://... pytest tests/test_consent_ingestion_pg.py
"""
import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.identity.resolver import _materialize_source_attributes_batch, _move_profile_references
from app.imports.mapping import suggest_mapping, validate_mapping
from app.imports.service import run_import, _upsert_consents
from app.imports.validation import map_and_validate_row
from app.models import Import, Profile, Source


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = os.environ.get("CONSENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set CONSENT_TEST_DATABASE_URL to a disposable PostgreSQL database")
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    schema = "consent_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    local = create_engine(url, connect_args={"options": f"-c search_path={schema},public"})
    try:
        # Exercise the deployed schema, including partition and index migrations.
        versions = Path(__file__).resolve().parents[1] / "alembic/versions"
        with local.connect() as connection:
            context = MigrationContext.configure(connection)
            with context.begin_transaction(), Operations.context(context):
                for path in sorted(versions.glob("*.py")):
                    spec = importlib.util.spec_from_file_location(path.stem, path)
                    migration = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(migration)
                    migration.upgrade()
        with Session(local) as session:
            yield session
    finally:
        local.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def ingest(db, tmp_path, body, record_type="contact", source=None):
    path = tmp_path / (uuid4().hex + ".csv")
    path.write_text(body)
    headers = body.splitlines()[0].split(",")
    mapping = validate_mapping(suggest_mapping(headers, record_type), record_type, headers)
    if source is None:
        source = Source(key=uuid4().hex, name="Consent regression", kind="csv",
                        record_types=[record_type])
        db.add(source)
        db.flush()
    imported = Import(source_id=source.id, filename=path.name, file_path=str(path),
                      record_type=record_type, mapping={"columns": mapping, "options": {}})
    db.add(imported)
    db.commit()
    run_import(imported.id, db=db)
    rows = db.execute(text("SELECT * FROM source_records WHERE source_id=:id ORDER BY external_id"),
                      {"id": source.id}).mappings().all()
    return source, rows


def materialize(db, rows):
    profile = Profile()
    db.add(profile)
    db.flush()
    _materialize_source_attributes_batch(db, [(dict(row), profile.id) for row in rows])
    return profile.id


def status(db, profile):
    return db.scalar(text("SELECT status FROM consents WHERE profile_id=:id AND channel='email'"),
                     {"id": profile})


def test_mapping_is_explicit():
    columns = suggest_mapping(["email", "email_consent"], "contact")
    valid = map_and_validate_row({"email": "person@example.org", "email_consent": "opted_in"},
                                 columns, "contact")
    assert valid["values"]["consent"]["status"] == "opted_in"
    invalid = map_and_validate_row({"email": "person@example.org", "email_consent": "yes"},
                                   columns, "contact")
    assert invalid["errors"]


@pytest.mark.parametrize("consent,expected", [("opted_in", "opted_in"),
                                             ("opted_out", "opted_out"),
                                             ("unknown", "unknown"), ("", None)])
def test_import_email_fields_and_no_fabrication(db, tmp_path, consent, expected):
    _, rows = ingest(db, tmp_path, f"external_id,email,email_consent\n1,a@example.org,{consent}\n")
    assert len(rows) == 1
    assert status(db, materialize(db, rows)) == expected


def test_bounce_suppression_overrides_future_import(db, tmp_path):
    _, rows = ingest(db, tmp_path,
                     "external_id,email,email_consent,hard_bounce\n1,a@example.org,opted_in,true\n")
    assert status(db, materialize(db, rows)) == "opted_out"
    assert db.scalar(text("SELECT count(*) FROM suppressions WHERE reason='hard_bounce'")) == 1
    _, later = ingest(db, tmp_path, "external_id,email,email_consent\n2,a@example.org,opted_in\n")
    assert status(db, materialize(db, later)) == "opted_out"


def test_optout_survives_unresolved_reimport(db, tmp_path):
    source, _ = ingest(db, tmp_path, "external_id,email,email_consent\n1,a@example.org,opted_out\n")
    _, rows = ingest(db, tmp_path, "external_id,email,email_consent\n1,a@example.org,opted_in\n",
                     source=source)
    assert status(db, materialize(db, rows)) == "opted_out"


def test_same_batch_duplicate_cannot_erase_optout(db, tmp_path):
    _, rows = ingest(db, tmp_path,
                     "external_id,email,email_consent\n"
                     "1,a@example.org,opted_out\n1,a@example.org,opted_in\n")
    assert status(db, materialize(db, rows)) == "opted_out"


@pytest.mark.parametrize("loser_out", [True, False])
def test_esp_merge_and_newer_optin_cannot_reauthorize(db, tmp_path, loser_out):
    source, rows = ingest(db, tmp_path,
                         "email,channel,status,captured_at\na@example.org,email,opted_out,2020-01-01T00:00:00Z\n",
                         "consent")
    opted_out = materialize(db, rows)
    _, newer = ingest(db, tmp_path,
                      "email,channel,status,captured_at\nb@example.org,email,opted_in,2025-01-01T00:00:00Z\n",
                      "consent")
    opted_in = materialize(db, newer)
    winner, loser = (opted_in, opted_out) if loser_out else (opted_out, opted_in)
    _move_profile_references(db, winner, loser)
    assert status(db, winner) == "opted_out"
    assert status(db, loser) is None
    _upsert_consents(db, source.id, [{
        "record_type": "consent", "profile_id": winner, "source_record_id": rows[0]["id"],
        "values": {"channel": "email", "status": "opted_in",
                   "captured_at": datetime(2030, 1, 1, tzinfo=timezone.utc)},
    }])
    assert status(db, winner) == "opted_out"


def test_index_migration_roundtrip(db, tmp_path):
    _, rows = ingest(db, tmp_path,
                     "external_id,email,email_consent\n"
                     "1,a@example.org,opted_in\n2,b@example.org,opted_out\n"
                     "3,c@example.org,\n4,d@example.org,opted_in\n")
    profiles = [materialize(db, [row]) for row in rows]
    for row, profile in zip(rows, profiles):
        db.execute(text("UPDATE source_records SET profile_id=:profile, "
                        "attributes=attributes-'_import_consent' WHERE id=:id"),
                   {"profile": profile, "id": row["id"]})
    db.execute(text("DELETE FROM consents WHERE profile_id=ANY(:ids)"),
               {"ids": profiles[:3]})
    db.execute(text("UPDATE consents SET status='opted_out' WHERE profile_id=:id"),
               {"id": profiles[3]})
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0021_consent_channel_status.py"
    spec = importlib.util.spec_from_file_location("consent_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0020_dashboard_rollups"
    with Operations.context(MigrationContext.configure(db.connection())):
        migration.downgrade()
        migration.upgrade()
    definition = db.scalar(text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
        "AND indexname='ix_consents_channel_status'"))
    assert "(channel, status)" in definition
    assert [status(db, profile) for profile in profiles] == [
        "opted_in", "opted_out", None, "opted_out",
    ]