"""Real migrated PostgreSQL destination, funnel, delta and authorization tests."""
import csv
import hashlib
import hmac
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text, select
from app.auth.deps import current_user
from app.db import session_scope
from app.config import get_settings
from app.models import Activation, ActivationRun, Destination, Segment
from app.activations import api, fields, service, security
from app.activations.schedule import tick_activations
from test_consent_ingestion_pg import db
from test_activations import echo


def setup(db, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "export_dir", str(tmp_path / "exports"))
    db.execute(text("""INSERT INTO users(id,subject,email,name,role) OVERRIDING SYSTEM VALUE
      VALUES(1,'activation-admin','admin@example.test','Synthetic admin','admin'),
      (2,'activation-analyst','analyst@example.test','Synthetic analyst','analyst'),
      (3,'activation-viewer','viewer@example.test','Synthetic viewer','viewer');
      INSERT INTO sources(id,key,name,kind,record_types,priority) OVERRIDING SYSTEM VALUE
        VALUES(1,'activation-test','Test source','csv','{contact}',10);
      INSERT INTO profiles(id,region,email,phone,first_name,last_name,country,postal_code)
      OVERRIDING SYSTEM VALUE VALUES
        (1,'TX','first@example.org',NULL,'Zoë','Pérez-O''Neil','US','90210'),
        (2,'TX','second@example.org',NULL,'Second','Partner','US','90210'),
        (3,'TX','third@example.org',NULL,'Third','Partner','US','90210'),
        (4,'TX',NULL,NULL,'No','Contact','US','90210'),
        (5,'TX','fifth@example.org',NULL,'Fifth','Partner','US','90210'),
        (6,'TX',NULL,'+12025550123','Phone','Partner','US','90210');
      INSERT INTO consents(profile_id,channel,status,source_id)
        SELECT p.id,c.channel,CASE WHEN p.id=2 THEN 'opted_out' ELSE 'opted_in' END,1
        FROM profiles p CROSS JOIN (VALUES('email'),('ads_personalization')) c(channel);
    """))
    for address, reason in (("third@example.org", "manual"), ("fifth@example.org", "deletion_request")):
        digest = hmac.new(get_settings().pii_hash_pepper.encode(), address.encode(), hashlib.sha256).hexdigest()
        db.execute(text("INSERT INTO suppressions(type,value_hash,reason) VALUES('email',:hash,:reason)"),
                   {"hash": digest, "reason": reason})
    segment = Segment(name="All TX partners", status="active", definition={"version": 1, "root": {
        "op": "and", "rules": [{"type": "profile", "field": "region", "operator": "eq", "value": "TX"}]}})
    db.add(segment)
    db.flush()
    return segment.id, SimpleNamespace(id=1, role="admin"), SimpleNamespace(id=2, role="analyst")


def make(db, segment_id, admin, analyst, kind, echo=None):
    config = {"include_pii": True}
    extra = {}
    if kind == "webhook":
        config.update(url=echo["url"], batch_size=1, mode="delta", allow_internal_network=True)
        extra = {"signing_secret": "synthetic-signing-secret-long", "headers": {"Authorization": "synthetic-header-secret"}}
    dest = api.create_destination(api.DestinationWrite(name=f"Test {kind}", type=kind, config=config, **extra), db, admin)
    mapping = service.preset(kind)
    consent = "ads_personalization" if kind in service.ADS else "email"
    if kind not in service.ADS:
        mapping["email"]["required"] = True
    activation = api.create_activation(api.ActivationWrite(name=f"Run {kind}", segment_id=segment_id,
        destination_id=dest["id"], field_mapping=mapping, required_consent=consent), db, analyst)
    return activation, dest


