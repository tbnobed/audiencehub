"""Master ID surface contracts, SFTP safety and resumability; no live credentials."""
import base64
import csv
import hashlib
import io
import socket
import stat
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.activations import fields, security, service
from app.identity import filedrop
from test_consent_ingestion_pg import db

MASTER = "kin_01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.fixture
def config():
    return {"host": "partner.example", "username": "lookup-service",
            "private_key": "test-only-not-a-real-key",
            "host_key_sha256": "SHA256:" + "A" * 43,
            "input_dir": "/incoming", "output_dir": "/results"}


class Remote:
    def __init__(self, files):
        self.files = dict(files)
        self.renames = []
        self.permissions = {}
        self.fail_write = False

    def listdir_attr(self, path):
        return [SimpleNamespace(filename=name.rsplit("/", 1)[1])
                for name in self.files if name.startswith(path + "/")]

    def lstat(self, path):
        if path not in self.files:
            raise FileNotFoundError()
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=len(self.files[path]))

    def open(self, path, mode):
        if mode == "rb":
            return io.BytesIO(self.files[path])
        assert mode == "wx" and path not in self.files
        remote = self
        class Upload(io.BytesIO):
            def write(self, content):
                if remote.fail_write:
                    raise OSError("secret input that must not appear")
                return super().write(content)
            def close(self):
                if not self.closed:
                    remote.files[path] = self.getvalue()
                super().close()
        self.files[path] = b""
        return Upload()

    def chmod(self, path, mode):
        self.permissions[path] = mode

    def rename(self, old, new):
        assert old.endswith(".tmp") and new.endswith(".lookup.csv")
        assert new not in self.files
        self.renames.append((old, new))
        self.files[new] = self.files.pop(old)

    def remove(self, path):
        del self.files[path]


@pytest.mark.parametrize("column,key", list(filedrop.KEY_COLUMNS.items()))
def test_csv_three_lookup_flows_unknown_and_ambiguity(column, key):
    def lookup(db, items):
        assert items == [{"key_type": key, "value": value} for value in ("known", "unknown", "shared")]
        return [{"status": "matched", "master_id": MASTER},
                {"status": "unknown", "master_id": None},
                {"status": "ambiguous", "master_id": None}]
    output = filedrop.lookup_csv(None, f"{column}\nknown\nunknown\nshared\n".encode(), lookup)
    rows = list(csv.DictReader(io.StringIO(output.decode())))
    assert rows[0] == {"key_type": key, "key_value": "known", "master_id": MASTER, "status": "matched"}
    assert rows[1]["status"] == "unknown" and rows[1]["master_id"] == ""
    assert rows[2]["status"] == "ambiguous" and rows[2]["master_id"] == ""


@pytest.mark.parametrize("content", [b"email\nsecret@example.com\n", b"hubspot_record_id,other\n1,2\n",
                                     b"hubspot_record_id\n1,2\n", b"hubspot_record_id\n\xff\n",
                                     b"hubspot_record_id\n" + b"x\n" * 10001])
def test_csv_invalid_inputs_are_explicit_without_pii(content):
    lookup = MagicMock()
    with pytest.raises(filedrop.FiledropError) as error:
        filedrop.lookup_csv(None, content, lookup)
    assert "secret" not in str(error.value)
    lookup.assert_not_called()


def test_remote_poll_atomic_idempotent_and_changed_input(config, caplog):
    remote = Remote({"/incoming/hubspot.csv": b"hubspot_record_ids\n123\n"})
    lookup = MagicMock(return_value=[{"status": "matched", "master_id": MASTER}])
    assert filedrop.poll(None, config, sftp=remote, lookup=lookup) == {"processed": 1, "skipped": 0}
    assert len(remote.renames) == 1
    assert filedrop.poll(None, config, sftp=remote, lookup=lookup) == {"processed": 0, "skipped": 1}
    assert lookup.call_count == 1
    assert set(remote.permissions.values()) == {0o600}
    remote.files["/incoming/hubspot.csv"] = b"hubspot_record_ids\n456\n"
    assert filedrop.poll(None, config, sftp=remote, lookup=lookup)["processed"] == 1
    assert len(remote.renames) == 2
    assert not caplog.text


