"""Disposable-schema integration checks; never contacts an SFTP endpoint."""
from sqlalchemy import select, text
from app.activations import api, service, sftp
from app.models import ActivationRun
from test_consent_ingestion_pg import db
from test_activations_pg import setup
from test_activation_sftp import CREDS, Remote


def test_sftp_history_manifest_and_failed_alert(db, tmp_path, monkeypatch):
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    destination = api.create_destination(api.DestinationWrite(
        name="Unit SFTP", type="sftp", config={"output_dir": "/exports", "gzip": True},
        credentials=CREDS), db, admin)
    assert destination["has_credentials"] and "credentials" not in destination
    activation = api.create_activation(api.ActivationWrite(
        name="Unit transfer", segment_id=segment, destination_id=destination["id"],
        required_consent="email", field_mapping={"email": {"kind": "profile", "key": "email"}}), db, analyst)
    remote = Remote()
    original = sftp.publish
    monkeypatch.setattr(sftp, "publish", lambda config, credentials, files, run_id:
                        original(config, credentials, files, run_id, connector=remote.connect, sleep=lambda _: None))
    queued = api.start_run(activation["id"], db, analyst)
    run = service.execute(db, queued["run_id"])
    db.commit()
    assert run.status == "succeeded"
    history = api.runs(activation["id"], db, analyst)["items"][0]
    assert history["manifest"]["files"][0]["row_count"] == run.profiles_sent
    assert history["file_sha256"]
    assert "private_key" not in str(history)
    def fail(*args, **kwargs):
        from app.activations.security import DeliveryError
        raise DeliveryError("SFTP publication failed after three attempts; no confirmed manifest receipt.")
    monkeypatch.setattr(sftp, "publish", fail)
    queued = api.start_run(activation["id"], db, analyst)
    run = service.execute(db, queued["run_id"])
    db.commit()
    assert run.status == "failed"
    assert api.activation_alerts(db, analyst)["items"][0]["run_id"] == run.id


def test_bootstrap_is_additive_and_disabled(db, tmp_path, monkeypatch):
    from app.activations.bootstrap import bootstrap
    from app.models import Activation, Segment
    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    row = db.get(Segment, segment)
    row.name = "All TBN Reference"
    db.flush()
    result = bootstrap(db)
    db.flush()
    assert len(result["created"]) == 2
    for identifier in result["created"]:
        activation = db.get(Activation, identifier)
        assert not activation.is_active and activation.timezone == "America/New_York"
    db.get(Activation, result["created"][0]).name = "Custom untouched"
    db.flush()
    # Existing destinations never get overwritten, even on a later bootstrap.
    db.execute(text("UPDATE destinations SET config='{\"output_dir\":\"/custom\"}'::jsonb WHERE name='CustomerPulse'"))
    bootstrap(db)
    assert db.scalar(text("SELECT config->>'output_dir' FROM destinations WHERE name='CustomerPulse'")) == "/custom"