@pytest.mark.parametrize("kind", ["csv", "google_customer_match", "meta_custom_audience", "webhook"])
def test_real_run_each_destination_funnel_and_encryption(db, tmp_path, monkeypatch, echo, kind):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    activation, destination = make(db, segment, admin, analyst, kind, echo)
    queued = api.start_run(activation["id"], db, analyst)
    run = service.execute(db, queued["run_id"])
    db.commit()
    assert run.status == "succeeded" and run.profiles_selected == 6
    assert run.profiles_excluded_consent == 1
    assert run.profiles_excluded_suppressed == (1 if kind in service.ADS else 2)
    assert run.profiles_excluded_missing_fields == (1 if kind in service.ADS else 2)
    assert run.profiles_sent == (3 if kind in service.ADS else 1)
    assert db.scalar(text("SELECT count(*) FROM audit_log WHERE action='activation.run'")) == 1
    assert db.scalar(text("SELECT count(*) FROM activation_run_members WHERE run_id=:id"), {"id": run.id}) == run.profiles_sent
    assert b"first@example.org" not in run.snapshot_encrypted
    if kind == "webhook":
        raw = db.get(Destination, destination["id"]).secret_encrypted
        assert b"synthetic-signing" not in raw and b"synthetic-header" not in raw
        assert destination["secret_last4"] == "long" and destination["header_names"] == ["Authorization"]
        assert "synthetic" not in json.dumps(destination, default=str)
        headers, body = echo["requests"][0]
        assert json.loads(body)["added"][0]["profile_id"] == 1
        assert headers["X-AH-Signature"] == "sha256=" + hmac.new(
            b"synthetic-signing-secret-long", body, hashlib.sha256).hexdigest()
    else:
        path = service.safe_file(run)
        rows = list(csv.reader(path.open()))
        assert len(rows) == run.profiles_sent + 1
        if kind == "google_customer_match":
            assert rows[0] == ["Email", "Phone", "First Name", "Last Name", "Country", "Zip"]
            assert rows[1][0] == hashlib.sha256(b"first@example.org").hexdigest()
            assert rows[1][-2:] == ["US", "90210"]
        elif kind == "meta_custom_audience":
            assert rows[0][-1] == "external_id"
            assert rows[1][-1] == hashlib.sha256(b"1").hexdigest()
        else:
            assert rows[1][1] == "first@example.org"
    # Re-executing a completed run does not produce another output or audit.
    assert service.execute(db, run.id).status == "succeeded"
    assert db.scalar(text("SELECT count(*) FROM audit_log WHERE action='activation.run'")) == 1


def test_delta_two_runs_failure_preserves_baseline_and_snapshot(db, tmp_path, monkeypatch, echo):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    activation, destination = make(db, segment, admin, analyst, "webhook", echo)
    first, _ = service.enqueue_run(db, activation["id"], analyst.id)
    db.commit()
    service.execute(db, first.id)
    db.commit()
    assert db.execute(text("SELECT profile_id FROM activation_last_members")).scalars().all() == [1]
    db.execute(text("UPDATE profiles SET region='CA' WHERE id=1"))
    db.execute(text("UPDATE consents SET status='opted_in' WHERE profile_id=2"))
    second, _ = service.enqueue_run(db, activation["id"], analyst.id)
    # Edits after enqueue must not change the frozen signing/config/mapping.
    db.get(Destination, destination["id"]).config = {"url": "http://127.0.0.1:1", "allow_internal_network": True}
    db.commit()
    echo["requests"].clear()
    service.execute(db, second.id)
    db.commit()
    payloads = [json.loads(body) for _, body in echo["requests"]]
    assert [p for body in payloads for p in body["added"]] == [{
        "profile_id": 2, "email": "second@example.org", "first_name": "Second", "last_name": "Partner"}]
    assert [p for body in payloads for p in body["removed"]] == [{"profile_id": 1}]
    assert db.execute(text("SELECT profile_id FROM activation_last_members")).scalars().all() == [2]
    db.get(Destination, destination["id"]).config = {
        "url": echo["url"], "allow_internal_network": True, "mode": "delta", "batch_size": 1}
    db.execute(text("UPDATE profiles SET region='TX' WHERE id=1"))
    db.execute(text("UPDATE profiles SET region='CA' WHERE id=2"))
    third, _ = service.enqueue_run(db, activation["id"], analyst.id)
    db.commit()
    echo["statuses"] = [200, 400]
    service.execute(db, third.id)
    db.commit()
    assert db.get(ActivationRun, third.id).status == "failed"
    assert db.get(ActivationRun, third.id).profiles_sent == 1
    assert db.execute(text("SELECT profile_id FROM activation_last_members")).scalars().all() == [2]