def test_remote_write_failure_no_partial_publication(config):
    remote = Remote({"/incoming/okta.csv": b"okta_customer_id\nsensitive\n"})
    remote.fail_write = True
    with pytest.raises(filedrop.FiledropError) as error:
        filedrop.poll(None, config, sftp=remote, lookup=lambda *_: [{"status": "unknown"}])
    assert "secret" not in str(error.value) and "sensitive" not in str(error.value)
    assert not remote.renames and not any(name.startswith("/results/") for name in remote.files)


def test_config_encrypted_only_and_host_key(config, tmp_path):
    path = tmp_path / "lookup.fernet"
    path.write_bytes(security.encrypt(config))
    assert filedrop.load_config(path) == config
    assert config["private_key"].encode() not in path.read_bytes()
    path.write_text('{"private_key":"should-never-be-read-as-plaintext"}')
    with pytest.raises(filedrop.FiledropError):
        filedrop.load_config(path)
    key = SimpleNamespace(asbytes=lambda: b"test-server-key")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    filedrop.verify_host_key(key, fingerprint)
    with pytest.raises(filedrop.FiledropError):
        filedrop.verify_host_key(key, config["host_key_sha256"])


def test_core_lookup_interface_not_found_is_explicit_unknown(monkeypatch):
    from app.identity import master
    lookup = MagicMock(return_value=[{"status": "not_found", "master_id": None}])
    monkeypatch.setattr(master, "lookup_bulk", lookup)
    result = filedrop.lookup_csv(None, b"donor_account_number\n123\n")
    assert list(csv.DictReader(io.StringIO(result.decode())))[0]["status"] == "unknown"
    lookup.assert_called_once_with(None, [{"key_type": "donor_account_number", "value": "123"}])


def test_sftp_ssrf_blocks_mixed_dns_without_connecting(config, monkeypatch):
    monkeypatch.setitem(sys.modules, "paramiko", SimpleNamespace())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 22)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 22))])
    connect = MagicMock()
    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(filedrop.FiledropError):
        with filedrop.connect(config):
            pass
    connect.assert_not_called()


def test_transport_pins_public_ip_checks_host_before_auth(config, monkeypatch):
    events = []
    transport = MagicMock()
    transport.start_client.side_effect = lambda **kw: events.append("start")
    transport.get_remote_server_key.return_value = SimpleNamespace(asbytes=lambda: b"wrong")
    transport.auth_publickey.side_effect = lambda *a: events.append("auth")
    fake_paramiko = SimpleNamespace(Transport=lambda sock: transport)
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(security, "target", lambda url: (None, "partner.example", 22, "8.8.8.8"))
    sock = MagicMock()
    connection = MagicMock(return_value=sock)
    monkeypatch.setattr(socket, "create_connection", connection)
    with pytest.raises(filedrop.FiledropError, match="host-key"):
        with filedrop.connect(config):
            pass
    connection.assert_called_once_with(("8.8.8.8", 22), timeout=10)
    assert events == ["start"]
    transport.close.assert_called_once()
    sock.close.assert_called_once()


def test_master_export_mapping_cannot_override_and_ads_unchanged():
    assert "master_id" in fields.PROFILE_FIELDS and "master_id" not in fields.PII_FIELDS
    mapping = {"master_id": {"kind": "constant", "value": "incorrect"}, "profile_id": {"kind": "profile", "key": "id"}}
    assert service.export_mapping("csv", mapping)["master_id"] == {
        "kind": "profile", "key": "master_id", "required": True}
    for kind in service.ADS:
        assert "master_id" not in service.preset(kind)
        assert service.export_mapping(kind, mapping) is mapping


