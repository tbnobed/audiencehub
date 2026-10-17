import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.config import get_settings
from app.models import Profile, Source, User
from app.privacy import service
from app.privacy.consent import consent_wins
from test_consent_ingestion_pg import db, ingest, materialize, status


@pytest.mark.parametrize("old,new,oa,na,expected", [
    (("opted_out", 1), ("opted_in", 2), False, False, False),
    (("opted_out", 1), ("opted_in", 2), False, True, True),
    (("opted_out", 2), ("opted_in", 2), False, True, False),
    (("opted_in", 2), ("opted_out", 1), True, False, False),
    (("opted_in", 2), ("opted_out", 2), True, False, True),
    (("opted_in", 2), ("opted_out", 1), False, False, True),
    (("opted_out", 1), ("unknown", 3), False, True, False),
    (("opted_in", 1), ("unknown", 3), False, True, False),
    (("unknown", 3), ("opted_in", 1), False, False, True),
    (("opted_in", 1), ("opted_in", 2), True, False, False),
])
def test_consent_policy(old, new, oa, na, expected):
    row = lambda item: {"status": item[0], "captured_at": datetime(2024, 1, item[1], tzinfo=timezone.utc)}
    assert consent_wins(row(old), row(new), oa, na) is expected


def people(db):
    admins = [User(subject=f"privacy:{uuid4()}", email=f"admin{i}@example.org", name="Privacy test", role="admin", is_active=True) for i in range(2)]
    source = Source(key="privacy_test", name="Privacy test", kind="csv", record_types=["contact", "gift", "event"])
    p = Profile(email="erase@example.org", phone="+12025550123")
    db.add_all([*admins, source, p]); db.flush()
    return admins, source, p


def test_manual_consent_notes_roles_and_suppression(db):
    from app.privacy.api import router
    from app.auth.deps import current_user
    from app.db import session_scope
    admins, source, p = people(db)
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[session_scope] = lambda: db
    app.dependency_overrides[current_user] = lambda: admins[0]
    client = TestClient(app)
    assert client.post(f"/api/profiles/{p.id}/consents", json={"channel": "email", "status": "opted_in", "note": " "}).status_code == 422
    response = client.post(f"/api/profiles/{p.id}/consents", json={"channel": "email", "status": "opted_in", "note": "Explicit preference received."})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "opted_in"
    assert not db.scalar(text("SELECT is_active FROM sources WHERE key='privacy_manual'"))
    assert client.post("/api/admin/suppressions", json={"type": "email", "value": p.email, "reason": "deletion_request"}).status_code == 422
    suppressed = client.post("/api/admin/suppressions", json={"type": "email", "value": "ERASE@example.org"})
    assert suppressed.status_code == 200
    assert "erase@" not in suppressed.text.lower()
    assert status(db, p.id) == "opted_out"
    assert client.post(f"/api/profiles/{p.id}/consents", json={"channel": "email", "status": "opted_in", "note": "Attempt."}).status_code == 422
    assert client.delete(f"/api/admin/suppressions/{suppressed.json()['id']}").status_code == 200
    assert status(db, p.id) == "opted_out"  # removal never silently opts in
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=admins[0].id, role="analyst")
    assert client.post(f"/api/profiles/{p.id}/consents", json={"channel": "email", "status": "opted_in", "note": "Attempt without permission."}).status_code == 403
    assert client.get("/api/admin/deletion-requests").status_code == 403
    assert client.get(f"/api/profiles/{p.id}/consents").status_code == 200
    assert all("erase@example.org" not in value for value in db.scalars(text("SELECT details::text FROM audit_log")))


def test_authoritative_import_and_bulk_precedence(db, tmp_path):
    from app.identity.bulk import resolve_bulk
    source = Source(key="preference_center", name="Preference center", kind="csv", record_types=["consent"],
                    settings={"authoritative_for_consent": True})
    db.add(source); db.flush()
    source, _ = ingest(db, tmp_path, "external_id,email,channel,status,captured_at\nx,a@example.org,email,opted_out,2024-01-01T00:00:00Z\n", "consent", source)
    _, rows = ingest(db, tmp_path, "external_id,email,channel,status,captured_at\nx,a@example.org,email,opted_in,2024-01-02T00:00:00Z\n", "consent", source)
    assert rows[0]["attributes"]["_import_consent"]["status"] == "opted_in"
    resolve_bulk(db)
    pid = db.scalar(text("SELECT profile_id FROM source_records WHERE source_id=:id"), {"id": source.id})
    assert status(db, pid) == "opted_in"
    _, out = ingest(db, tmp_path, "external_id,email,channel,status,captured_at\nx,a@example.org,email,opted_out,2024-01-01T00:00:00Z\n", "consent", source)
    assert out[0]["attributes"]["_import_consent"]["status"] == "opted_in"