def test_roles_ads_override_download_audit_and_retention(db, tmp_path, monkeypatch):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    activation, destination = make(db, segment, admin, analyst, "google_customer_match")
    app = FastAPI()
    app.include_router(api.router)
    user = {"value": SimpleNamespace(id=3, role="viewer")}
    app.dependency_overrides[current_user] = lambda: user["value"]
    app.dependency_overrides[session_scope] = lambda: db
    client = TestClient(app)
    for path in ("/api/destinations", "/api/activations", "/api/activations/fields"):
        assert client.get(path).status_code == 403
    user["value"] = analyst
    assert client.post("/api/destinations", json={"name": "No", "type": "csv"}).status_code == 403
    bad = {"name": "Unsafe", "segment_id": segment, "destination_id": destination["id"],
           "required_consent": None, "consent_override": True, "override_note": "Not allowed"}
    assert client.post("/api/activations", json=bad).status_code == 403
    user["value"] = admin
    assert client.patch(f"/api/destinations/{destination['id']}", json={"type": "csv"}).status_code == 422
    overridden = client.post("/api/activations", json=bad)
    assert overridden.status_code == 200
    assert db.scalar(text("SELECT count(*) FROM audit_log WHERE action='activation.consent_override'")) == 1
    overridden_id = overridden.json()["id"]
    user["value"] = analyst
    assert client.patch(f"/api/activations/{overridden_id}", json={"is_active": False}).status_code == 200
    assert client.patch(f"/api/activations/{overridden_id}", json={**bad, "is_active": True,
        "consent_override": False, "override_note": None}).status_code == 200
    assert client.patch(f"/api/activations/{overridden_id}", json={"required_consent": "email"}).status_code == 403
    db.rollback()
    queued = api.start_run(activation["id"], db, analyst)
    service.execute(db, queued["run_id"])
    db.commit()
    user["value"] = analyst
    response = client.get(f"/api/activation-runs/{queued['run_id']}/download")
    assert response.status_code == 200 and response.text.startswith("Email,Phone,")
    assert db.scalar(text("SELECT count(*) FROM audit_log WHERE action='export.download'")) == 1
    user["value"] = SimpleNamespace(id=3, role="viewer")
    assert client.get(f"/api/activation-runs/{queued['run_id']}/download").status_code == 403
    run = db.get(ActivationRun, queued["run_id"])
    path = Path(run.file_path)
    run.finished_at = datetime.now(timezone.utc) - timedelta(days=100)
    db.commit()
    user["value"] = analyst
    assert client.get(f"/api/activation-runs/{queued['run_id']}/download").status_code == 410
    service.expire_exports(db)
    db.commit()
    assert not path.exists() and run.file_path is None and run.revoked_at is not None


def test_mapping_whitelist_pii_licensing_and_csv_formula(db, tmp_path, monkeypatch):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        fields.validate_mapping(db, {"bad": {"kind": "profile", "key": "email; DROP TABLE profiles"}})
    with pytest.raises(ValueError):
        fields.validate_mapping(db, {"email": {"kind": "profile", "key": "email"}}, False)
    with pytest.raises(ValueError):
        fields.validate_mapping(db, {"constant": {"kind": "constant", "value": "possible PII"}}, False)
    db.execute(text("""INSERT INTO sources(id,key,name,kind,record_types,settings) OVERRIDING SYSTEM VALUE
        VALUES(2,'licensed','Licensed','enrichment','{enrichment}',jsonb_build_object('exportable',false));
      INSERT INTO enrichment_attributes(source_id,key,label,data_type) VALUES(2,'income','Income','text')"""))
    mapping = {"income": {"kind": "enrichment", "source": "licensed", "key": "income"}}
    with pytest.raises(ValueError, match="unlicensed"):
        fields.validate_mapping(db, mapping)
    db.execute(text("UPDATE sources SET settings='{}' WHERE id=2"))
    assert fields.validate_mapping(db, mapping)
    db.execute(text("UPDATE sources SET license_expires_at=current_date-1 WHERE id=2"))
    with pytest.raises(ValueError):
        fields.validate_mapping(db, mapping)
    destination = api.create_destination(api.DestinationWrite(name="Safe CSV", type="csv"), db, admin)
    activation = api.create_activation(api.ActivationWrite(name="Constant", segment_id=segment,
        destination_id=destination["id"], field_mapping={"=header": {"kind": "constant", "value": "=1+1"}}), db, analyst)
    run, _ = service.enqueue_run(db, activation["id"])
    db.commit()
    service.execute(db, run.id)
    db.commit()
    rows = list(csv.reader(Path(run.file_path).open()))
    assert rows[0] == ["'=header"] and rows[1] == ["'=1+1"]


def test_scheduled_dedupe_and_missing_traits_barrier(db, tmp_path, monkeypatch):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    activation, _ = make(db, segment, admin, analyst, "csv")
    row = db.get(Activation, activation["id"])
    row.schedule = "30 1 * * *"
    row.timezone = "America/Chicago"
    db.commit()
    # Both occurrences of the autumn repeated wall minute share one durable key.
    tick_activations(db, datetime(2024, 11, 3, 6, 30, tzinfo=timezone.utc))
    db.flush()
    tick_activations(db, datetime(2024, 11, 3, 7, 30, tzinfo=timezone.utc))
    db.commit()
    assert db.scalar(text("SELECT count(*) FROM activation_runs")) == 1
    assert db.scalar(text("SELECT count(*) FROM scheduled_runs WHERE task LIKE 'activation.cron:%'")) == 1
    from app.jobs.handlers import SegmentTraitsRefreshRequired
    db.execute(text("INSERT INTO jobs(type,payload,status) VALUES('traits.recompute','{\"mode\":\"full\"}','failed')"))
    with pytest.raises(SegmentTraitsRefreshRequired):
        service.trait_barrier(db)


