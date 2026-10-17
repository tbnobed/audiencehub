import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.models import Profile
from app.privacy import service
from app.privacy.metadata import FALSE_DEFINITION, REDACTED, metadata_match_counts, redactor, repair_completed_request
from test_consent_ingestion_pg import db
from test_privacy_pg import people, assert_all_tables_erased


def test_metadata_literal_boundaries_and_credential_exclusions():
    _, redact = redactor({("email", "erase@example.org"), ("phone", "+12025550123")})
    assert redact("Email ERASE@EXAMPLE.ORG; phone +1 (202) 555-0123.") == f"Email {REDACTED}; phone {REDACTED}."
    assert redact("erase@example.org.csv") == REDACTED + ".csv"
    assert redact("noterase@example.org erase@example.org.evil x+erase@example.org") == "noterase@example.org erase@example.org.evil x+erase@example.org"
    value = {"notes": "erase@example.org", "credentials": {"token": "erase@example.org"},
             "headers": {"Authorization": "erase@example.org"}, "api_key": "erase@example.org"}
    clean = redact(value)
    assert clean["notes"] == REDACTED
    assert clean["credentials"] == value["credentials"]
    assert clean["headers"] == value["headers"]
    assert clean["api_key"] == value["api_key"]


def test_deletion_scrubs_metadata_without_broadening_audiences_or_touching_auth(db, tmp_path, monkeypatch):
    from app.activations.security import encrypt
    from app.segments.compiler import compile_definition, validate_definition
    admins, source, partner = people(db)
    identifier = partner.email
    partner_id = partner.id
    admins[0].email = identifier
    source.settings = {"notes": f"Follow up {identifier}", "api_key": identifier,
                       "credentials": {"password": identifier}}
    db.flush()
    retained = Profile(email="retained@example.org", address1=f"Care of {identifier}",
                       search_text=f"retained@example.org {identifier}")
    db.add(retained); db.flush()
    definition = {"version": 1, "root": {"op": "and", "rules": [
        {"type": "profile", "field": "email", "operator": "eq", "value": identifier.upper()}
    ]}}
    sid = db.scalar(text("""INSERT INTO segments(name,description,definition,status)
      VALUES(:name,:description,CAST(:definition AS jsonb),'active') RETURNING id"""),
      {"name": f"Audience for {identifier}", "description": f"Audit {identifier.upper()}",
       "definition": json.dumps(definition)})
    dependent = db.scalar(text("""INSERT INTO segments(name,definition,status)
      VALUES('Exclusion audience',CAST(:definition AS jsonb),'active') RETURNING id"""),
      {"definition": json.dumps({"version": 1, "root": {"op": "and", "negate": True,
        "rules": [{"type": "segment", "segment_id": sid, "operator": "in"}]}})})
    db.execute(text("INSERT INTO segment_membership(segment_id,profile_id) VALUES(:segment,:profile)"),
               {"segment": dependent, "profile": retained.id})
    encrypted_credentials = encrypt({"token": identifier})
    destination = db.scalar(text("""INSERT INTO destinations(name,type,config,secret_encrypted)
      VALUES(:name,'csv',CAST(:config AS jsonb),:secret) RETURNING id"""),
      {"name": f"Export {identifier}", "config": json.dumps({"include_pii": True, "notes": identifier, "api_key": identifier}),
       "secret": encrypted_credentials})
    mapping = {"email": {"kind": "constant", "value": identifier}}
    activation = db.scalar(text("""INSERT INTO activations(name,segment_id,destination_id,field_mapping,column_order)
      VALUES(:name,:segment,:destination,CAST(:mapping AS jsonb),ARRAY['email']) RETURNING id"""),
      {"name": f"Send {identifier}", "segment": sid, "destination": destination, "mapping": json.dumps(mapping)})
    root = tmp_path / "exports"; root.mkdir()
    monkeypatch.setattr(get_settings(), "export_dir", str(root))
    exported = root / "metadata.csv"; exported.write_text("email\n" + identifier + "\n")
    run = db.scalar(text("""INSERT INTO activation_runs(activation_id,status,file_path,snapshot_encrypted)
      VALUES(:activation,'succeeded',:path,:snapshot) RETURNING id"""),
      {"activation": activation, "path": str(exported),
       "snapshot": encrypt({"mapping": mapping, "segment_name": identifier, "secret": {"token": identifier}})})
    # No membership tracking here: the constant, not the selected partner, leaks.
    uploads = tmp_path / "uploads"; uploads.mkdir()
    monkeypatch.setattr(get_settings(), "upload_dir", str(uploads))
    uploaded = uploads / "metadata.csv"; uploaded.write_text("email\nretained@example.org\n")
    imported = db.scalar(text("""INSERT INTO imports(source_id,filename,file_path,record_type)
      VALUES(:source,:filename,:path,'contact') RETURNING id"""),
      {"source": source.id, "filename": identifier + ".csv", "path": str(uploaded)})
    db.execute(text("""INSERT INTO import_batch_diagnostics(import_id,record_number,diagnostics)
      VALUES(:id,2,CAST(:notes AS jsonb))"""), {"id": imported, "notes": json.dumps([{"message": identifier}])})
    db.execute(text("""INSERT INTO audit_log(actor_type,action,entity_type,details)
      VALUES('system','fixture','fixture',CAST(:details AS jsonb))"""),
      {"details": json.dumps({"notes": [f"Archived {identifier.upper()}"], identifier: "key"})})
    db.execute(text("INSERT INTO events(profile_id,source_id,type,name,occurred_at,properties) VALUES(:id,:source,'track',:name,now(),CAST(:value AS jsonb))"),
               {"id": retained.id, "source": source.id, "name": "Metadata note",
                "value": json.dumps({"notes": identifier})})
    request = service.create_deletion(db, "email", identifier, admins[0].id)
    service.approve_deletion(db, request["id"], admins[1].id)
    result = service.execute_deletion(db, request["id"])
    assert result["audiences_frozen"] == 2
    assert_all_tables_erased(db, [partner_id])
    assert metadata_match_counts(db, {("email", identifier)}) == {}
    assert not uploaded.exists() and not exported.exists()
    assert db.scalar(text("SELECT filename FROM imports WHERE id=:id"), {"id": imported}) == REDACTED + ".csv"
    assert db.scalar(text("SELECT snapshot_encrypted FROM activation_runs WHERE id=:id"), {"id": run}) is None
    for audience in (sid, dependent):
        row = db.execute(text("SELECT definition,status FROM segments WHERE id=:id"), {"id": audience}).one()
        assert row == (FALSE_DEFINITION, "archived")
        validate_definition(db, row.definition)
        assert db.execute(compile_definition(db, row.definition)).all() == []
    assert db.scalar(text("SELECT count(*) FROM segment_membership WHERE segment_id=:id"), {"id": dependent}) == 0
    assert not db.scalar(text("SELECT is_active FROM activations WHERE id=:id"), {"id": activation})
    assert db.scalar(text("SELECT secret_encrypted FROM destinations WHERE id=:id"), {"id": destination}) == encrypted_credentials
    assert db.scalar(text("SELECT email FROM users WHERE id=:id"), {"id": admins[0].id}) == identifier
    settings = db.scalar(text("SELECT settings FROM sources WHERE id=:id"), {"id": source.id})
    assert settings["notes"] == f"Follow up {REDACTED}"
    assert settings["api_key"] == identifier and settings["credentials"]["password"] == identifier
    assert db.scalar(text("SELECT address1 FROM profiles WHERE id=:id"), {"id": retained.id}) == f"Care of {REDACTED}"
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth.deps import current_user
    from app.db import session_scope
    from app.segments.api import router
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[session_scope] = lambda: db
    app.dependency_overrides[current_user] = lambda: admins[1]
    response = TestClient(app).get(f"/api/segments/{sid}")
    assert response.status_code == 200
    assert identifier not in response.text.casefold()


