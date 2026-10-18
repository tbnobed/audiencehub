"""Step 2 export boundary regression tests, only in disposable migrated schemas."""
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.activations import api, fields, security, service
from app.auth.deps import current_user
from app.db import session_scope
from app.models import Activation, ActivationRun
from test_consent_ingestion_pg import db
from test_activations_pg import setup
from test_activations import echo


def seeded(db, tmp_path, monkeypatch):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    db.execute(text("""
      INSERT INTO source_records(source_id,external_id,profile_id,attributes,raw_hash)
      VALUES(1,'canonical-export',1,
        jsonb_build_object('inmate',true,'donor_status','imported-only','rfm_rfm_score',123), 'test-only');
      INSERT INTO profile_traits(profile_id,donor_status) VALUES(1,'computed-only');
      INSERT INTO sources(id,key,name,kind,record_types) OVERRIDING SYSTEM VALUE
        VALUES(2,'vendor-export','Licensed vendor','enrichment','{enrichment}');
      INSERT INTO enrichment_attributes(source_id,key,label,data_type)
        SELECT 2,k,k,'text' FROM unnest(ARRAY['religion','politicalParty','military_status','custom_private','income']) k;
      INSERT INTO enrichment_values(profile_id,source_id,attribute_key,value_text,vendor,license_id,append_date,sensitive)
        SELECT 1,2,k,'sensitive-test-value','test-vendor','test-license',current_date,k='custom_private'
        FROM unnest(ARRAY['religion','politicalParty','military_status','custom_private','income']) k;
    """))
    destination = api.create_destination(api.DestinationWrite(name="Private CSV", type="csv"), db, admin)
    mapping = {
        "imported": {"kind": "canonical", "key": "donor_status"},
        "computed": {"kind": "trait", "key": "donor_status"},
        "inmate": {"kind": "canonical", "key": "inmate"},
        "religion": {"kind": "enrichment", "source": "vendor-export", "key": "religion"},
        "email": {"kind": "profile", "key": "email", "required": True},
    }
    body = dict(name="Sensitive test", segment_id=segment, destination_id=destination["id"],
                field_mapping=mapping, required_consent="email")
    return admin, analyst, body


def approve(db, admin, body, **extra):
    return api.create_activation(api.ActivationWrite(**body, sensitive_fields=[
        "canonical:inmate", "enrichment:vendor-export:religion"], **extra), db, admin)


def client_for(db, user):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[session_scope] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def test_sensitive_registry_and_all_attributes_default_exclusion(db, tmp_path, monkeypatch):
    admin, analyst, body = seeded(db, tmp_path, monkeypatch)
    registry = fields.registry(db)
    private = {fields.field_reference(f) for f in registry if f["sensitive"]}
    assert {"canonical:inmate", "enrichment:vendor-export:religion",
            "enrichment:vendor-export:politicalParty", "enrichment:vendor-export:military_status",
            "enrichment:vendor-export:custom_private"} <= private
    assert not any(f["sensitive"] for f in fields.registry(db, "viewer"))
    assert not private.intersection(fields.all_attributes(db))
    assert "enrichment:vendor-export:income" in fields.all_attributes(db)
    with pytest.raises(ValueError, match="explicit admin"):
        fields.validate_mapping(db, body["field_mapping"])
    # Metadata is authoritative, never the client-supplied mapping flag.
    forged = {"inmate": {"kind": "canonical", "key": "inmate", "sensitive": False}}
    with pytest.raises(ValueError):
        fields.validate_mapping(db, forged)


@pytest.mark.parametrize("role,explicit", [("analyst", False), ("analyst", True), ("admin", False)])
def test_mapping_alone_cannot_approve_sensitive_exports(db, tmp_path, monkeypatch, role, explicit):
    admin, analyst, body = seeded(db, tmp_path, monkeypatch)
    user = admin if role == "admin" else analyst
    if explicit:
        body["sensitive_fields"] = ["canonical:inmate", "enrichment:vendor-export:religion"]
    with pytest.raises(HTTPException) as error:
        api.create_activation(api.ActivationWrite(**body), db, user)
    assert error.value.status_code in (403, 422)
    db.rollback()
    assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM audit_log WHERE action='activation.sensitive_approval')"))