def test_write_ahead_erasure_index_and_filename_survive_worker_rollback(db, tmp_path, monkeypatch):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    activation, _ = make(db, segment, admin, analyst, "csv")
    run, _ = service.enqueue_run(db, activation["id"])
    run_id = run.id
    planned = Path(run.file_path)
    db.commit()
    assert not planned.exists()
    service.execute(db, run_id)
    assert planned.exists()
    db.rollback()  # simulate loss of the execution transaction after writing bytes
    persisted = db.get(ActivationRun, run_id)
    assert Path(persisted.file_path) == planned and persisted.status == "queued"
    assert db.scalars(text("SELECT profile_id FROM activation_run_members WHERE run_id=:id"),
                      {"id": run_id}).all() == [1]
    with pytest.raises(ValueError):
        service.safe_file(persisted)  # never downloadable while not succeeded


def test_queued_run_cancelled_if_destination_deactivated(db, tmp_path, monkeypatch):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    activation, destination = make(db, segment, admin, analyst, "csv")
    run, _ = service.enqueue_run(db, activation["id"])
    db.commit()
    db.get(Destination, destination["id"]).is_active = False
    db.commit()
    assert service.execute(db, run.id).status == "cancelled"
    assert not Path(run.file_path).exists()


def test_real_job_dispatch_and_column_order(db, tmp_path, monkeypatch):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    destination = api.create_destination(api.DestinationWrite(name="Ordered CSV", type="csv"), db, admin)
    activation = api.create_activation(api.ActivationWrite(name="Ordered columns", segment_id=segment,
        destination_id=destination["id"], field_mapping={
            "Z first": {"kind": "profile", "key": "id"},
            "A second": {"kind": "trait", "key": "gift_count_total"}}), db, analyst)
    queued = api.start_run(activation["id"], db, analyst)
    from app import db as app_db
    from app.jobs.handlers import run
    monkeypatch.setattr(app_db, "engine", db.get_bind())
    run("activation.run", {"run_id": queued["run_id"]}, queued["job_id"])
    db.expire_all()
    record = db.get(ActivationRun, queued["run_id"])
    assert record.status == "succeeded"
    with Path(record.file_path).open() as stream:
        assert next(csv.reader(stream)) == ["Z first", "A second"]
    assert list(api.get_activation(activation["id"], db, analyst)["field_mapping"]) == ["Z first", "A second"]


def test_meta_external_id_is_fixed_and_missing_mapping_uses_preset(db, tmp_path, monkeypatch):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    destination = api.create_destination(api.DestinationWrite(
        name="Fixed Meta identity", type="meta_custom_audience"), db, admin)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[current_user] = lambda: analyst
    app.dependency_overrides[session_scope] = lambda: db
    client = TestClient(app)
    # An omitted mapping retains the documented preset behavior.
    created = client.post("/api/activations", json={
        "segment_id": segment, "destination_id": destination["id"], "name": "Meta preset"})
    assert created.status_code == 200
    activation = created.json()
    preset = service.preset("meta_custom_audience")
    assert activation["field_mapping"]["external_id"] == {"kind": "profile", "key": "id"}
    invalid = [
        {**preset, "external_id": {"kind": "profile", "key": "email"}},
        {**preset, "external_id": {"kind": "constant", "value": "different-id"}},
        {**preset, "external_id": {}},
        {**preset, "external_id": None},
        {key: value for key, value in preset.items() if key != "external_id"},
        {}, None,
    ]
    for mapping in invalid:
        response = client.patch(f"/api/activations/{activation['id']}",
                                json={"field_mapping": mapping})
        assert response.status_code == 422
        expected = "cannot be null" if mapping is None else "fixed to the internal profile ID"
        assert expected in response.json()["detail"]
        db.rollback()
    accepted = client.patch(f"/api/activations/{activation['id']}", json={"field_mapping": preset})
    assert accepted.status_code == 200
    with pytest.raises(ValueError, match="fixed to the internal profile ID"):
        service.validate_provider_mapping("meta_custom_audience", invalid[0])


@pytest.mark.parametrize("kind,column", [("google_customer_match", "First Name"), ("meta_custom_audience", "fn")])
def test_other_provider_fields_honor_custom_mapping(db, tmp_path, monkeypatch, kind, column):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    activation, _ = make(db, segment, admin, analyst, kind)
    mapping = service.preset(kind)
    mapping[column] = {"kind": "profile", "key": "last_name"}
    api.update_activation(activation["id"], api.ActivationPatch(field_mapping=mapping), db, analyst)
    queued = api.start_run(activation["id"], db, analyst)
    run = service.execute(db, queued["run_id"])
    db.commit()
    with service.safe_file(run).open() as stream:
        rows = list(csv.reader(stream))
    assert rows[1][rows[0].index(column)] == hashlib.sha256("pérezoneil".encode()).hexdigest()
