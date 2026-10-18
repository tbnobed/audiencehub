"""Deterministic fake SFTP tests: no sockets, credentials or remote services."""
import gzip
import hashlib
import io
import json
from contextlib import contextmanager
from datetime import datetime, timezone
import pytest
from app.activations import schedule, sftp
from app.activations.security import DeliveryError

CREDS = {"host": "sftp.example.test", "username": "unit", "private_key": "unit-test-not-a-key",
         "host_key_sha256": "SHA256:" + "A" * 43}
CONFIG = {"output_dir": "/exports", "gzip": True}


class Remote:
    def __init__(self, failures=0):
        self.files = {}
        self.renames = []
        self.failures = failures
    def mkdir(self, *args, **kwargs):
        pass
    def stat(self, path):
        return object()
    def chmod(self, *args):
        pass
    def remove(self, path):
        if path not in self.files:
            raise FileNotFoundError()
        del self.files[path]
    def open(self, path, mode):
        if mode == "rb":
            if path not in self.files:
                raise FileNotFoundError()
            return io.BytesIO(self.files[path])
        remote = self
        class Writer(io.BytesIO):
            def close(self):
                remote.files[path] = self.getvalue()
                super().close()
        return Writer()
    def rename(self, old, new):
        if "aliases" in new and self.failures:
            self.failures -= 1
            raise OSError("simulated transport")
        self.files[new] = self.files.pop(old)
        self.renames.append(new)
    @contextmanager
    def connect(self, config):
        yield self


def test_gzip_two_file_manifest_commits_last_and_retry(tmp_path):
    first, second = tmp_path / "crosswalk.csv", tmp_path / "aliases.csv"
    first.write_text("master_id,key_type,key_value,status,updated_at\n1,email,a,active,t\n")
    second.write_text("retired_id,survivor_id,merged_at\n2,1,t\n")
    remote, waits = Remote(failures=1), []
    result = sftp.publish(CONFIG, CREDS, [(first, 1), (second, 1)], 7,
                          connector=remote.connect, sleep=waits.append)
    assert waits == [1]
    assert remote.renames[-1] == "/exports/run-7/manifest.json"
    assert len(result["files"]) == 2
    for item, original in zip(result["files"], (first, second)):
        blob = remote.files["/exports/run-7/" + item["name"]]
        assert hashlib.sha256(blob).hexdigest() == item["sha256"]
        assert gzip.decompress(blob) == original.read_bytes()
        assert item["row_count"] == 1
    assert json.loads(remote.files[remote.renames[-1]]) == result
    # An uncertain acknowledgement cannot lead to a second publication.
    renames = list(remote.renames)
    assert sftp.publish(CONFIG, CREDS, [(first, 1), (second, 1)], 7,
                        connector=remote.connect, sleep=waits.append) == result
    assert remote.renames == renames


def test_partial_failure_never_publishes_manifest(tmp_path):
    first, second = tmp_path / "crosswalk.csv", tmp_path / "aliases.csv"
    first.write_text("header\n")
    second.write_text("header\n")
    remote, waits = Remote(failures=10), []
    with pytest.raises(DeliveryError, match="three attempts"):
        sftp.publish(CONFIG, CREDS, [(first, 0), (second, 0)], 8,
                     connector=remote.connect, sleep=waits.append)
    assert waits == [1, 2]
    assert not any(p.endswith("manifest.json") for p in remote.files)


def test_cron_29_skips_nonleap_february_and_uses_et():
    value = schedule.next_run("0 0 29 * *", "America/New_York",
                              datetime(2025, 2, 1, tzinfo=timezone.utc))
    assert value == datetime(2025, 3, 29, 4, tzinfo=timezone.utc)


def test_disabled_credentials_and_crosswalk_purpose():
    sftp.validate({"output_dir": "/exports"}, {}, enabled=False)
    with pytest.raises(ValueError):
        sftp.validate({"output_dir": "/exports"}, {})
    with pytest.raises(ValueError, match="purpose"):
        sftp.validate({"output_dir": "/exports", "export_kind": "crosswalk"}, CREDS)
    with pytest.raises(ValueError):
        sftp.validate({"output_dir": "/exports/../private"}, CREDS)
