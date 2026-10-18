import csv
import io
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import source_connectors as connectors
from app import source_pulls as pulls
from app.canonical import EVENT_NAMES
from app.imports.validation import map_and_validate_row


def test_jsonl_stream_bounds_and_shape(monkeypatch):
    monkeypatch.setattr(connectors, "MAX_LINES", 2)
    assert list(connectors.jsonl_rows(io.BytesIO(b'{"id":1}\n{"id":2}\n'))) == [{"id": 1}, {"id": 2}]
    for content in (b'{}\n{}\n{}\n', b'[]\n', b'\n', b'{"broken"\n'):
        with pytest.raises(connectors.SourceError):
            list(connectors.jsonl_rows(io.BytesIO(content)))
    monkeypatch.setattr(connectors, "MAX_LINE_BYTES", 4)
    with pytest.raises(connectors.SourceError):
        list(connectors.jsonl_rows(io.BytesIO(b'{"long": 1}\n')))


@pytest.mark.parametrize("query", [
    "DELETE FROM people", "SELECT * FROM people; DROP TABLE people",
    "SELECT * FROM people;", "SELECT system$abort_session(1) FROM people",
    "SELECT * FROM people -- comment", "SELECT * FROM people /*comment*/",
    "WITH x AS (DELETE FROM p) SELECT * FROM x", "SELECT * INTO archive FROM people",
    "SELECT * FROM people UNION SELECT * FROM secrets", "CALL procedure()",
    "SELECT * FROM TABLE(external_function())",
])
def test_strict_readonly(query):
    with pytest.raises(connectors.SourceError):
        connectors.readonly_query(query)


def test_bound_identifiers_cursor_inclusive_and_all_ties():
    result = MagicMock()
    result.__enter__.return_value = result
    result.description = [("ID",), ("UPDATED_AT",)]
    result.fetchmany.side_effect = [[("a", 12), ("b", 12)], [("c", 12)], []]
    connection = MagicMock()
    connection.cursor.return_value = result
    mapping = {"table": "TBN.PUBLIC.PEOPLE", "cursor_column": "updated_at"}
    rows = list(connectors.snowflake_rows(connection, mapping, "12"))
    assert [r["id"] for r in rows] == ["a", "b", "c"]
    sql, values = result.execute.call_args.args
    assert ">= %s" in sql and "TBN.PUBLIC.PEOPLE" not in sql
    assert values == ("TBN.PUBLIC.PEOPLE", "updated_at", "12", "updated_at")
    assert "LIMIT" not in sql


def test_snowflake_100_row_dryrun():
    result = MagicMock()
    result.__enter__.return_value = result
    result.description = [("ID",)]
    result.fetchmany.return_value = [(i,) for i in range(150)]
    connection = MagicMock()
    connection.cursor.return_value = result
    assert len(list(connectors.snowflake_rows(connection, {"query": "SELECT id FROM people"}, limit=100))) == 100
    assert result.execute.call_args.args[1] == (100,)


def test_schedule_eastern_dst_and_defaults():
    event = connectors.DEFAULT_SCHEDULES["events"]
    assert connectors.window(event, datetime(2026, 7, 1, 0, 59, tzinfo=timezone.utc)) is None
    assert connectors.window(event, datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)) == "2026-06-30"
    assert connectors.window(event, datetime(2026, 1, 2, 2, 0, tzinfo=timezone.utc)) == "2026-01-01"
    resource = connectors.DEFAULT_SCHEDULES["resources"]
    assert connectors.window(resource, datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)) == "2026-07-01"
    assert connectors.window(connectors.DEFAULT_SCHEDULES["models"], datetime(2026, 7, 2, 9, tzinfo=timezone.utc)) == "2026-07"


def test_all_audited_events_normalize_and_no_identity_creation():
    for name in EVENT_NAMES:
        kind, row = pulls.canonical_row({"id": "e1", "user_id": "cdp-1", "type": name.upper().replace("_", " "),
                                         "timestamp": "2026-01-02T00:00:00Z", "amount": 10, "fund": "A",
                                         "source_code": "S1"}, "events")
        assert kind == "event" and row["name"] == name
        assert row["contact_external_id"] == "cdp-1"
        assert row["message_id"] == "e1"
        assert row["external_id"] != "e1"
        assert row["properties"]["amount"] == 10


def test_spool_contact_preserves_canonical_properties_and_enrichment(tmp_path, monkeypatch):
    monkeypatch.setattr(pulls, "get_settings", lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    manifest = pulls.spool(iter([{"id": "cdp-7", "properties": {"email": "a@example.org", "lifetime_amount": 15},
                                 "enrichment": {"vendor": "licensed-vendor", "license_id": "contract-1",
                                                "append_date": "2026-01-02", "attributes": {"religion": "licensed-value"}}}]),
                           "people", "tbn")
    assert manifest["record_type"] == "contact"
    with open(manifest["path"]) as handle:
        row = next(csv.DictReader(handle))
    assert row["zeta_cdp_id"] == "cdp-7" and row["lifetime_amount"] == "15"
    assert "enrichment" not in row and "religion" not in row
    assert manifest["children"][0]["record_type"] == "enrichment"
    assert len(list(tmp_path.glob("*.jsonl"))) == 0


def test_missing_license_not_fabricated(tmp_path, monkeypatch):
    monkeypatch.setattr(pulls, "get_settings", lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    with pytest.raises(connectors.SourceError, match="license_id"):
        pulls.spool(iter([{"id": "cdp-7", "enrichment": {"attributes": {"religion": "x"}}}]), "people", "tbn")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name,amount,expected", [("donation", 100, 100), ("donation_return", 25, -25),
                                               ("donation_return", -25, -25)])