@pytest.mark.parametrize("authoritative_is_winner", [True, False])
def test_authoritative_bounded_merge_preserves_new_explicit_choice(db, tmp_path, authoritative_is_winner):
    from app.identity.resolver import _move_profile_references
    regular, out = ingest(db, tmp_path, "external_id,email,channel,status,captured_at\nout,out@example.org,email,opted_out,2024-01-01T00:00:00Z\n", "consent")
    authoritative = Source(key="authoritative", name="Preference center", kind="csv", record_types=["consent"],
                           settings={"authoritative_for_consent": True})
    db.add(authoritative); db.flush()
    _, incoming = ingest(db, tmp_path, "external_id,email,channel,status,captured_at\nin,in@example.org,email,opted_in,2024-01-02T00:00:00Z\n", "consent", authoritative)
    old_id, new_id = materialize(db, out), materialize(db, incoming)
    winner, loser = (new_id, old_id) if authoritative_is_winner else (old_id, new_id)
    _move_profile_references(db, winner, loser)
    assert status(db, winner) == "opted_in"


def test_database_policy_matches_python(db):
    sources = []
    for authoritative in (False, True):
        source = Source(key=str(authoritative), name="Policy fixture", kind="csv", record_types=["consent"],
                        settings={"authoritative_for_consent": authoritative})
        db.add(source); db.flush(); sources.append(source.id)
    for old_status in ("unknown", "opted_in", "opted_out"):
        for new_status in ("unknown", "opted_in", "opted_out"):
            for oa in (False, True):
                for na in (False, True):
                    for new_day in (1, 2, 3):
                        old = {"status": old_status, "captured_at": datetime(2024, 1, 2, tzinfo=timezone.utc)}
                        new = {"status": new_status, "captured_at": datetime(2024, 1, new_day, tzinfo=timezone.utc)}
                        actual = db.scalar(text("SELECT consent_candidate_wins(:os,:ot,:oi,:ns,:nt,:ni)"),
                          {"os": old_status, "ot": old["captured_at"], "oi": sources[int(oa)],
                           "ns": new_status, "nt": new["captured_at"], "ni": sources[int(na)]})
                        assert actual == consent_wins(old, new, oa, na)


def test_unknown_new_profile_reference_fails_closed(db):
    admins, source, p = people(db)
    db.execute(text("CREATE TABLE unreviewed_partner_data(id bigint, subject bigint REFERENCES profiles(id))"))
    with pytest.raises(ValueError, match="Unreviewed"):
        service.create_deletion(db, "email", p.email, admins[0].id)


def test_two_person_approval_and_idempotency(db, monkeypatch):
    admins, source, p = people(db)
    request = service.create_deletion(db, "email", p.email, admins[0].id)
    preview = service.preview_deletion(db, "email", p.email)
    assert set(preview) == {"matched_partners", "counts", "backup_keep_days"}
    assert "erase@example.org" not in json.dumps(preview)
    with pytest.raises(ValueError, match="different admin"):
        service.approve_deletion(db, request["id"], admins[0].id)
    with pytest.raises(ValueError, match="approval"):
        service.execute_deletion(db, request["id"])
    first = service.approve_deletion(db, request["id"], admins[1].id)
    second = service.approve_deletion(db, request["id"], admins[1].id)
    assert first == second
    result = service.execute_deletion(db, request["id"])
    assert result["profiles"] == 1
    assert service.execute_deletion(db, request["id"]) == result
    with pytest.raises(ValueError, match="cannot be removed"):
        suppression = db.scalar(text("SELECT id FROM suppressions WHERE reason='deletion_request' LIMIT 1"))
        service.remove_suppression(db, suppression, admins[1].id)
    other = Profile(email="single@example.org"); db.add(other); db.flush()
    monkeypatch.setattr(get_settings(), "deletion_two_person", False)
    single = service.create_deletion(db, "email", other.email, admins[0].id)
    service.approve_deletion(db, single["id"], admins[0].id)
    service.execute_deletion(db, single["id"])


