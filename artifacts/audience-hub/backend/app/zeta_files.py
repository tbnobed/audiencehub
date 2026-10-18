"""Bounded, restartable file preparation for Zeta exit exports."""
import csv
import hashlib
import itertools
import json
import os
import re
from pathlib import Path

from app.config import get_settings
from app.source_connectors import MAX_LINES, SourceError, TARGET_FEEDS, jsonl_rows
from app.source_pulls import spool
from app.source_rows import mapped_row, identity_aliases

TARGETS = {"auto", *TARGET_FEEDS}
CHUNK_BYTES = 8 * 1024 * 1024
DROP_FOLDERS = ("/users/", "/resources/", "/events_non_real_time/")


def upload_root():
    root = Path(get_settings().upload_dir).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def checked_path(value):
    original = Path(value)
    path = original.resolve()
    if not path.is_relative_to(upload_root()) or original.is_symlink():
        raise SourceError("Migration file path is not inside protected upload storage.")
    return path


def filename(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,199}", value):
        raise SourceError("Export filename must be a simple basename.")
    if not value.lower().endswith((".csv", ".jsonl", ".ndjson")):
        raise SourceError("Upload CSV, JSONL or NDJSON exports.")
    return value


def records(path):
    csv.field_size_limit(1024 * 1024)
    try:
        with open(path, "rb") as binary:
            beginning = binary.read(3)
        if str(path).lower().endswith((".jsonl", ".ndjson")) or beginning.lstrip().startswith(b"{"):
            with open(path, encoding="utf-8-sig") as stream:
                yield from jsonl_rows(stream)
        else:
            with open(path, newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or len(reader.fieldnames) > 2048 or len(set(reader.fieldnames)) != len(reader.fieldnames):
                    raise SourceError("CSV needs unique headers and at most 2,048 columns.")
                for row in reader:
                    if None in row:
                        raise SourceError("CSV row does not match its headers.")
                    yield row
    except (UnicodeError, csv.Error, ValueError):
        raise SourceError("Export contains invalid UTF-8, CSV or JSONL.") from None


def detect_type(name, first):
    lower = re.sub(r"[^a-z0-9]+", "_", name.lower())
    for target in ("donation_return", "vod_viewed", "order_shipped", "source_codes",
                   "vod_titles", "products", "model_scores", "lookups", "donation"):
        if target in lower:
            return target
    normalized = {re.sub(r"[^a-z0-9]+", "_", key.lower()): value for key, value in first.items()}
    nested = normalized.get("properties", {})
    if isinstance(nested, str):
        try:
            nested = json.loads(nested)
        except ValueError:
            nested = {}
    keys = set(normalized)
    if isinstance(nested, dict):
        keys.update(key.lower() for key in nested)
    event = str(normalized.get("event") or normalized.get("event_name") or normalized.get("name") or normalized.get("type") or "").lower()
    if event in {"donation", "donation_return", "vod_viewed", "order_shipped"}:
        return event
    if {"amount", "timestamp"} <= keys or {"amount", "occurred_at"} <= keys:
        return "donation_return" if str(normalized.get("is_refund", "")).lower() == "true" else "donation"
    if any("lookup" in lower or provider in lower for provider in ("hubspot", "okta", "donor_account")):
        return "lookups"
    if {"source_code", "name"} <= keys and not keys & {"user_id", "zeta_uid", "zeta_cdp_id"}:
        return "source_codes"
    if "product_id" in keys and not keys & {"user_id", "zeta_uid", "zeta_cdp_id"}:
        return "products"
    if "vod_title_id" in keys and not keys & {"user_id", "zeta_uid", "zeta_cdp_id"}:
        return "vod_titles"
    if keys & {"rfm_date", "donation_likelihood_prob", "wealth_score"}:
        return "model_scores"
    if keys & {"zeta_cdp_id", "zeta_uid", "cdp_id", "uid", "user_id", "id", "properties"}:
        return "people"
    raise SourceError("Cannot detect this export type. Select its target type before uploading.")


def normalized_records(path, target, name):
    source = iter(records(path))
    first = next(source, None)
    if first is None:
        return
    if target == "auto":
        target = detect_type(name, first)
    feed = TARGET_FEEDS[target]
    mapping = {"feed": feed, "target_type": target}
    for incoming in itertools.chain([first], source):
        row = mapped_row(incoming, mapping, require_zeta=True)
        yield from identity_aliases(row, feed)


def prepare_file(file, source_key, save_chunk, pulse):
    """Persist a chunk manifest before proceeding; replay skips prepared chunks."""
    path = checked_path(file["file_path"])
    raw = iter(records(path))
    first = next(raw, None)
    if first is None:
        raise SourceError("Empty Zeta export.")
    target = file["target_type"]
    if target == "auto":
        target = detect_type(file["filename"], first)
    feed = TARGET_FEEDS[target]
    iterator = iter(normalized_records(path, target, file["filename"]))
    manifests = list(file["manifests"])
    prepared = file["prepared_chunks"]
    chunk_number = 0
    while (first := next(iterator, None)) is not None:
        chunk = itertools.chain([first], itertools.islice(iterator, MAX_LINES - 1))
        if chunk_number < prepared:
            for _ in chunk:
                pulse()
        else:
            def tracked():
                for item in chunk:
                    pulse()
                    yield item
            manifest = spool(tracked(), feed, source_key, target if feed == "resources" else None)
            if manifest:
                manifests.extend(manifest.pop("children"))
                manifests.append(manifest)
            save_chunk(manifests, chunk_number + 1, target)
        chunk_number += 1
    return manifests


def remote_listing(remote):
    import stat
    result = []
    for folder in DROP_FOLDERS:
        try:
            entries = remote.listdir_attr(folder)
        except FileNotFoundError:
            continue
        for entry in entries:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,199}", entry.filename):
                continue
            if not entry.filename.lower().endswith((".csv", ".jsonl", ".ndjson")):
                continue
            path = folder + entry.filename
            info = remote.lstat(path)
            if stat.S_ISREG(info.st_mode):
                result.append({"path": path, "size": info.st_size, "modified": info.st_mtime})
    return result


def remote_fingerprint(item):
    return hashlib.sha256(f"{item['path']}:{item['size']}:{item['modified']}".encode()).hexdigest()