def test_crosswalk_filtered_bundle_permissions_and_real_privacy_erasure(db, tmp_path, monkeypatch):
    import csv
    import gzip
    import hashlib
    import io
    import json
    from pathlib import Path
    import pytest
    from fastapi import HTTPException
    from app.privacy import service as privacy
    from app.activations.crosswalk import COLUMNS, ALIAS_COLUMNS

    segment, admin, analyst = setup(db, tmp_path, monkeypatch)
    db.execute(text("""INSERT INTO users(id,subject,email,name,role) OVERRIDING SYSTEM VALUE
      VALUES(4,'crosswalk-privacy-admin','privacy-admin@example.test','Privacy approver','admin');
      INSERT INTO identity_xref(master_id,key_type,key_value,source_id)
      SELECT master_id,'donor_account_number','account-'||id,1 FROM profiles;
    """))
    masters = dict(db.execute(text("SELECT id,master_id FROM profiles")).all())
    retired = {}
    for profile_id, master_id in masters.items():
        alias = db.scalar(text("INSERT INTO master_id_registry(master_id) VALUES(kin_new_master_id()) RETURNING master_id"))
        db.execute(text("INSERT INTO master_id_aliases(retired_id,survivor_id) VALUES(:retired,:survivor)"),
                   {"retired": alias, "survivor": master_id})
        retired[profile_id] = alias
    db.commit()

    # An actually erased profile, rather than merely a suppression fixture.
    deleted = privacy.create_deletion(db, "phone", "+12025550123", admin.id)
    privacy.approve_deletion(db, deleted["id"], 4)
    privacy.execute_deletion(db, deleted["id"])
    db.commit()
    assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM profiles WHERE id=6)"))
    # Complete the real post-deletion refresh in this isolated schema before
    # requesting an activation; the production trait barrier must stay enabled.
    from app.traits.engine import recompute_traits
    recompute_traits(db)
    db.execute(text("""UPDATE jobs SET status='succeeded',finished_at=now()
      WHERE type='traits.recompute' AND dedupe_key=:key"""),
      {"key": f"traits:after-deletion:{deleted['id']}"})
    db.commit()

    config = {"output_dir": "/exports", "gzip": True, "export_kind": "crosswalk"}
    with pytest.raises(HTTPException, match="purpose") as denied:
        api.create_destination(api.DestinationWrite(
            name="Unapproved crosswalk", type="sftp", config=config, credentials=CREDS), db, admin)
    assert denied.value.status_code == 422
    config["approved_purpose"] = "Approved identity reconciliation"
    destination = api.create_destination(api.DestinationWrite(
        name="Approved crosswalk", type="sftp", config=config, credentials=CREDS), db, admin)
    ordinary = api.create_destination(api.DestinationWrite(name="Ordinary CSV", type="csv"), db, admin)
    body = dict(name="Daily crosswalk", segment_id=segment, destination_id=destination["id"],
                required_consent="email", field_mapping={"master_id": {"kind": "profile", "key": "master_id"}})
    with pytest.raises(HTTPException) as denied:
        api.create_activation(api.ActivationWrite(**body), db, analyst)
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException, match="consent"):
        api.create_activation(api.ActivationWrite(**dict(body, required_consent=None)), db, admin)
    activation = api.create_activation(api.ActivationWrite(**body), db, admin)
    for patch in ({"name": "Analyst edit"}, {"destination_id": ordinary["id"]}, {"required_consent": None}):
        with pytest.raises(HTTPException) as denied:
            with db.begin_nested():
                api.update_activation(activation["id"], api.ActivationPatch(**patch), db, analyst)
        assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied:
        api.start_run(activation["id"], db, analyst)
    assert denied.value.status_code == 403
    with pytest.raises(ValueError, match="admin"):
        service.enqueue_run(db, activation["id"], analyst.id)
    # Also reject repointing an ordinary activation toward the PII destination.
    regular = api.create_activation(api.ActivationWrite(
        name="Regular", segment_id=segment, destination_id=ordinary["id"]), db, analyst)
    with pytest.raises(HTTPException) as denied:
        with db.begin_nested():
            api.update_activation(regular["id"], api.ActivationPatch(
                destination_id=destination["id"], required_consent="email"), db, analyst)
    assert denied.value.status_code == 403

    remote = Remote()
    original = sftp.publish
    monkeypatch.setattr(sftp, "publish", lambda config, credentials, files, run_id:
                        original(config, credentials, files, run_id, connector=remote.connect, sleep=lambda _: None))
    queued = api.start_run(activation["id"], db, admin)
    run = service.execute(db, queued["run_id"])
    db.commit()
    assert run.status == "succeeded"
    assert run.profiles_selected == 5
    assert run.profiles_excluded_consent == 1
    assert run.profiles_excluded_suppressed == 2
    assert run.profiles_sent == 2
    history = api.runs(activation["id"], db, admin)["items"][0]
    manifest = history["manifest"]
    assert [f["name"] for f in manifest["files"]] == ["crosswalk.csv.gz", "aliases.csv.gz"]
    assert remote.renames[-1].endswith("/manifest.json")
    contents = {}
    for entry in manifest["files"]:
        blob = remote.files[f"/exports/run-{run.id}/{entry['name']}"]
        assert hashlib.sha256(blob).hexdigest() == entry["sha256"]
        assert entry["bytes"] == len(blob)
        reader = csv.DictReader(io.StringIO(gzip.decompress(blob).decode()))
        rows = list(reader)
        assert entry["row_count"] == len(rows) == 2
        assert reader.fieldnames == (COLUMNS if entry["name"].startswith("crosswalk") else ALIAS_COLUMNS)
        contents[entry["name"]] = rows
    assert {r["master_id"] for r in contents["crosswalk.csv.gz"]} == {masters[1], masters[4]}
    assert {r["key_value"] for r in contents["crosswalk.csv.gz"]} == {"account-1", "account-4"}
    assert {r["survivor_id"] for r in contents["aliases.csv.gz"]} == {masters[1], masters[4]}
    assert {r["retired_id"] for r in contents["aliases.csv.gz"]} == {retired[1], retired[4]}
    assert all(r["status"] == "active" and r["updated_at"] for r in contents["crosswalk.csv.gz"])
    audit = db.scalar(text("SELECT details FROM audit_log WHERE action='export.crosswalk' AND entity_id=:id"),
                      {"id": str(run.id)})
    assert audit["fields"] == COLUMNS and audit["alias_fields"] == ALIAS_COLUMNS
    assert "account-1" not in json.dumps(audit) and masters[1] not in json.dumps(audit)
    assert not api.runs(activation["id"], db, analyst)["items"][0]["downloadable"]
    with pytest.raises(HTTPException) as denied:
        api.download(run.id, db, analyst)
    assert denied.value.status_code == 403
    db.commit()

    directory = Path(run.file_path).parent
    assert {p.name for p in directory.iterdir()} == {
        "crosswalk.csv", "crosswalk.csv.gz", "aliases.csv", "aliases.csv.gz"}
    # Exercise the actual deletion service after a successful two-file export.
    request = privacy.create_deletion(db, "email", "first@example.org", admin.id)
    privacy.approve_deletion(db, request["id"], 4)
    privacy.execute_deletion(db, request["id"])
    db.commit()
    assert not list(directory.iterdir())
    db.expire_all()
    erased_run = db.get(ActivationRun, run.id)
    assert erased_run.revoked_at and erased_run.file_path is None and erased_run.snapshot_encrypted is None
    assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM profiles WHERE id=1)"))
    assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM identity_xref WHERE master_id=:id)"), {"id": masters[1]})
    assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM master_id_aliases WHERE survivor_id=:id)"), {"id": masters[1]})
    assert db.scalar(text("SELECT status FROM deletion_requests WHERE id=:id"), {"id": request["id"]}) == "completed"