def test_request_rechecks_new_matches_and_erases_its_encrypted_identifier(db):
    admins, source, p = people(db)
    request = service.create_deletion(db, "email", "late@example.org", admins[0].id)
    assert request["matched_profile_ids"] == []
    assert b"late@example.org" not in request["identifier_encrypted"]
    service.approve_deletion(db, request["id"], admins[1].id)
    late = Profile(email="late@example.org"); db.add(late); db.flush()
    late_id = late.id
    result = service.execute_deletion(db, request["id"])
    assert result["profiles"] == 1
    assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM profiles WHERE id=:id)"), {"id": late_id})
    assert db.scalar(text("SELECT identifier_encrypted FROM deletion_requests WHERE id=:id"), {"id": request["id"]}) is None


def assert_all_tables_erased(db, ids):
    """Query EVERY physical table, including event partitions and non-FK JSON."""
    tables = list(db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")))
    assert len(tables) > 20
    quote = db.get_bind().dialect.identifier_preparer.quote
    for table in tables:
        for row in db.execute(text(f"SELECT * FROM {quote(table)}")).mappings():
            if table == "profiles":
                assert row["id"] not in ids
            for key, value in row.items():
                if key in service.PROFILE_KEYS:
                    assert not service.has_reference(value, ids, key), (table, key)
                if isinstance(value, (dict, list)):
                    assert not service.has_reference(value, ids), (table, key)
            if table == "audit_log" and row["entity_type"] in ("profile", "partner"):
                assert row["entity_id"] not in {str(i) for i in ids}


def test_deletion_completeness_every_table_and_exports(db, tmp_path, monkeypatch):
    admins, source, p = people(db)
    loser = Profile(email="old-alias@example.org", phone="+12025550124", merged_into_id=p.id)
    db.add(loser); db.flush()
    ids = [p.id, loser.id]
    for pid in ids:
        sr = db.scalar(text("""INSERT INTO source_records(source_id,external_id,profile_id,attributes,raw_hash)
          VALUES(:source,:external,:id,CAST(:attrs AS jsonb),'privacy-fixture') RETURNING id"""),
          {"source": source.id, "external": str(pid), "id": pid, "attrs": json.dumps({"nested": {"profile_id": pid}})})
        db.execute(text("""INSERT INTO gifts(source_id,external_id,profile_id,source_record_id,amount,gift_date)
          VALUES(:source,:external,:id,:sr,25,'2024-01-01')"""), {"source": source.id, "external": str(pid), "id": pid, "sr": sr})
        db.execute(text("""INSERT INTO identifiers(type,value,profile_id) VALUES('email',:value,:id)"""),
                   {"value": f"alias-{pid}@example.org", "id": pid})
        db.execute(text("INSERT INTO trait_dirty_profiles(profile_id) VALUES(:id)"), {"id": pid})
        db.execute(text("INSERT INTO profile_traits(profile_id) VALUES(:id)"), {"id": pid})
        db.execute(text("""INSERT INTO consents(profile_id,source_id,channel,status)
          VALUES(:id,:source,'email','opted_in')"""), {"id": pid, "source": source.id})
        db.execute(text("""INSERT INTO client_errors(user_id,category,message,route,profile_id,component_stack)
          VALUES(:actor,'react','render_exception','/profiles/:id',:id,'[]')"""),
          {"actor": admins[0].id, "id": pid})
        db.execute(text("""INSERT INTO dashboard_donor_daily(profile_id,day,gift_count,gift_amount,recurring)
          VALUES(:id,'2024-01-01',1,25,false)"""), {"id": pid})
        db.execute(text("""INSERT INTO enrichment_values(profile_id,source_id,attribute_key,value_text)
          VALUES(:id,:source,'test','licensed')"""), {"id": pid, "source": source.id})
        db.execute(text("""INSERT INTO events(profile_id,source_id,type,name,occurred_at,message_id,properties)
          VALUES(:id,:source,'track','Test',now(),:message,CAST(:properties AS jsonb))"""),
          {"id": pid, "source": source.id, "message": str(pid), "properties": json.dumps({"profile_id": pid})})
        service.audit(db, admins[0].id, "profile.view", "profile", pid, {"profile_id": pid})
    db.execute(text("""INSERT INTO profile_merges(winner_id,loser_id,reason) VALUES(:w,:l,'{}')"""), {"w": p.id, "l": loser.id})
    retained = Profile(email="retained@example.org"); db.add(retained); db.flush()
    db.execute(text("""INSERT INTO events(profile_id,source_id,type,name,occurred_at,message_id,properties)
      VALUES(:id,:source,'track','Retained',now()-interval '1 month','retained',
        '{"keep": "do-not-change"}'::jsonb)"""), {"id": retained.id, "source": source.id})
    db.execute(text("""INSERT INTO jobs(type,status,payload,progress) VALUES('legacy','queued',:payload,'{}')"""),
               {"payload": json.dumps({"profile_ids": ids})})
    segment = db.scalar(text("""INSERT INTO segments(name,definition,status) VALUES('Privacy fixture','{}','draft') RETURNING id"""))
    db.execute(text("INSERT INTO segment_membership(segment_id,profile_id) VALUES(:s,:p)"), {"s": segment, "p": p.id})
    destination = db.scalar(text("INSERT INTO destinations(name,type) VALUES('Privacy fixture','csv') RETURNING id"))
    activation = db.scalar(text("INSERT INTO activations(segment_id,destination_id) VALUES(:s,:d) RETURNING id"),
                           {"s": segment, "d": destination})
    root = tmp_path / "exports"; root.mkdir()
    monkeypatch.setattr(get_settings(), "export_dir", str(root))
    file = root / "privacy.csv"; file.write_text("email\nerase@example.org\n")
    run = db.scalar(text("""INSERT INTO activation_runs(activation_id,status,file_path,snapshot_encrypted)
      VALUES(:a,'succeeded',:path,:snapshot) RETURNING id"""),
      {"a": activation, "path": str(file), "snapshot": b"encrypted fixture"})
    db.execute(text("INSERT INTO activation_run_members(run_id,profile_id) VALUES(:r,:p)"), {"r": run, "p": p.id})
    db.execute(text("INSERT INTO activation_last_members(activation_id,profile_id) VALUES(:a,:p)"), {"a": activation, "p": p.id})
    request = service.create_deletion(db, "email", p.email, admins[0].id)
    assert request["matched_profile_ids"] == ids
    service.approve_deletion(db, request["id"], admins[1].id)
    service.execute_deletion(db, request["id"])
    assert not file.exists()
    assert db.scalar(text("SELECT snapshot_encrypted FROM activation_runs WHERE id=:id"), {"id": run}) is None
    assert db.scalar(text("SELECT revoked_at IS NOT NULL FROM activation_runs WHERE id=:id"), {"id": run})
    assert_all_tables_erased(db, ids)
    assert db.scalar(text("SELECT properties FROM events WHERE message_id='retained'")) == {"keep": "do-not-change"}
    assert db.scalar(text("SELECT count(*) FROM suppressions WHERE reason='deletion_request'")) >= 6


def test_approval_concurrent_deduplication(db):
    import os
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    admins, source, p = people(db)
    request = service.create_deletion(db, "email", p.email, admins[0].id)
    actor, request_id = admins[1].id, request["id"]
    schema = db.scalar(text("SELECT current_schema()"))
    db.commit()
    engine = create_engine(os.environ["CONSENT_TEST_DATABASE_URL"],
                           connect_args={"options": f"-c search_path={schema},public"})
    def approve():
        with Session(engine) as independent:
            result = service.approve_deletion(independent, request_id, actor)
            independent.commit()
            return result
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: approve(), range(2)))
        assert results[0] == results[1]
        assert db.scalar(text("SELECT count(*) FROM jobs WHERE type='deletion.execute'")) == 1
    finally:
        engine.dispose()