@pytest.mark.parametrize("kind", ["csv", "google_customer_match", "meta_custom_audience"])
def test_actual_csv_has_master_id_except_ads(kind, tmp_path, monkeypatch):
    row = {"id": 1, "master_id": MASTER, "email": "test@example.com", "first_name": "Test",
           "last_name": "Person", "country": "US", "postal_code": "90210"}
    monkeypatch.setattr(service, "_rows", lambda *a: [[row]])
    monkeypatch.setattr(fields, "enrichments", lambda *a: {})
    monkeypatch.setattr(service, "run_directory", lambda *a: tmp_path)
    run = SimpleNamespace(id=1, file_path=str(tmp_path / "audience.csv"), profiles_sent=0)
    service.export_csv(None, run, {"type": kind, "mapping": service.preset(kind), "config": {}})
    with open(run.file_path) as stream:
        reader = csv.DictReader(stream)
        result = next(reader)
        if kind == "csv":
            assert result["master_id"] == MASTER
        else:
            expected = service.normalization.GOOGLE_COLUMNS if kind == "google_customer_match" else service.normalization.META_COLUMNS
            assert list(result) == list(expected) and "master_id" not in result
    assert run.profiles_sent == 1


def test_backfill_commits_each_batch_and_resumes(monkeypatch):
    import app.cli as cli
    import app.identity.master as master
    calls = []
    db = MagicMock()
    session = MagicMock()
    session.return_value.__enter__.return_value = db
    monkeypatch.setattr(cli, "Session", session)
    updates = iter([2, 2, 1, 0])
    monkeypatch.setattr(master, "backfill_master_ids", lambda db, batch_size: calls.append(batch_size) or next(updates))
    assert cli.run_master_id_backfill(None, batch_size=2, max_batches=2) == 4
    assert db.commit.call_count == 2
    assert cli.run_master_id_backfill(None, batch_size=2) == 1
    assert db.commit.call_count == 4 and calls == [2] * 4


def test_profile_list_master_id_search_aliases_and_detail(monkeypatch):
    from app.profiles import api
    from collections import defaultdict
    queries = []
    db = MagicMock()
    db.execute.return_value.scalar_one.return_value = 1
    monkeypatch.setattr(api, "_rows", lambda db, sql, params=None: queries.append(sql) or [])
    api.list_profiles(search=MASTER, source_id=None, has_email=None, has_phone=None,
                      donor_status=None, page=1, page_size=50,
                      user=SimpleNamespace(role="analyst"), db=db)
    assert "p.master_id" in queries[0] and "master_id_aliases" in queries[0]
    profile = defaultdict(lambda: None, id=1, master_id=MASTER)
    monkeypatch.setattr(api, "_profile_or_404", lambda *a: profile)
    detail = api.profile_detail(1, user=SimpleNamespace(role="analyst", id=1), db=db)
    assert detail["master_id"] == MASTER


@pytest.mark.parametrize("with_canonical", [False, True])
def test_webhook_added_and_removed_always_contain_master_id(monkeypatch, with_canonical):
    from app import canonical
    added = {"id": 1, "profile_id": 1, "removed": False, "master_id": MASTER}
    removed = {"id": None, "profile_id": 2, "removed": True, "master_id": None}
    db = MagicMock()
    db.scalar.side_effect = [2, MASTER]
    db.execute.return_value.mappings.return_value = [added, removed]
    monkeypatch.setattr(fields, "enrichments", lambda *a: {})
    # Canonical source records have a different query/row contract from the
    # activation_send rows above. Mock that dependency, not its database rows.
    canonical_many = MagicMock(return_value={1: {"donor_status": "canonical-source"}})
    monkeypatch.setattr(canonical, "canonical_attributes_many", canonical_many)
    monkeypatch.setattr(service, "track_members_durably", lambda *a, **k: None)
    deliver = MagicMock()
    monkeypatch.setattr(security, "deliver", deliver)
    snapshot = {"config": {"mode": "delta", "batch_size": 500, "url": "https://partner.example/"},
                "secrets": {"signing_secret": "test-secret"}, "mapping": {},
                "segment_id": 1, "segment_name": "Test"}
    if with_canonical:
        snapshot["mapping"]["imported_status"] = {"kind": "canonical", "key": "donor_status"}
    run = SimpleNamespace(id=1, activation_id=2, profiles_sent=0)
    service.send_webhook(db, run, snapshot)
    payload = deliver.call_args.args[1]
    expected_added = {"profile_id": 1, "master_id": MASTER}
    if with_canonical:
        expected_added["imported_status"] = "canonical-source"
    assert payload["added"] == [expected_added]
    assert payload["removed"] == [{"profile_id": 2, "master_id": MASTER}]
    canonical_many.assert_called_once_with(db, [1])
    assert run.profiles_sent == 1