def test_admin_explicit_export_canonical_separation_and_audited_download(db, tmp_path, monkeypatch):
    admin, analyst, body = seeded(db, tmp_path, monkeypatch)
    activation = approve(db, admin, body)
    queued = api.start_run(activation["id"], db, analyst)
    run = service.execute(db, queued["run_id"])
    db.commit()
    assert run.status == "succeeded" and run.profiles_sent == 1
    rows = list(csv.DictReader(Path(run.file_path).open()))
    assert rows[0]["imported"] == "imported-only"
    assert rows[0]["computed"] == "computed-only"
    assert rows[0]["inmate"] == "True"
    assert rows[0]["master_id"].startswith("kin_")
    snapshot = security.decrypt(run.snapshot_encrypted)
    assert snapshot["sensitive_fields"] == ["canonical:inmate", "enrichment:vendor-export:religion"]
    assert b"sensitive-test-value" not in run.snapshot_encrypted
    assert client_for(db, analyst).get(f"/api/activation-runs/{run.id}/download").status_code == 403
    viewer = SimpleNamespace(id=3, role="viewer")
    assert client_for(db, viewer).get(f"/api/activation-runs/{run.id}/download").status_code == 403
    response = client_for(db, admin).get(f"/api/activation-runs/{run.id}/download")
    assert response.status_code == 200
    details = db.execute(text("SELECT action,details FROM audit_log WHERE action LIKE '%sensitive%'")).all()
    assert {row.action for row in details} == {
        "activation.sensitive_approval", "export.sensitive", "export.sensitive_download"}
    assert all(row.details["fields"] == snapshot["sensitive_fields"] for row in details)
    assert "sensitive-test-value" not in json.dumps([row.details for row in details])
    assert "imported-only" not in json.dumps([row.details for row in details])
    assert not api.run_json(run, db, analyst)["downloadable"]
    assert api.run_json(run, db, admin)["downloadable"]


@pytest.mark.parametrize("tamper", ["actor_role", "actor_inactive", "snapshot_fields", "snapshot_mapping", "approval_actor"])
def test_queued_execution_rechecks_stored_admin_approval(db, tmp_path, monkeypatch, tamper):
    admin, analyst, body = seeded(db, tmp_path, monkeypatch)
    activation = approve(db, admin, body)
    run, _ = service.enqueue_run(db, activation["id"], analyst.id, triggered_by="scheduled")
    db.commit()
    snapshot = security.decrypt(run.snapshot_encrypted)
    if tamper == "actor_role":
        db.execute(text("UPDATE users SET role='analyst' WHERE id=1"))
    elif tamper == "actor_inactive":
        db.execute(text("UPDATE users SET is_active=false WHERE id=1"))
    elif tamper == "approval_actor":
        db.execute(text("UPDATE audit_log SET user_id=2 WHERE id=:id"), {"id": snapshot["sensitive_approval_id"]})
    elif tamper == "snapshot_fields":
        snapshot["sensitive_fields"] = []
        run.snapshot_encrypted = security.encrypt(snapshot)
    else:
        snapshot["mapping"]["extra"] = {"kind": "enrichment", "source": "vendor-export", "key": "custom_private"}
        run.snapshot_encrypted = security.encrypt(snapshot)
    db.commit()
    with pytest.raises(ValueError, match="approval"):
        service.execute(db, run.id)
    assert not Path(run.file_path).exists()
    assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM audit_log WHERE action='export.sensitive')"))


def test_analyst_cannot_expand_previously_approved_mapping(db, tmp_path, monkeypatch):
    admin, analyst, body = seeded(db, tmp_path, monkeypatch)
    activation = approve(db, admin, body)
    mapping = dict(body["field_mapping"], custom={
        "kind": "enrichment", "source": "vendor-export", "key": "custom_private"})
    with pytest.raises(HTTPException):
        api.update_activation(activation["id"], api.ActivationPatch(field_mapping=mapping), db, analyst)
    db.rollback()
    # Forging the stored activation directly still cannot bypass enqueue.
    db.get(Activation, activation["id"]).field_mapping = mapping
    db.flush()
    with pytest.raises(ValueError, match="approval"):
        service.enqueue_run(db, activation["id"], analyst.id)


def test_sensitive_download_rechecks_admin_approval_actor(db, tmp_path, monkeypatch):
    admin, analyst, body = seeded(db, tmp_path, monkeypatch)
    activation = approve(db, admin, body)
    queued = api.start_run(activation["id"], db, analyst)
    service.execute(db, queued["run_id"])
    db.commit()
    db.execute(text("UPDATE users SET is_active=false WHERE id=1"))
    db.commit()
    assert client_for(db, admin).get(f"/api/activation-runs/{queued['run_id']}/download").status_code == 403


@pytest.mark.parametrize("status", [200, 400])
def test_sensitive_webhook_delivery_audited_even_on_failure(db, tmp_path, monkeypatch, echo, status):
    admin, analyst, body = seeded(db, tmp_path, monkeypatch)
    destination = api.create_destination(api.DestinationWrite(
        name="Sensitive webhook", type="webhook",
        config={"url": echo["url"], "allow_internal_network": True, "batch_size": 1},
        signing_secret="synthetic-signing-secret-long"), db, admin)
    body["destination_id"] = destination["id"]
    activation = approve(db, admin, body)
    run, _ = service.enqueue_run(db, activation["id"], triggered_by="scheduled")
    db.commit()
    echo["statuses"] = [status]
    service.execute(db, run.id)
    db.commit()
    assert run.status == ("succeeded" if status == 200 else "failed")
    assert echo["requests"]
    payload = json.loads(echo["requests"][0][1])
    assert payload["added"][0]["imported"] == "imported-only"
    assert payload["added"][0]["religion"] == "sensitive-test-value"
    audit = db.scalar(text("SELECT details FROM audit_log WHERE action='export.sensitive'"))
    assert audit["fields"] == ["canonical:inmate", "enrichment:vendor-export:religion"]
    assert "sensitive-test-value" not in json.dumps(audit)