def test_privacy_shared_lock_and_schema_isolation(db):
    import os
    from sqlalchemy import create_engine
    from app.privacy.locking import privacy_shared
    schema = db.scalar(text("SELECT current_schema()"))
    independent = create_engine(os.environ["CONSENT_TEST_DATABASE_URL"],
                               connect_args={"options": f"-c search_path={schema},public"})
    privacy_shared(db)
    try:
        with independent.begin() as other:
            assert not other.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(current_schema() || :scope))"),
                                    {"scope": ":audience-hub:privacy"})
            assert other.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:different))"),
                                {"different": "public:audience-hub:privacy"})
    finally:
        independent.dispose()


def test_active_import_defers_deletion(db):
    from app.identity.locking import IdentityResolutionDeferred
    admins, source, p = people(db)
    request = service.create_deletion(db, "email", p.email, admins[0].id)
    service.approve_deletion(db, request["id"], admins[1].id)
    db.execute(text("""INSERT INTO imports(source_id,filename,file_path,record_type,status)
      VALUES(:source,'test.csv','/unused','contact','running')"""), {"source": source.id})
    with pytest.raises(IdentityResolutionDeferred):
        service.execute_deletion(db, request["id"])
    assert db.scalar(text("SELECT EXISTS(SELECT 1 FROM profiles WHERE id=:id)"), {"id": p.id})


