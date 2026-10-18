"""Host-pinned key-auth publication; manifest rename is the commit marker."""
import gzip
import hashlib
import json
import posixpath
import shutil
import time
from pathlib import Path

from app.activations.security import DeliveryError
from app.identity.filedrop import connect, validate_config, FiledropError


def connection_config(config, credentials):
    output = config.get("output_dir", "")
    return dict(credentials, output_dir=output,
                input_dir="/.activation-unused" if output != "/.activation-unused" else "/.unused")


def validate(config, credentials, enabled=True):
    if set(config) - {"output_dir", "gzip", "include_pii", "delimiter", "export_kind", "approved_purpose"}:
        raise ValueError("Unsupported SFTP destination configuration.")
    output = config.get("output_dir", "")
    if (not isinstance(output, str) or not output.startswith("/")
            or posixpath.normpath(output) != output or any(ord(c) < 32 for c in output)):
        raise ValueError("SFTP output_dir must be an absolute normalized path.")
    if type(config.get("gzip", False)) is not bool:
        raise ValueError("SFTP gzip must be boolean.")
    if config.get("export_kind", "audience") not in ("audience", "crosswalk"):
        raise ValueError("Unsupported SFTP export_kind.")
    if config.get("export_kind") == "crosswalk" and enabled:
        if not isinstance(config.get("approved_purpose"), str) or not config["approved_purpose"].strip():
            raise ValueError("Crosswalk export requires an admin-approved purpose.")
        if not config.get("include_pii", True):
            raise ValueError("Crosswalk exports contain PII.")
    if set(credentials) - {"host", "port", "username", "private_key", "private_key_passphrase", "host_key_sha256"}:
        raise ValueError("Unsupported SFTP credentials.")
    if enabled or credentials:
        validate_config(connection_config(config, credentials))


def package(path, compressed=False):
    path = Path(path)
    if compressed:
        target = path.with_suffix(path.suffix + ".gz")
        with path.open("rb") as source, target.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as output:
                shutil.copyfileobj(source, output, 65536)
        path = target
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return path, digest.hexdigest()


def publish(config, credentials, files, run_id, *, connector=connect, sleep=time.sleep):
    """Files are (local path, row count). Never advertise a partial file set.

    Unique run directories allow standard atomic rename, without depending on
    posix-rename extensions. Consumers only ingest directories containing manifest.json.
    """
    validate(config, credentials)
    entries = []
    for path, count in files:
        path, checksum = package(path, config.get("gzip", False))
        entries.append((path, {"name": path.name, "row_count": count, "sha256": checksum,
                               "bytes": path.stat().st_size}))
    manifest = {"version": 1, "run_id": run_id, "files": [entry for _, entry in entries]}
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    folder = posixpath.join(config["output_dir"], f"run-{int(run_id)}")
    for attempt in range(3):
        try:
            with connector(connection_config(config, credentials)) as remote:
                try:
                    remote.mkdir(folder, mode=0o700)
                except OSError:
                    remote.stat(folder)
                # A lost acknowledgement after the commit rename is success only
                # if the exact manifest matches; never overwrite another receipt.
                try:
                    with remote.open(folder + "/manifest.json", "rb") as receipt:
                        existing = receipt.read(len(body) + 1)
                    if existing != body:
                        raise DeliveryError("SFTP receipt conflicts with this run.")
                    return manifest
                except FileNotFoundError:
                    pass
                for path, entry in entries:
                    temporary = folder + "/" + entry["name"] + ".part"
                    final = folder + "/" + entry["name"]
                    # Retry may follow a lost data rename acknowledgement.
                    for stale in (temporary, final):
                        try:
                            remote.remove(stale)
                        except FileNotFoundError:
                            pass
                    with path.open("rb") as source, remote.open(temporary, "wb") as output:
                        remote.chmod(temporary, 0o600)
                        shutil.copyfileobj(source, output, 65536)
                        output.flush()
                    remote.rename(temporary, final)
                temporary = folder + "/manifest.json.part"
                with remote.open(temporary, "wb") as output:
                    remote.chmod(temporary, 0o600)
                    output.write(body)
                    output.flush()
                remote.rename(temporary, folder + "/manifest.json")
                return manifest
        except DeliveryError:
            raise
        except (OSError, FiledropError):
            if attempt < 2:
                sleep(2 ** attempt)
    raise DeliveryError("SFTP publication failed after three attempts; no confirmed manifest receipt.")
