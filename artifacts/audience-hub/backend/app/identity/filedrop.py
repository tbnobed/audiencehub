"""Bounded, read-only identity lookup over an encrypted, host-pinned SFTP drop.

The config file is a Fernet token created with activations.security.encrypt:
host, port (22), username, private_key (OpenSSH/PEM), host_key_sha256
(SHA256:base64), input_dir, output_dir; optional private_key_passphrase.
Input files must be published by their producer with an atomic rename to .csv.
No credentials, input values, remote filenames or exception bodies are logged.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import posixpath
import re
import socket
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.activations import security
from app.activations.normalization import csv_safe

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10_000
KEY_COLUMNS = {
    "hubspot_record_id": "hubspot_record_id",
    "hubspot_record_ids": "hubspot_record_id",
    "okta_customer_id": "okta_customer_id",
    "okta_customer_ids": "okta_customer_id",
    "fusionauth_user_id": "fusionauth_user_id",
    "fusionauth_user_ids": "fusionauth_user_id",
    "donor_account_number": "donor_account_number",
    "donor_account_numbers": "donor_account_number",
}


class FiledropError(ValueError):
    """Only constant, non-PII messages may cross the CLI boundary."""


def load_config(path):
    try:
        token = Path(path).read_bytes()
        if len(token) > 128 * 1024:
            raise ValueError()
        config = security.decrypt(token)
        validate_config(config)
        return config
    except (OSError, ValueError, TypeError, KeyError):
        raise FiledropError("SFTP lookup encrypted configuration is invalid or unavailable.") from None


def validate_config(config):
    required = {"host", "username", "private_key", "host_key_sha256", "input_dir", "output_dir"}
    if not isinstance(config, dict) or not required <= config.keys():
        raise FiledropError("SFTP lookup configuration is incomplete.")
    if any(not isinstance(config[key], str) or not config[key] for key in required):
        raise FiledropError("SFTP lookup configuration fields must be nonempty text.")
    host = config["host"]
    if not re.fullmatch(r"[A-Za-z0-9.:\-]+", host):
        raise FiledropError("SFTP lookup host is invalid.")
    if type(config.get("port", 22)) is not int or not 1 <= config.get("port", 22) <= 65535:
        raise FiledropError("SFTP lookup port is invalid.")
    if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}=?", config["host_key_sha256"]):
        raise FiledropError("SFTP lookup requires an SHA256 host-key pin.")
    for key in ("input_dir", "output_dir"):
        path = config[key]
        if not path.startswith("/") or posixpath.normpath(path) != path or any(ord(c) < 32 for c in path):
            raise FiledropError("SFTP lookup folders must be absolute normalized paths.")
    if config["input_dir"] == config["output_dir"]:
        raise FiledropError("SFTP lookup input and output folders must differ.")


def verify_host_key(key, expected):
    actual = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    if not hmac.compare_digest(actual, expected.rstrip("=")):
        raise FiledropError("SFTP lookup host-key pin did not match.")


@contextmanager
def connect(config):
    """Validate every DNS answer and connect only to the pinned public address."""
    validate_config(config)
    sock = transport = client = None
    try:
        import paramiko
        host = config["host"]
        authority = f"[{host}]" if ":" in host else host
        _, _, port, address = security.target(f"https://{authority}:{config.get('port', 22)}/")
        sock = socket.create_connection((address, port), timeout=10)
        transport = paramiko.Transport(sock)
        transport.banner_timeout = transport.auth_timeout = 10
        transport.start_client(timeout=10)
        verify_host_key(transport.get_remote_server_key(), config["host_key_sha256"])
        private = io.StringIO(config["private_key"])
        key = None
        for key_class in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            private.seek(0)
            try:
                key = key_class.from_private_key(private, password=config.get("private_key_passphrase"))
                break
            except (paramiko.SSHException, ValueError):
                continue
        if key is None:
            raise FiledropError("SFTP lookup private key could not be loaded.")
        transport.auth_publickey(config["username"], key)
        client = paramiko.SFTPClient.from_transport(transport)
        client.get_channel().settimeout(10)
        yield client
    except FiledropError:
        raise
    except Exception:
        raise FiledropError("SFTP lookup connection or transfer failed; verify configuration and server availability.") from None
    finally:
        if client is not None:
            client.close()
        if transport is not None:
            transport.close()
        if sock is not None:
            sock.close()


def lookup_csv(db, content, lookup=None):
    """Return CSV key -> canonical master ID; never guess unknown or ambiguous IDs."""
    if len(content) > MAX_BYTES:
        raise FiledropError("SFTP lookup CSV exceeds the 8 MiB limit.")
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""), strict=True)
        columns = reader.fieldnames or []
        if len(columns) != 1 or columns[0] not in KEY_COLUMNS:
            raise FiledropError("SFTP lookup CSV requires exactly one supported identity-key column.")
        items = []
        for row in reader:
            if len(items) >= MAX_ROWS or None in row or row[columns[0]] is None:
                raise FiledropError("SFTP lookup CSV is malformed or exceeds 10,000 rows.")
            value = row[columns[0]].strip()
            if not value or len(value) > 512 or any(ord(c) < 32 for c in value):
                raise FiledropError("SFTP lookup CSV contains an invalid identity key.")
            items.append({"key_type": KEY_COLUMNS[columns[0]], "value": value})
    except (UnicodeError, csv.Error):
        raise FiledropError("SFTP lookup CSV must be valid UTF-8 CSV.") from None
    if not items:
        raise FiledropError("SFTP lookup CSV must contain at least one identity key.")
    if lookup is None:
        from app.identity.master import lookup_bulk
        lookup = lookup_bulk
    try:
        results = lookup(db, items)
    except ValueError:
        raise FiledropError("SFTP lookup could not validate the supplied identity keys.") from None
    if len(results) != len(items):
        raise FiledropError("SFTP lookup returned an invalid result count.")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["key_type", "key_value", "master_id", "status"])
    for item, result in zip(items, results):
        status = "unknown" if result["status"] == "not_found" else result["status"]
        master_id = result.get("master_id")
        if status not in {"matched", "unknown", "ambiguous"} or (status == "matched" and not master_id):
            raise FiledropError("SFTP lookup returned an invalid identity result.")
        writer.writerow([item["key_type"], csv_safe(item["value"]),
                         master_id if status == "matched" else "", status])
    return stream.getvalue().encode("utf-8")


def _exists(sftp, path):
    try:
        sftp.lstat(path)
        return True
    except FileNotFoundError:
        return False


def poll(db, config, *, sftp=None, lookup=None):
    """One bounded folder pass. Outputs are immutable and content-addressed.

    Repeated input bytes/name produce the same result path; successful outputs
    are skipped without re-resolving. A retry after a failed write replaces only
    a random unpublished .tmp file. Standard SFTP rename publishes atomically
    without overwriting an existing output (including a concurrent worker's).
    """
    validate_config(config)
    if sftp is None:
        with connect(config) as remote:
            return poll(db, config, sftp=remote, lookup=lookup)
    counts = {"processed": 0, "skipped": 0}
    try:
        entries = sorted(sftp.listdir_attr(config["input_dir"]), key=lambda entry: entry.filename)
        for entry in entries[:1000]:
            name = entry.filename
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.csv", name):
                continue
            source = posixpath.join(config["input_dir"], name)
            info = sftp.lstat(source)
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > MAX_BYTES:
                raise FiledropError("SFTP lookup input exceeds the 8 MiB limit.")
            with sftp.open(source, "rb") as stream:
                content = stream.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise FiledropError("SFTP lookup input exceeds the 8 MiB limit.")
            digest = hashlib.sha256(name.encode() + b"\0" + content).hexdigest()
            final = posixpath.join(config["output_dir"], digest + ".lookup.csv")
            if _exists(sftp, final):
                counts["skipped"] += 1
                continue
            output = lookup_csv(db, content, lookup)
            temporary = final + "." + uuid.uuid4().hex + ".tmp"
            try:
                with sftp.open(temporary, "wx") as stream:
                    sftp.chmod(temporary, 0o600)
                    stream.write(output)
                    stream.flush()
                try:
                    sftp.rename(temporary, final)
                except OSError:
                    if not _exists(sftp, final):
                        raise
            finally:
                if _exists(sftp, temporary):
                    sftp.remove(temporary)
            counts["processed"] += 1
        return counts
    except FiledropError:
        raise
    except Exception:
        raise FiledropError("SFTP lookup polling failed; no partial output was published for the failed input.") from None