def test_file_revocation_failure_rolls_back_and_retry_succeeds(db, tmp_path, monkeypatch):
    admins, source, p = people(db)
    path = tmp_path / "uploads"; path.mkdir()
    file = path / "privacy.csv"; file.write_text("email\nerase@example.org\n")
    iid = db.scalar(text("""INSERT INTO imports(source_id,filename,file_path,record_type,status)
      VALUES(:source,'privacy.csv',:file,'contact','completed') RETURNING id"""),
      {"source": source.id, "file": str(file)})
    db.execute(text("""INSERT INTO source_records(source_id,external_id,profile_id,last_import_id,raw_hash)
      VALUES(:source,'privacy',:id,:iid,'privacy')"""), {"source": source.id, "id": p.id, "iid": iid})
    request = service.create_deletion(db, "email", p.email, admins[0].id)
    service.approve_deletion(db, request["id"], admins[1].id)
    original = service.remove_owned_file
    def denied(*args):
        raise PermissionError("private filesystem path must never be logged")
    monkeypatch.setattr(service, "remove_owned_file", denied)
    with pytest.raises(PermissionError), db.begin_nested():
        service.execute_deletion(db, request["id"])
    assert db.scalar(text("SELECT EXISTS(SELECT 1 FROM profiles WHERE id=:id)"), {"id": p.id})
    assert db.scalar(text("SELECT count(*) FROM suppressions")) == 0
    assert file.exists()
    monkeypatch.setattr(service, "remove_owned_file", original)
    service.execute_deletion(db, request["id"])
    assert not file.exists()


def test_historical_changed_imports_and_diagnostics_are_revoked(db, tmp_path, monkeypatch):
    from app.auth.deps import current_user
    from app.db import session_scope
    from app.imports.api import router
    from app.identity.bulk import resolve_bulk
    admins, source, partner = people(db)
    uploads = tmp_path / "uploads"; uploads.mkdir()
    monkeypatch.setattr(get_settings(), "upload_dir", str(uploads))
    source, _ = ingest(db, uploads,
        "external_id,email,first_name\nhistorical,erase@example.org,Original\n", source=source)
    first = db.execute(text("SELECT id,file_path FROM imports ORDER BY id DESC LIMIT 1")).mappings().one()
    source, rows = ingest(db, uploads,
        "external_id,email,first_name\nhistorical,erase@example.org,Changed\n", source=source)
    second = db.execute(text("SELECT id,file_path FROM imports ORDER BY id DESC LIMIT 1")).mappings().one()
    assert first["id"] != second["id"]
    assert rows[0]["last_import_id"] == second["id"]
    resolve_bulk(db)
    paths = [Path(first["file_path"]), Path(second["file_path"])]
    for imported in (first, second):
        diagnostic = uploads / f"errors-{imported['id']}.csv"
        diagnostic.write_text("record_number,message\n2,erase@example.org\n")
        paths.append(diagnostic)
        db.execute(text("UPDATE imports SET error_file_path=:path WHERE id=:id"),
                   {"path": str(diagnostic), "id": imported["id"]})
    unaffected = Source(key="unaffected", name="Unrelated", kind="csv", record_types=["contact"])
    db.add(unaffected); db.flush()
    retained = uploads / "unaffected.csv"; retained.write_text("email\nkeep@example.org\n")
    retained_error = uploads / "unaffected-errors.csv"; retained_error.write_text("record_number,message\n2,Unrelated warning\n")
    retained_id = db.scalar(text("""INSERT INTO imports(source_id,filename,file_path,error_file_path,record_type)
      VALUES(:source,'unaffected.csv',:path,:error,'contact') RETURNING id"""),
      {"source": unaffected.id, "path": str(retained), "error": str(retained_error)})
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[session_scope] = lambda: db
    app.dependency_overrides[current_user] = lambda: admins[0]
    client = TestClient(app)
    assert client.get(f"/api/imports/{first['id']}/preview").status_code == 200
    assert client.get(f"/api/imports/{second['id']}/preview").status_code == 200
    request = service.create_deletion(db, "email", partner.email, admins[0].id)
    service.approve_deletion(db, request["id"], admins[1].id)
    service.execute_deletion(db, request["id"])
    assert all(not path.exists() for path in paths)
    for imported in (first, second):
        assert client.get(f"/api/imports/{imported['id']}/preview").status_code == 404
        assert client.get(f"/api/imports/{imported['id']}/errors.csv").status_code == 404
        row = db.execute(text("SELECT file_path,error_file_path FROM imports WHERE id=:id"),
                         {"id": imported["id"]}).one()
        assert row == ("", None)
    assert retained.exists() and retained_error.exists()
    assert client.get(f"/api/imports/{retained_id}/preview").status_code == 200


