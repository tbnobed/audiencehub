"""Encrypted destination secrets and DNS-pinned, non-redirecting webhook POSTs."""
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import urlsplit
from cryptography.fernet import Fernet, InvalidToken
from app.config import get_settings

MAX_ATTEMPTS = 5
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 65536
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_dns_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook-dns")
_dns_slots = threading.BoundedSemaphore(4)
FORBIDDEN_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "proxy-authorization",
    "proxy-connection", "x-ah-signature", "x-ah-timestamp", "idempotency-key",
    "content-type", "accept-encoding", "upgrade", "trailer", "te",
}


class DeliveryError(ValueError):
    """Message is deliberately safe to show/log: no target, body or credentials."""


def encrypt(value):
    return Fernet(get_settings().fernet_key.encode()).encrypt(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def decrypt(value):
    if value is None:
        return {}
    try:
        return json.loads(Fernet(get_settings().fernet_key.encode()).decrypt(bytes(value)))
    except (InvalidToken, ValueError, TypeError):
        raise DeliveryError("Destination credentials could not be decrypted.") from None


def validate_headers(headers):
    if len(headers) > 20:
        raise ValueError("At most 20 custom headers are allowed.")
    seen = set()
    for key, value in headers.items():
        if (not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}", key)
                or key.lower() in FORBIDDEN_HEADERS
                or key.lower() in seen
                or not isinstance(value, str) or len(value) > 4096
                or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise ValueError("Unsupported custom header.")
        seen.add(key.lower())


def target(url, allow_internal=False):
    """Resolve once and validate EVERY answer; returned address is pinned to connect."""
    if not isinstance(url, str):
        raise DeliveryError("Webhook target must be an explicit HTTP or HTTPS URL.")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.query or len(url) > 2048
                or any(ord(c) <= 32 for c in url) or "%" in parsed.hostname):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not _dns_slots.acquire(blocking=False):
            raise DeliveryError("Webhook DNS resolution is busy; try again later.")
        try:
            pending = _dns_workers.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        except BaseException:
            _dns_slots.release()
            raise
        pending.add_done_callback(lambda _: _dns_slots.release())
        try:
            answers = pending.result(timeout=5)
        except FutureTimeout:
            pending.cancel()
            raise DeliveryError("Webhook DNS resolution timed out.") from None
        addresses = list(dict.fromkeys(a[4][0] for a in answers))
        if not addresses:
            raise ValueError()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            effective = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
            if not allow_internal and not effective.is_global:
                raise ValueError()
            # Multicast/unspecified are never meaningful webhook destinations.
            if effective.is_multicast or effective.is_unspecified:
                raise ValueError()
        return parsed, host, port, addresses[0]
    except (ValueError, UnicodeError, OSError):
        raise DeliveryError("Webhook target is invalid or blocked by the network policy.") from None


def post_once(url, body, headers, allow_internal=False):
    parsed, host, port, address = target(url, allow_internal)
    connection = (http.client.HTTPSConnection(host, port, timeout=TIMEOUT_SECONDS,
                  context=ssl.create_default_context()) if parsed.scheme == "https"
                  else http.client.HTTPConnection(host, port, timeout=TIMEOUT_SECONDS))
    # http.client retains the ORIGINAL hostname for Host and TLS SNI/certificate
    # validation. Only TCP connection creation uses the already-checked address.
    # No proxy environment is consulted and a 3xx is never followed.
    def pinned_connect(_address, timeout=TIMEOUT_SECONDS, source_address=None):
        return socket.create_connection((address, port), timeout, source_address)
    connection._create_connection = pinned_connect
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    expired = threading.Event()
    connected = [None]
    def interrupt():
        expired.set()
        sock = connected[0] or connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    deadline = threading.Timer(TIMEOUT_SECONDS, interrupt)
    deadline.daemon = True
    deadline.start()
    try:
        connection.connect()
        connected[0] = connection.sock
        if expired.is_set():
            raise TimeoutError()
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        response.read(MAX_RESPONSE_BYTES + 1)
        if expired.is_set():
            raise TimeoutError()
        return status
    finally:
        deadline.cancel()
        connection.close()


def deliver(url, payload, secret, headers, run_id, batch, allow_internal=False, sleep=time.sleep):
    validate_headers(headers)
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    if len(body) > MAX_PAYLOAD_BYTES:
        raise DeliveryError("Webhook batch exceeds 8 MiB; reduce the batch size or selected fields.")
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    outgoing = dict(headers, **{
        "Content-Type": "application/json",
        "X-AH-Signature": "sha256=" + signature,
        "X-AH-Timestamp": str(int(time.time())),
        "Idempotency-Key": f"{run_id}-{batch}",
    })
    for attempt in range(MAX_ATTEMPTS):
        try:
            status = post_once(url, body, outgoing, allow_internal)
        except DeliveryError:
            raise  # invalid/blocked targets are not transient network failures
        except (OSError, http.client.HTTPException, TimeoutError):
            status = 503
        if 200 <= status < 300:
            return
        if status != 429 and status < 500:
            raise DeliveryError(f"Webhook delivery refused (HTTP {status}); redirects are not followed.")
        if attempt + 1 < MAX_ATTEMPTS:
            sleep(min(2 ** (attempt + 1), 32))
    raise DeliveryError("Webhook delivery failed after five attempts.")