def test_completed_request_repair_is_targeted_authorized_and_idempotent(db):
    admins, source, partner = people(db)
    identifier = partner.email
    request = service.create_deletion(db, "email", identifier, admins[0].id)
    with pytest.raises(ValueError, match="completed deletion"):
        repair_completed_request(db, request["id"], "email", identifier)
    service.approve_deletion(db, request["id"], admins[1].id)
    service.execute_deletion(db, request["id"])
    db.execute(text("""INSERT INTO segments(name,description,definition,status)
      VALUES('Historical metadata',:note,CAST(:definition AS jsonb),'archived')"""),
      {"note": identifier.upper(), "definition": json.dumps(FALSE_DEFINITION)})
    with pytest.raises(ValueError, match="matching identifier"):
        repair_completed_request(db, request["id"], "email", "unrelated@example.org")
    assert metadata_match_counts(db, {("email", identifier)}) == {"segments.description": 1}
    counts = repair_completed_request(db, request["id"], "email", identifier)
    assert counts == {"segments.description": 1}
    assert metadata_match_counts(db, {("email", identifier)}) == {}
    assert repair_completed_request(db, request["id"], "email", identifier) == {}


def test_historical_snapshot_and_export_filename_are_checked_even_after_configuration_changes(db, tmp_path, monkeypatch):
    from app.activations.security import encrypt
    from app.privacy.metadata import redact_known_metadata
    admins, source, partner = people(db)
    identifier = partner.email
    segment = db.scalar(text("""INSERT INTO segments(name,definition,status)
      VALUES('Clean audience',CAST(:definition AS jsonb),'active') RETURNING id"""),
      {"definition": json.dumps(FALSE_DEFINITION)})
    destination = db.scalar(text("INSERT INTO destinations(name,type) VALUES('Clean destination','csv') RETURNING id"))
    activation = db.scalar(text("""INSERT INTO activations(name,segment_id,destination_id,field_mapping)
      VALUES('Clean activation',:segment,:destination,CAST(:mapping AS jsonb)) RETURNING id"""),
      {"segment": segment, "destination": destination, "mapping": json.dumps({"email": {"kind": "profile", "key": "email"}})})
    root = tmp_path / "exports"; root.mkdir()
    monkeypatch.setattr(get_settings(), "export_dir", str(root))
    snapshot_path = root / "old.csv"; snapshot_path.write_text(identifier)
    named_path = root / (identifier + ".csv"); named_path.write_text("Unrelated content")
    for path, snapshot in ((snapshot_path, encrypt({"mapping": {"old": {"kind": "constant", "value": identifier}}})),
                           (named_path, None)):
        db.execute(text("""INSERT INTO activation_runs(activation_id,status,file_path,snapshot_encrypted)
          VALUES(:activation,'succeeded',:path,:snapshot)"""),
          {"activation": activation, "path": str(path), "snapshot": snapshot})
    counts = redact_known_metadata(db, {("email", identifier)})
    assert counts["exports_revoked"] == 2
    assert not snapshot_path.exists() and not named_path.exists()
    assert metadata_match_counts(db, {("email", identifier)}) == {}
    assert db.scalar(text("SELECT is_active FROM activations WHERE id=:id"), {"id": activation})