@pytest.mark.parametrize("where", ["upload", "diagnostic"])
def test_unprocessed_other_source_files_are_streamed_for_normalized_identifiers(db, tmp_path, monkeypatch, where):
    admins, source, partner = people(db)
    uploads = tmp_path / "uploads"; uploads.mkdir()
    monkeypatch.setattr(get_settings(), "upload_dir", str(uploads))
    raw = uploads / "not-yet-processed.csv"
    raw.write_text("email\n" + (" ERASE+historic@EXAMPLE.ORG \n" if where == "upload" else "keep@example.org\n"))
    error = uploads / "diagnostics.csv"
    error.write_text("record_number,message\n2," + ("erase@example.org" if where == "diagnostic" else "Clean") + "\n")
    iid = db.scalar(text("""INSERT INTO imports(source_id,filename,file_path,error_file_path,record_type)
      VALUES(:source,'not-yet-processed.csv',:raw,:error,'contact') RETURNING id"""),
      {"source": source.id, "raw": str(raw), "error": str(error)})
    request = service.create_deletion(db, "email", partner.email, admins[0].id)
    service.approve_deletion(db, request["id"], admins[1].id)
    service.execute_deletion(db, request["id"])
    assert not raw.exists() and not error.exists()
    assert db.execute(text("SELECT file_path,error_file_path FROM imports WHERE id=:id"), {"id": iid}).one() == ("", None)


@pytest.mark.parametrize("unsafe", ["outside", "unreadable"])
def test_historical_file_scan_failure_does_not_complete_erasure(db, tmp_path, monkeypatch, unsafe):
    admins, source, partner = people(db)
    uploads = tmp_path / "uploads"; uploads.mkdir()
    monkeypatch.setattr(get_settings(), "upload_dir", str(uploads))
    path = (tmp_path if unsafe == "outside" else uploads) / "retained.csv"
    path.write_text("email\nerase@example.org\n")
    db.execute(text("""INSERT INTO imports(source_id,filename,file_path,record_type)
      VALUES(:source,'retained.csv',:path,'contact')"""), {"source": source.id, "path": str(path)})
    request = service.create_deletion(db, "email", partner.email, admins[0].id)
    service.approve_deletion(db, request["id"], admins[1].id)
    if unsafe == "unreadable":
        import app.imports.service as imports_service
        def denied(*args):
            raise PermissionError("Fixture file is inaccessible")
        monkeypatch.setattr(imports_service, "csv_reader", denied)
    with pytest.raises((ValueError, PermissionError)), db.begin_nested():
        service.execute_deletion(db, request["id"])
    assert path.exists()
    assert db.scalar(text("SELECT status FROM deletion_requests WHERE id=:id"), {"id": request["id"]}) == "approved"
    assert db.scalar(text("SELECT EXISTS(SELECT 1 FROM profiles WHERE id=:id)"), {"id": partner.id})