def test_filedrop_cli_poll_is_read_only_and_needs_no_live_credentials(config, monkeypatch):
    import app.cli as cli
    import app.db
    monkeypatch.setattr(sys, "argv", ["kinship", "identity-filedrop", "--config", "encrypted.fernet"])
    monkeypatch.setattr(filedrop, "load_config", lambda *_: config)
    poll = MagicMock(return_value={"processed": 1, "skipped": 0})
    monkeypatch.setattr(filedrop, "poll", poll)
    session = MagicMock()
    db = session.return_value.__enter__.return_value
    monkeypatch.setattr(cli, "Session", session)
    cli.main()
    assert str(db.execute.call_args.args[0]) == "SET TRANSACTION READ ONLY"
    poll.assert_called_once_with(db, config)
    db.commit.assert_not_called()


def test_real_profile_detail_viewer_redacts_identity_provenance(db):
    """Exercise migrated DB -> actual detail endpoint -> HTTP JSON serialization."""
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth.deps import current_user
    from app.db import session_scope
    from app.models import User, Source, Profile, SourceRecord, Identifier, ProfileMerge
    from app.profiles import api

    viewer = User(subject="viewer-identity-regression", email="viewer@example.test",
                  name="Viewer", role="viewer")
    source = Source(key="viewer-identity-regression", name="Identity source",
                    kind="csv", record_types=["contact"])
    person, loser = Profile(), Profile()
    db.add_all([viewer, source, person, loser])
    db.flush()
    private = {
        "donor_account_number": "donor-secret-X",
        "hubspot_record_id": "hubspot-secret-X",
        "okta_customer_id": "okta-secret-X",
        "zeta_cdp_id": "cdp-secret-X",
        "zeta_uid": "uuid-secret-X",
        "email_md5": "a1" * 16,
        "email_sha256": "b2" * 32,
        "custom_identifier": "custom-secret-X",
        "email": "private-person@example.test",
        "phone": "+12025550123",
    }
    for kind, value in private.items():
        db.add(Identifier(profile_id=person.id, type=kind, value=value))
    db.add(SourceRecord(source_id=source.id, profile_id=person.id,
                        external_id="external-secret-X", raw_hash="synthetic",
                        attributes={"nested": private, "donorAccountNumber": "camel-secret-X",
                                    "state": "active"}))
    db.add(ProfileMerge(winner_id=person.id, loser_id=loser.id,
                        reason={"keys": [{"kind": "custom_identifier", "value": "reason-secret-X"}],
                                "secret-dictionary-key-X": "legacy"}))
    db.commit()
    app = FastAPI()
    app.include_router(api.router)
    role = {"value": "viewer"}
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=viewer.id, role=role["value"])
    app.dependency_overrides[session_scope] = lambda: db
    client = TestClient(app)
    response = client.get(f"/api/profiles/{person.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["master_id"].startswith("kin_")
    assert all(item["value"] == "[redacted]" for item in body["identifiers"])
    assert body["source_records"][0]["external_id"] == "[redacted]"
    assert body["source_records"][0]["attributes"]["state"] == "active"
    assert body["merges"][0]["reason"] == {"redacted": True}
    for secret in [*private.values(), "external-secret-X", "camel-secret-X",
                   "reason-secret-X", "secret-dictionary-key-X"]:
        assert secret not in json.dumps(body)
    role["value"] = "analyst"
    analyst = client.get(f"/api/profiles/{person.id}")
    assert analyst.status_code == 200
    assert analyst.json()["source_records"][0]["attributes"]["nested"] == private
    assert "reason-secret-X" in analyst.text