def test_donation_gift_projection_refund_evidence(tmp_path, monkeypatch, name, amount, expected):
    monkeypatch.setattr(pulls, "get_settings", lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    manifest = pulls.spool(iter([{"id": "d1", "user_id": "cdp-7", "event": name,
                                 "timestamp": "2026-01-02T00:00:00Z", "amount": amount,
                                 "fund": "Mission", "source_code": "S1"}]), "events", "tbn")
    gift = manifest["children"][0]
    assert gift["record_type"] == "gift"
    with open(gift["path"]) as handle:
        row = next(csv.DictReader(handle))
    result = map_and_validate_row(row, gift["mapping"]["columns"], "gift", gift["mapping"]["options"])
    assert result["errors"] == []
    assert result["values"]["amount"] == expected
    assert result["values"]["attributes"]["source_code"] == "S1"
    assert result["values"]["attributes"].get("is_refund", False) == (name == "donation_return")


def test_configuration_rejects_secret_in_config_and_mutating_queries():
    config = {"mappings": [{"id": "people", "feed": "people", "table": "PUBLIC.PEOPLE", "cursor_column": "updated_at"}]}
    assert connectors.validate_config("snowflake", config) == config
    with pytest.raises(connectors.SourceError):
        connectors.validate_config("snowflake", {**config, "private_key": "never-store-plaintext"})
    with pytest.raises(connectors.SourceError):
        connectors.validate_config("snowflake", {"mappings": [{"id": "people", "feed": "people", "table": "X; DELETE"}]})


def test_sftp_skips_symlinks_and_paths():
    import stat
    regular = SimpleNamespace(filename="people.jsonl", st_mode=stat.S_IFREG, st_size=2, st_mtime=1)
    link = SimpleNamespace(filename="link.jsonl", st_mode=stat.S_IFLNK, st_size=2, st_mtime=1)
    remote = MagicMock()
    remote.listdir_attr.return_value = [regular, link, SimpleNamespace(filename="../escape.jsonl")]
    remote.lstat.side_effect = lambda path: link if "link" in path else regular
    assert [p for p, _ in connectors.remote_files(remote, {"folder": "/users/"})] == ["/users/people.jsonl"]


def test_collect_cursor_only_returned_after_complete_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(pulls, "get_settings", lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    monkeypatch.setattr(pulls.security, "decrypt", lambda value: {})
    @contextmanager
    def connection(credentials):
        yield object()
    monkeypatch.setattr(pulls, "snowflake_connection", connection)
    def broken(*args):
        yield {"id": "cdp-1", "updated_at": 1}
        raise connectors.SourceError("Remote interrupted.")
    monkeypatch.setattr(pulls, "snowflake_rows", broken)
    state = {"cursors": {"people": 0}}
    row = {"credentials": b"", "kind": "snowflake", "key": "tbn", "state": state,
           "config": {"mappings": [{"id": "people", "feed": "people", "table": "PEOPLE", "cursor_column": "updated_at"}]}}
    with pytest.raises(connectors.SourceError):
        pulls.collect(row)
    assert state == {"cursors": {"people": 0}}


def test_encrypted_credentials_roundtrip_no_plaintext():
    from app.activations import security
    token = security.encrypt({"private_key": "test-key-not-a-secret", "user": "unit-test"})
    assert b"test-key-not-a-secret" not in token
    assert security.decrypt(token)["user"] == "unit-test"


def test_mock_keypair_connection_never_passes_pem_or_password():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    factory = MagicMock()
    credentials = {"account": "org-account", "user": "reader", "role": "KINSHIP_READER", "private_key": pem}
    with connectors.snowflake_connection(credentials, factory=factory) as connection:
        assert connection is factory.return_value
    options = factory.call_args.kwargs
    assert isinstance(options["private_key"], bytes) and b"BEGIN" not in options["private_key"]
    assert "password" not in options and options["role"] == "KINSHIP_READER"
    assert options["session_parameters"]["STATEMENT_TIMEOUT_IN_SECONDS"] == 600
    factory.return_value.close.assert_called_once()


def test_jobs_and_schedule_callable_wiring(monkeypatch):
    from app.jobs import handlers, scheduler
    executed = []
    monkeypatch.setattr(pulls, "run_source", lambda *args, **kwargs: executed.append((args, kwargs)))
    handlers.run("source.pull", {"source_id": 7, "feed": "people"}, job_id=88)
    assert executed == [((7,), {"feed": "people", "job_id": 88})]
    scheduled = []
    monkeypatch.setattr(scheduler, "schedule_once", lambda *args: scheduled.append(args))
    db = MagicMock()
    db.execute.return_value.mappings.return_value = [
        {"id": 7, "config": {"mappings": [{"id": "p", "feed": "people"}, {"id": "e", "feed": "events"}]}}
    ]
    pulls.tick_sources(db, datetime(2026, 7, 1, 1, tzinfo=timezone.utc))
    assert len(scheduled) == 2 and all(call[3] == "source.pull" for call in scheduled)


def test_source_handler_redacts_database_or_transport_details(monkeypatch):
    from app.jobs import handlers
    def broken(*args, **kwargs):
        raise RuntimeError("private-key=must-not-leak")
    monkeypatch.setattr(pulls, "run_source", broken)
    with pytest.raises(connectors.SourceError) as failure:
        handlers.run("source.pull", {"source_id": 7})
    assert "must-not-leak" not in str(failure.value)
    assert failure.value.__suppress_context__


def test_cursor_ties_across_multiple_spools(tmp_path, monkeypatch):
    monkeypatch.setattr(pulls, "get_settings", lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    monkeypatch.setattr(pulls, "MAX_LINES", 2)
    monkeypatch.setattr(pulls.security, "decrypt", lambda _: {})
    @contextmanager
    def connection(_):
        yield object()
    monkeypatch.setattr(pulls, "snowflake_connection", connection)
    monkeypatch.setattr(pulls, "snowflake_rows", lambda *args: iter(
        [{"id": "cdp-1", "updated_at": 12}, {"id": "cdp-2", "updated_at": 12}, {"id": "cdp-3", "updated_at": 12}]))
    row = {"credentials": b"", "kind": "snowflake", "key": "tbn", "state": {"cursors": {"people": 11}},
           "config": {"mappings": [{"id": "people", "feed": "people", "table": "PEOPLE", "cursor_column": "updated_at"}]}}
    manifests, cursors = pulls.collect(row)
    assert [m["count"] for m in manifests] == [2, 1]
    assert cursors == {"people": "12"}
    assert row["state"]["cursors"] == {"people": 11}


def test_mock_sftp_three_folders_and_metadata_change(tmp_path, monkeypatch):
    import stat
    monkeypatch.setattr(pulls, "get_settings", lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    monkeypatch.setattr(pulls.security, "decrypt", lambda _: {})
    remote = MagicMock()
    info = SimpleNamespace(filename="feed.jsonl", st_mode=stat.S_IFREG, st_size=100, st_mtime=1)
    remote.listdir_attr.return_value = [info]
    remote.lstat.return_value = info
    content = {
        "/users/feed.jsonl": {"id": "cdp-1", "properties": {"first_name": "Donor"}},
        "/resources/feed.jsonl": {"key": "S1", "name": "Appeal"},
        "/events_non_real_time/feed.jsonl": {"id": "e1", "user_id": "cdp-1", "event": "vod_viewed",
                                           "timestamp": "2026-01-02T00:00:00Z"},
    }
    remote.open.side_effect = lambda path, mode: io.BytesIO((json.dumps(content[path]) + "\n").encode())
    @contextmanager
    def connection(_):
        yield remote
    monkeypatch.setattr(pulls, "sftp_connection", connection)
    row = {"credentials": b"", "kind": "sftp_jsonl", "key": "tbn", "state": {},
           "config": {"mappings": [
               {"id": "p", "feed": "people", "folder": "/users/"},
               {"id": "r", "feed": "resources", "folder": "/resources/", "resource_type": "source_codes"},
               {"id": "e", "feed": "events", "folder": "/events_non_real_time/"},
           ]}}
    manifests, _ = pulls.collect(row)
    assert {m["record_type"] for m in manifests} == {"contact", "source_codes", "event"}
    remote.lstat.side_effect = [info, SimpleNamespace(st_size=101, st_mtime=2)]
    with pytest.raises(connectors.SourceError, match="changed"):
        pulls.collect(row)


def test_pull_history_allowlist_never_exposes_job_errors_or_payload():
    from app.source_api import pull_history_item
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    row = {"id": 12, "status": "failed", "feed": "private-key-not-a-feed", "attempts": 3,
           "max_attempts": 3, "has_error": True, "error": "SELECT email, private_key FROM secret",
           "payload": {"private_key": "never-return"}, "progress": {"pii": "never-return"},
           "batch_id": "123", "created_at": now, "run_after": now, "started_at": now,
           "finished_at": now, "heartbeat_at": None, "imports_total": 2, "imports_completed": 1,
           "rows_total": 25, "rows_ok": 10, "rows_rejected": 0}
    actual = pull_history_item(row)
    assert actual["feed"] is None and actual["status"] == "failed"
    assert actual["counts"]["imports_completed"] == 1
    assert actual["created_at"] == "2026-07-01T00:00:00+00:00"
    serialized = json.dumps(actual)
    assert "never-return" not in serialized and "SELECT" not in serialized
    assert "private-key" not in serialized and "batch_id" not in actual