def test_upload_retention_removes_only_expired_completed_files(db, tmp_path, monkeypatch):
    admins, source, partner = people(db)
    uploads = tmp_path / "uploads"; uploads.mkdir()
    monkeypatch.setattr(get_settings(), "upload_dir", str(uploads))
    monkeypatch.setattr(get_settings(), "upload_retention_days", 7)
    files = {}
    for name, age, state in (("expired", 8, "completed"), ("recent", 1, "completed"), ("running", 8, "running")):
        raw, diagnostic = uploads / f"{name}.csv", uploads / f"{name}-errors.csv"
        raw.write_text("email\nerase@example.org\n")
        diagnostic.write_text("record_number,message\n2,Example\n")
        iid = db.scalar(text("""INSERT INTO imports(source_id,filename,file_path,error_file_path,
          record_type,status,finished_at) VALUES(:source,:name,:raw,:error,'contact',:status,
          now()-(:age * interval '1 day')) RETURNING id"""),
          {"source": source.id, "name": raw.name, "raw": str(raw), "error": str(diagnostic), "status": state, "age": age})
        files[name] = (iid, raw, diagnostic)
    assert service.expire_import_uploads(db) == {"imports_revoked": 1}
    assert not files["expired"][1].exists() and not files["expired"][2].exists()
    assert all(path.exists() for key in ("recent", "running") for path in files[key][1:])
    assert db.execute(text("SELECT file_path,error_file_path FROM imports WHERE id=:id"),
                      {"id": files["expired"][0]}).one() == ("", None)
    assert service.expire_import_uploads(db) == {"imports_revoked": 0}


def test_history_scan_skips_impossible_normalizations(db, tmp_path, monkeypatch):
    admins, source, partner = people(db)
    uploads = tmp_path / "uploads"; uploads.mkdir()
    monkeypatch.setattr(get_settings(), "upload_dir", str(uploads))
    file = uploads / "unrelated.csv"
    file.write_text("first_name,external_id\n" + "Unrelated,123456789\n" * 200)
    db.execute(text("""INSERT INTO imports(source_id,filename,file_path,record_type)
      VALUES(:source,'unrelated.csv',:path,'contact')"""), {"source": source.id, "path": str(file)})
    def unexpected(*args):
        raise AssertionError("Impossible identifier normalization wastes file-scan CPU")
    monkeypatch.setattr(service, "normalize_email", unexpected)
    monkeypatch.setattr(service, "normalize_phone", unexpected)
    service.revoke_import_history(db, [partner.id], {("email", partner.email)})
    assert file.exists()


def test_deletion_reimport_external_only_aliases_and_unaffected_partner(db, tmp_path, monkeypatch):
    from app.identity.bulk import resolve_bulk
    admins, source, p = people(db)
    # Use real importer-managed files inside the configured private upload root.
    uploads = tmp_path / "uploads"; uploads.mkdir()
    source, _ = ingest(db, uploads,
        "external_id,email,phone\nremoved,erase@example.org,+12025550123\nkept,keep@example.org,+12025550125\n", source=source)
    gifts, _ = ingest(db, uploads, "external_id,email,amount,gift_date\ngift-1,erase@example.org,25,2024-01-01\n", "gift")
    events, _ = ingest(db, uploads, "message_id,email,name,occurred_at\nevent-1,erase@example.org,Test,2024-01-01T00:00:00Z\n", "event")
    resolve_bulk(db); db.commit()
    pid = db.scalar(text("SELECT profile_id FROM source_records WHERE external_id='removed'"))
    request = service.create_deletion(db, "email", "erase@example.org", admins[0].id)
    service.approve_deletion(db, request["id"], admins[1].id)
    service.execute_deletion(db, request["id"]); db.commit()
    _, rows = ingest(db, uploads, "external_id,email,phone\nremoved,,\nnew-alias,erase@example.org,\nphone-only,,+12025550123\nkept,keep@example.org,+12025550125\n", source=source)
    _, gift_rows = ingest(db, uploads, "external_id,amount,gift_date\ngift-1,25,2024-01-01\n", "gift", gifts)
    _, event_rows = ingest(db, uploads, "message_id,name,occurred_at\nevent-1,Test,2024-01-01T00:00:00Z\n", "event", events)
    resolve_bulk(db)
    assert {r["external_id"] for r in rows} == {"kept"}
    assert not gift_rows and not event_rows
    assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM profiles WHERE id=:id)"), {"id": pid})
    assert db.scalar(text("SELECT count(*) FROM source_records WHERE external_id='kept'")) == 1
