import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.ingestion.api import allow, authenticate, cors
from app.ingestion.validation import validate, truncate_ip


def test_validation_clamping_ip_and_identity():
    now = datetime.now(timezone.utc)
    message = validate({"anonymousId": "device", "event": "Watch",
                        "timestamp": (now + timedelta(days=8)).isoformat(),
                        "context": {"ip": "192.168.1.25"}}, "track", now, "2001:db8:abcd:1234::5")
    assert message["occurred_at"] == now
    assert message["context"] == {"timestamp_clamped": True, "ip": "2001:db8:abcd::"}
    assert truncate_ip("192.168.1.25") == "192.168.1.0"
    assert truncate_ip("not-ip") is None
    assert message["messageId"]
    for timestamp in ((now - timedelta(days=731)).isoformat(),):
        assert validate({"userId": "u", "timestamp": timestamp}, "page", now)["context"]["timestamp_clamped"]


@pytest.mark.parametrize("message", [
    {}, {"anonymousId": ""}, {"anonymousId": "a", "properties": []},
    {"anonymousId": "a", "properties": {"a": {"b": {"c": {"d": {"e": {}}}}}}},
    {"anonymousId": "a", "properties": {"big": "x" * 32768}},
    {"anonymousId": "a", "timestamp": "2024-01-01"},
    {"anonymousId": "a", "timestamp": 123},
    {"anonymousId": "a", "traits": {"email": "invalid"}},
    {"anonymousId": "a", "properties": {"value": float("nan")}},
])
def test_invalid_messages(message):
    with pytest.raises(ValueError):
        validate(message, "page")


def test_token_bucket_exact_burst_and_refill():
    key = "isolated-rate-test"
    assert all(allow(key, 100) for _ in range(200))
    assert not allow(key, 100)
    assert all(allow(key, 101) for _ in range(50))
    assert not allow(key, 101)


def request(headers):
    return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})


def test_auth_header_basic_and_rejection():
    source = SimpleNamespace(record_types=["event"])
    db = SimpleNamespace(scalar=lambda stmt: source)
    assert authenticate(request({"X-Write-Key": "wk_test_example"}), db) is source
    encoded = base64.b64encode(b"wk_test_example:").decode()
    assert authenticate(request({"Authorization": f"Basic {encoded}"}), db) is source
    for headers in ({}, {"Authorization": "Bearer secret"}, {"Authorization": "Basic ###"},
                    {"X-Write-Key": "different", "Authorization": f"Basic {encoded}"}):
        with pytest.raises(HTTPException) as error:
            authenticate(request(headers), db)
        assert error.value.status_code == 401


def test_cors_explicit_origins_only(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ingest_cors_origins", "https://allowed.example, *")
    assert cors(request({"origin": "https://allowed.example"}))["Access-Control-Allow-Origin"] == "https://allowed.example"
    with pytest.raises(HTTPException):
        cors(request({"origin": "https://other.example"}))
    assert cors(request({})) == {}


def test_seed_sender_uses_actual_http_and_stable_messages(monkeypatch):
    """A local HTTP server, not a DB insert or mocked HTTP transport."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from app.ingestion.seed_events import send_events
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers["X-Write-Key"], body))
            payload = json.dumps({"accepted": len(body["batch"]), "rejected": []}).encode()
            self.send_response(202)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        kwargs = dict(profiles=2, random_seed=7, host=f"http://127.0.0.1:{server.server_port}", write_key="test-key")
        assert send_events(**kwargs) == 8
        assert send_events(**kwargs) == 8
        assert received[0] == received[1]
        assert received[0][0] == "/v1/batch"
        assert received[0][2]["batch"][2]["traits"]["email"].endswith("@example.org")
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_generated_keys_are_redacted_from_structured_logs():
    import logging
    from app.logging_config import JsonFormatter
    key = "wk_live_never-expose-write-key"
    record = logging.LogRecord("test", logging.INFO, "", 1, "credential=%s", (key,), None)
    assert key not in JsonFormatter().format(record)


def test_seed_events_cli_is_additive_and_keeps_key_out_of_arguments(monkeypatch, tmp_path, capsys):
    import sys
    from app.cli import main
    from app import seed
    from app.ingestion import seed_events
    contacts = tmp_path / "contacts.csv"
    calls = []
    monkeypatch.setattr(seed, "generate_seed", lambda **kwargs: {
        "output_dir": tmp_path, "counts": {}, "record_count": 1, "ground_truth": tmp_path,
        "files": {"donor_crm_contacts.csv": contacts}})
    monkeypatch.setattr(seed_events, "send_events", lambda **kwargs: calls.append(kwargs) or 4)
    monkeypatch.setattr(sys, "argv", ["app.cli", "seed", "--profiles", "1", "--events",
                                    "--events-host", "http://localhost:8000"])
    main()
    from datetime import datetime, timezone
    assert calls == [{"profiles": 1, "random_seed": 20250308, "host": "http://localhost:8000", "contacts_path": contacts,
                      "as_of": datetime.now(timezone.utc).date()}]
    assert "Event API accepted 4" in capsys.readouterr().out
