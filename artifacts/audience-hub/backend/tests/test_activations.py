"""M7 provider vectors, cron and real bounded webhook transport tests."""
import hashlib
import hmac
import json
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from app.activations import normalization as n, security, schedule


@pytest.fixture
def echo():
    state = {"requests": [], "statuses": []}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append((dict(self.headers), body))
            status = state["statuses"].pop(0) if state["statuses"] else 200
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/audience"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def test_provider_vectors_and_formula_cells():
    row = {"id": 42, "email": "  Zoe.Perez@GMAIL.com  ", "phone": "+1 (202) 555-0123",
           "first_name": " Zoë ", "last_name": "Pérez-O'Neil", "country": "us",
           "postal_code": "90210-1234", "region": "CA", "city": "Los Angeles"}
    google_values = ["zoeperez@gmail.com", "+12025550123", "zoë", "pérezoneil"]
    assert n.google(row) == [hashlib.sha256(v.encode()).hexdigest() for v in google_values] + ["US", "90210"]
    meta_values = ["zoe.perez@gmail.com", "12025550123", "zoë", "pérezoneil",
                   "90210-1234", "losangeles", "ca", "us", "42"]
    assert n.meta(row) == [hashlib.sha256(v.encode()).hexdigest() for v in meta_values]
    # Fixed independently computed standard SHA256 vector; Unicode survives.
    assert n.digest("test@example.com") == "973dfe463ec85785f5f95af5ba3906eedb2d931c24e69824a89ea65dba4e813b"
    assert n.name("Zoë") == "zoë"
    assert n.meta(dict(row, region="California"))[6] == hashlib.sha256(b"ca").hexdigest()
    for value in ("=SUM(A1)", "+123", "-3", "@formula", "  =formula", "\t=evil"):
        assert n.csv_safe(value).startswith("'")
    assert n.csv_safe("Partner") == "Partner"


@pytest.mark.parametrize("address", ["127.0.0.1", "10.12.1.2", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1"])
def test_ssrf_blocks_private_literals(address):
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(security.DeliveryError):
        security.target(f"http://{host}/")


def test_ssrf_dns_all_answers_and_url_guards(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80))])
    with pytest.raises(security.DeliveryError):
        security.target("http://partner.example/")
    assert security.target("http://partner.example/", True)[3] == "8.8.8.8"
    for value in ("file:///tmp/file", "http://user:secret@example.org", "http://example.org/#fragment",
                  "http://example.org/\r\nHeader:bad", "http://[fe80::1%eth0]/"):
        with pytest.raises(security.DeliveryError):
            security.target(value, True)
    for header in ("Host", "X-AH-Signature", "Content-Length", "Proxy-Authorization"):
        with pytest.raises(ValueError):
            security.validate_headers({header: "secret"})


def test_real_webhook_signature_retries_and_no_redirect(echo):
    echo["statuses"] = [429, 503, 200]
    waits = []
    payload = {"added": [{"profile_id": 1}], "removed": [], "mode": "delta"}
    secret = "synthetic-secret-with-at-least-16"
    security.deliver(echo["url"], payload, secret, {"Authorization": "synthetic"}, 88, 2, True, waits.append)
    assert waits == [2, 4] and len(echo["requests"]) == 3
    headers, body = echo["requests"][0]
    assert headers["X-AH-Signature"] == "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert headers["Idempotency-Key"] == "88-2" and headers["X-AH-Timestamp"].isdigit()
    assert all(item[1] == body and item[0]["Idempotency-Key"] == "88-2" for item in echo["requests"])
    for status in (400, 401, 403, 302):
        echo["requests"].clear()
        echo["statuses"] = [status]
        with pytest.raises(security.DeliveryError):
            security.deliver(echo["url"], payload, secret, {}, 89, 1, True, waits.append)
        assert len(echo["requests"]) == 1


def test_five_network_attempts(monkeypatch):
    calls, waits = [], []
    def fail(*a, **k):
        calls.append(1)
        raise OSError("Sensitive network detail must not escape")
    monkeypatch.setattr(security, "post_once", fail)
    with pytest.raises(security.DeliveryError, match="five attempts"):
        security.deliver("http://unused.test/", {}, "synthetic-secret-value", {}, 1, 1, sleep=waits.append)
    assert len(calls) == 5 and waits == [2, 4, 8, 16]


def test_dns_pinned_connection_preserves_original_host(echo, monkeypatch):
    real = socket.getaddrinfo
    calls = []
    def resolve(host, *args, **kwargs):
        calls.append(host)
        if host == "rebinding.example":
            return real("127.0.0.1", *args, **kwargs)
        return real(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    url = echo["url"].replace("127.0.0.1", "rebinding.example")
    security.deliver(url, {"safe": True}, "synthetic-secret-value", {}, 1, 1, True, lambda _: None)
    assert calls.count("rebinding.example") == 1
    assert echo["requests"][0][0]["Host"].startswith("rebinding.example:")


def test_slow_response_has_absolute_deadline(monkeypatch):
    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(.01)
            except (OSError, BrokenPipeError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(security, "TIMEOUT_SECONDS", .05)
    try:
        start = time.monotonic()
        with pytest.raises(security.DeliveryError, match="five attempts"):
            security.deliver(f"http://127.0.0.1:{server.server_port}/", {},
                             "synthetic-signing-secret", {}, 1, 1, True, lambda _: None)
        assert time.monotonic() - start < 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_cron_validation_timezones_and_dst():
    schedule.validate("*/15 9-17 * * 1-5", "America/Chicago")
    assert schedule.matches("0 6 * * 1", "America/Chicago", datetime(2024, 1, 8, 12, tzinfo=timezone.utc))
    assert schedule.next_run("30 2 * * *", "America/Chicago",
        datetime(2024, 3, 10, 7, tzinfo=timezone.utc)) == datetime(2024, 3, 11, 7, 30, tzinfo=timezone.utc)
    assert schedule.next_run("0 0 29 2 *", "UTC",
        datetime(2025, 1, 1, tzinfo=timezone.utc)) == datetime(2028, 2, 29, tzinfo=timezone.utc)
    for cron in ("* * *", "61 * * * *", "*/0 * * * *", "0 0 * 13 *", "0 0 * * 8"):
        with pytest.raises(ValueError):
            schedule.validate(cron, "UTC")
    with pytest.raises(ValueError):
        schedule.validate("* * * * *", "invalid/zone")
