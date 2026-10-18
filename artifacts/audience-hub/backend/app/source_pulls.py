"""Durable source jobs through the existing checkpointed COPY importer.

Remote streams are converted to bounded disk spools, never a row ORM loop.
An entire poll is registered before any import executes. Failed polls retain
their batch and immutable local inputs, so retry resumes the same checkpoints.
"""
import csv
import hashlib
import itertools
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.activations import security
from app.config import get_settings
from app.db import engine
from app.imports.mapping import TYPE_TARGETS, suggest_mapping, validate_mapping
from app.imports.service import run_import
from app.imports.staging import copy_upsert
from app.imports.validation import map_and_validate_row
from app.jobs.queue import enqueue
from app.source_connectors import (
    DEFAULT_SCHEDULES, MAX_LINES, RESOURCES, SourceError, jsonl_rows, remote_files,
    sftp_connection, snowflake_connection, snowflake_rows, validate_config, window,
)

IDENTITIES = ("zeta_cdp_id", "zeta_uid", "donor_account_number", "hubspot_record_id", "okta_customer_id")


def json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def canonical_row(row, feed):
    row = dict(row)
    if feed == "_gifts":
        return "gift", row
    if feed in {"people", "models", "lookups"}:
        properties = row.pop("properties", {})
        if not isinstance(properties, dict):
            raise SourceError("People properties must be an object.")
        attributes = row.pop("attributes", {})
        if not isinstance(attributes, dict):
            raise SourceError("People attributes must be an object.")
        row = {**properties, **attributes, **row}
        if "enrichment" in row:
            raise SourceError("Appended enrichment must be extracted with its licensing evidence.")
        zeta_id = row.pop("user_id", None) or row.pop("id", None)
        if zeta_id and not any(row.get(key) for key in ("zeta_cdp_id", "zeta_uid")):
            row["zeta_cdp_id" if str(zeta_id).startswith("cdp-") else "zeta_uid"] = str(zeta_id)
        row.setdefault("external_id", next((str(row[k]) for k in IDENTITIES if row.get(k)), None))
        if not row.get("external_id"):
            raise SourceError("People, models and lookup rows require a stable external identity.")
        return "contact", row
    if feed == "enrichment":
        attributes = row.pop("attributes", {})
        if not isinstance(attributes, dict):
            raise SourceError("Enrichment attributes must be an object.")
        row = {**attributes, **row}
        row.setdefault("contact_external_id", row.pop("user_id", None) or next(
            (str(row[k]) for k in IDENTITIES if row.get(k)), None))
        for key in IDENTITIES:
            row.pop(key, None)
        if not all(row.get(key) for key in ("vendor", "license_id", "append_date", "contact_external_id")):
            raise SourceError("Enrichment requires vendor, license_id, append_date and a Zeta/contact identity.")
        row.setdefault("external_id", hashlib.sha256(json.dumps(row, sort_keys=True, default=json_default).encode()).hexdigest())
        row["external_id"] = "_enrichment:" + str(row["external_id"])
        return "enrichment", row
    if feed == "resources":
        row["key"] = str(row.get("key") or row.get("id") or row.get("source_code") or "")
        if not row["key"] or len(row["key"]) > 200:
            raise SourceError("Resource rows require a key.")
        if not isinstance(row.get("attributes", {}), dict):
            raise SourceError("Resource attributes must be an object.")
        if any(row.get(key) is not None and not isinstance(row[key], str) for key in ("name", "series")):
            raise SourceError("Resource name and series must be text.")
        row.pop("id", None)
        return "resource", row
    row.setdefault("contact_external_id", row.pop("user_id", None) or next(
        (str(row[k]) for k in IDENTITIES if row.get(k)), None))
    for key in IDENTITIES:
        row.pop(key, None)
    row.setdefault("external_id", row.pop("id", None))
    row.setdefault("name", row.pop("event", None) or row.get("type"))
    from app.canonical import EVENT_NAMES
    names = {re.sub(r"[^a-z0-9]", "", name): name for name in EVENT_NAMES}
    compact = re.sub(r"[^a-z0-9]", "", str(row.get("name", "")).lower())
    if compact in names:
        row["name"] = names[compact]
    if row.get("type") not in {None, "track", "page", "screen", "identify"}:
        row.pop("type", None)
    row.setdefault("occurred_at", row.pop("timestamp", None))
    if not row.get("external_id"):
        raise SourceError("Events require a stable external_id to preserve retry idempotency.")
    row.setdefault("message_id", str(row["external_id"]))
    row["external_id"] = "_event:" + str(row["external_id"])
    if not row.get("contact_external_id") and not row.get("email") and not row.get("phone"):
        raise SourceError("Events require a contact reference; events never create identities.")
    properties = row.get("properties", {})
    if not isinstance(properties, dict):
        raise SourceError("Event properties must be an object.")
    for key in list(row):
        if key not in TYPE_TARGETS["event"]:
            properties[key] = row.pop(key)
    row["properties"] = properties
    return "event", row


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=json_default)
    if isinstance(value, bool):
        return "true" if value else "false"
    return json_default(value)


def mapping_for(headers, record_type, source_key):
    columns = suggest_mapping(headers, record_type)
    if record_type == "contact":
        for header in headers:
            if header not in columns:
                columns[header] = "attributes." + header
    if record_type == "enrichment":
        columns = {header: (header if header in TYPE_TARGETS["enrichment"] else
                            {"enrichment": header, "data_type": "text"}) for header in headers}
    return {"columns": validate_mapping(columns, record_type, headers),
            "options": {"reference_source": source_key}}


def spool(rows, feed, source_key, resource_type=None):
    """At most 500k rows per immutable spool, O(row + header count) RAM."""
    directory = Path(get_settings().upload_dir).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="pull-", suffix=".jsonl", dir=directory)
    enrich_fd, enrich_path = tempfile.mkstemp(prefix="pull-enrichment-", suffix=".jsonl", dir=directory)
    gift_fd, gift_path = tempfile.mkstemp(prefix="pull-gifts-", suffix=".jsonl", dir=directory)
    headers, count, record_type = set(), 0, None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as raw, os.fdopen(enrich_fd, "w", encoding="utf-8") as enriched, os.fdopen(gift_fd, "w", encoding="utf-8") as gifts:
            for incoming in rows:
                incoming, extras = split_enrichment(incoming, feed)
                record_type, row = canonical_row(incoming, feed)
                gift = gift_for_event(row) if record_type == "event" else None
                if gift:
                    gifts.write(json.dumps(gift, default=json_default) + "\n")
                for extra in extras:
                    extra["contact_external_id"] = row["external_id"]
                    # Validate evidence now, never fabricate a vendor/license.
                    canonical_row(extra, "enrichment")
                    enriched.write(json.dumps(extra, default=json_default) + "\n")
                count += 1
                if count > MAX_LINES:
                    raise SourceError("Pull chunk exceeds 500,000 rows.")
                headers.update(row)
                if len(headers) > 2048:
                    raise SourceError("Feed exceeds 2,048 distinct fields.")
                raw.write(json.dumps(row, sort_keys=True, default=json_default) + "\n")
        if not count:
            return None
        headers = sorted(headers)
        mapping = None if record_type == "resource" else mapping_for(headers, record_type, source_key)
        fd, csv_path = tempfile.mkstemp(prefix="pull-", suffix=".csv", dir=directory)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as out, open(raw_path, encoding="utf-8") as raw:
                writer = csv.DictWriter(out, fieldnames=headers)
                writer.writeheader()
                for line in raw:
                    row = {k: csv_value(v) for k, v in json.loads(line).items()}
                    # Reject before any commits. Never mark a partly rejected
                    # feed successful or advance a cursor past malformed rows.
                    if mapping and map_and_validate_row(row, mapping["columns"], record_type, mapping["options"])["errors"]:
                        raise SourceError("Feed contains invalid canonical rows; use dry-run and repair the source.")
                    writer.writerow(row)
            digest = hashlib.sha256()
            with open(csv_path, "rb") as content:
                for block in iter(lambda: content.read(1024 * 1024), b""):
                    digest.update(block)
            key = digest.hexdigest()
            final = directory / f"pull-{key}.csv"
            os.replace(csv_path, final)
            children = []
            with open(enrich_path, encoding="utf-8") as enriched:
                iterator = (json.loads(line) for line in enriched)
                while (first := next(iterator, None)) is not None:
                    child = spool(itertools.chain([first], itertools.islice(iterator, MAX_LINES - 1)),
                                  "enrichment", source_key)
                    if child:
                        children.append(child)
            with open(gift_path, encoding="utf-8") as gifts:
                iterator = (json.loads(line) for line in gifts)
                while (first := next(iterator, None)) is not None:
                    child = spool(itertools.chain([first], itertools.islice(iterator, MAX_LINES - 1)),
                                  "_gifts", source_key)
                    if child:
                        children.append(child)
            return {"path": str(final), "digest": key, "record_type": resource_type or record_type,
                    "count": count, "mapping": mapping, "children": children}
        except BaseException:
            Path(csv_path).unlink(missing_ok=True)
            raise
    finally:
        Path(raw_path).unlink(missing_ok=True)
        Path(enrich_path).unlink(missing_ok=True)
        Path(gift_path).unlink(missing_ok=True)


def gift_for_event(row):
    """A return is a signed ledger entry, never a second positive donation.

    Stable namespaced external IDs make replay/upsert idempotent; original gift
    identifiers remain evidence when supplied, without inventing associations.
    """
    if row.get("name") not in {"donation", "donation_return"}:
        return None
    props = row["properties"]
    gift = {key: row[key] for key in ("contact_external_id", "email", "phone") if row.get(key)}
    gift.update({"external_id": f"{row['name']}:{row['external_id']}",
                 "amount": props.get("amount"), "fund": props.get("fund"),
                 "source_code": props.get("source_code"),
                 "gift_date": str(row.get("occurred_at", ""))[:10],
                 "is_refund": row["name"] == "donation_return",
                 "original_donation_id": props.get("original_donation_id")})
    return gift


def split_enrichment(row, feed):
    """Exit rows may carry licensed enrichment envelopes beside properties."""
    if feed not in {"people", "models", "lookups"}:
        return row, []
    row = dict(row)
    extras = row.pop("enrichment", [])
    if isinstance(extras, dict):
        extras = [extras]
    if not isinstance(extras, list) or len(extras) > 100 or any(not isinstance(e, dict) for e in extras):
        raise SourceError("Appended enrichment must contain licensed enrichment objects.")
    return row, [dict(extra) for extra in extras]


def connection_row(db, source_id):
    row = db.execute(text("""SELECT s.id,s.key,s.kind,s.is_active,c.config,c.credentials,c.state
        FROM sources s JOIN source_connections c ON c.source_id=s.id WHERE s.id=:id"""),
                     {"id": source_id}).mappings().first()
    if not row:
        raise SourceError("Source connection is not configured.")
    if not row["is_active"]:
        raise SourceError("Source is inactive.")
    validate_config(row["kind"], row["config"])
    return row


def collect(row, feed=None, dry_run=False):
    credentials = security.decrypt(row["credentials"])
    mappings = [m for m in row["config"]["mappings"] if feed is None or m["feed"] == feed]
    manifests, previews, cursors = [], [], dict(row["state"].get("cursors", {}))
    connection = snowflake_connection if row["kind"] == "snowflake" else sftp_connection
    with connection(credentials) as remote:
        for mapping in mappings:
            def consume(rows):
                if dry_run:
                    samples = []
                    for item in itertools.islice(rows, 100):
                        item, extras = split_enrichment(item, mapping["feed"])
                        record_type, normalized = canonical_row(item, mapping["feed"])
                        for extra in extras:
                            extra["contact_external_id"] = normalized["external_id"]
                            canonical_row(extra, "enrichment")
                        samples.append((record_type, normalized))
                    # Counts/field names only. No contact data, credentials or
                    # sensitive enrichment values are returned to the browser.
                    headers = sorted({key for _, item in samples for key in item})
                    errors = 0
                    if samples and samples[0][0] != "resource":
                        mapped = mapping_for(headers, samples[0][0], row["key"])
                        for kind, item in samples:
                            invalid = bool(map_and_validate_row(
                                {k: csv_value(v) for k, v in item.items()}, mapped["columns"],
                                kind, mapped["options"])["errors"])
                            gift = gift_for_event(item) if kind == "event" else None
                            if gift:
                                gift_mapping = mapping_for(list(gift), "gift", row["key"])
                                invalid |= bool(map_and_validate_row(
                                    {k: csv_value(v) for k, v in gift.items()}, gift_mapping["columns"],
                                    "gift", gift_mapping["options"])["errors"])
                            errors += int(invalid)
                    previews.append({"mapping_id": mapping["id"], "rows_checked": len(samples),
                                     "fields": headers, "rows_rejected": errors})
                    return
                iterator = iter(rows)
                while True:
                    first = next(iterator, None)
                    if first is None:
                        break
                    chunk = itertools.chain([first], itertools.islice(iterator, MAX_LINES - 1))
                    def track():
                        for item in chunk:
                            cursor = mapping.get("cursor_column")
                            if cursor:
                                value = item.get(cursor.lower())
                                if value is None:
                                    raise SourceError("Snowflake incremental cursor cannot be null.")
                                cursors[mapping["id"]] = json_default(value)
                            yield item
                    manifest = spool(track(), mapping["feed"], row["key"], mapping.get("resource_type"))
                    if manifest:
                        manifests.extend(manifest.pop("children"))
                        manifests.append(manifest)
            if row["kind"] == "snowflake":
                consume(snowflake_rows(remote, mapping, cursors.get(mapping["id"]), 100 if dry_run else None))
            else:
                for path, before in remote_files(remote, mapping):
                    with remote.open(path, "rb") as stream:
                        consume(jsonl_rows(stream))
                    after = remote.lstat(path)
                    if before.st_size != after.st_size or before.st_mtime != after.st_mtime:
                        raise SourceError("SFTP file changed during read; publish files with atomic rename.")
                    if dry_run:
                        break
    return previews if dry_run else (manifests, cursors)


def resource_import(db, manifest):
    table = manifest["record_type"]
    if table not in RESOURCES:
        raise SourceError("Unsupported resource type.")
    with open(manifest["path"], newline="", encoding="utf-8") as content:
        iterator = csv.DictReader(content)
        while rows := list(itertools.islice(iterator, 5000)):
            records = {}
            for row in rows:
                attributes = json.loads(row.pop("attributes", "") or "{}")
                attributes.update({k: v for k, v in row.items() if k not in {"key", "name", "series"} and v != ""})
                records[row["key"]] = {"key": row["key"], "name": row.get("name"),
                                       "series": row.get("series"), "attributes": json.dumps(attributes)}
            copy_upsert(db, f"""INSERT INTO {table} (key,name,series,attributes)
                SELECT :key,:name,:series,:attributes FROM {{stage}} WHERE true
                ON CONFLICT(key) DO UPDATE SET name=EXCLUDED.name,series=EXCLUDED.series,
                attributes=EXCLUDED.attributes,updated_at=now()""",
                        list(records.values()), "key text,name text,series text,attributes jsonb")
            db.commit()


@contextmanager
def source_lock(source_id):
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    key = f"source-pull:{source_id}"
    acquired = False
    try:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": key})
        if not acquired:
            from app.identity.locking import IdentityResolutionDeferred
            raise IdentityResolutionDeferred("Source pull is already running.")
        yield
    finally:
        if acquired:
            connection.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": key})
        connection.close()


def save_state(db, source_id, state):
    db.execute(text("UPDATE source_connections SET state=CAST(:state AS jsonb),updated_at=now() WHERE source_id=:id"),
               {"id": source_id, "state": json.dumps(state, default=json_default)})
    db.commit()


def run_source(source_id, feed=None, job_id=None):
    with source_lock(source_id), Session(engine) as db:
        row = connection_row(db, source_id)
        state = dict(row["state"])
        pending = state.get("pending")
        if not pending:
            manifests, cursors = collect(row, feed)
            manifests.sort(key=lambda m: 0 if m["record_type"] in RESOURCES else
                           1 if m["record_type"] == "contact" else 2)
            batch_id = db.scalar(text("INSERT INTO identity_batches DEFAULT VALUES RETURNING id"))
            pending = {"batch_id": batch_id, "manifests": manifests, "cursors": cursors, "feed": feed}
            state["pending"] = pending
            save_state(db, source_id, state)
        batch_id = pending["batch_id"]
        if job_id is not None:
            from app.jobs.queue import heartbeat
            if not heartbeat(db, job_id, {"batch_id": batch_id}):
                from app.jobs.lifecycle import OwnershipLost
                raise OwnershipLost("Source pull no longer owns its job.")
            db.commit()
        # Register every member before allowing identity work. A replay reuses
        # receipt/import IDs rather than creating duplicate partial imports.
        for manifest in pending["manifests"]:
            params = {"source": source_id, "digest": manifest["digest"], "type": manifest["record_type"]}
            receipt = db.execute(text("""SELECT import_id,completed FROM source_import_receipts
                WHERE source_id=:source AND digest=:digest AND record_type=:type"""), params).mappings().first()
            if receipt:
                continue
            import_id = None
            if manifest["record_type"] not in RESOURCES:
                import_id = db.scalar(text("""INSERT INTO imports
                    (source_id,filename,file_path,file_sha256,record_type,mapping,status,rows_total,
                     rows_ok,rows_rejected,identity_batch_id)
                    VALUES (:source,'source-pull.csv',:path,:digest,:type,CAST(:mapping AS jsonb),
                            'running',:count,0,0,:batch) RETURNING id"""),
                    {**params, **manifest, "mapping": json.dumps(manifest["mapping"]), "batch": batch_id})
            db.execute(text("""INSERT INTO source_import_receipts(source_id,digest,record_type,import_id)
                VALUES (:source,:digest,:type,:import_id)"""), {**params, "import_id": import_id})
            db.commit()
        for manifest in pending["manifests"]:
            params = {"source": source_id, "digest": manifest["digest"], "type": manifest["record_type"]}
            receipt = db.execute(text("""SELECT import_id,completed FROM source_import_receipts
                WHERE source_id=:source AND digest=:digest AND record_type=:type"""), params).mappings().one()
            if receipt["completed"]:
                continue
            if receipt["import_id"]:
                result = run_import(receipt["import_id"], db=db)
                if result["rows_rejected"]:
                    raise SourceError("Source import rejected rows; cursor remains unchanged.")
            else:
                resource_import(db, manifest)
            db.execute(text("""UPDATE source_import_receipts SET completed=true
                WHERE source_id=:source AND digest=:digest AND record_type=:type"""), params)
            db.commit()
        db.execute(text("UPDATE identity_batches SET phase='importing' WHERE id=:id"), {"id": batch_id})
        enqueue(db, "identity.resolve_batch", {"batch_id": batch_id}, dedupe_key=f"identity:batch:{batch_id}")
        state["cursors"] = pending["cursors"]
        state.pop("pending", None)
        save_state(db, source_id, state)
        result = {"batch_id": batch_id, "files": len(pending["manifests"])}
    # Recover an older poll first, then honor this job's distinct schedule.
    if pending.get("feed") != feed:
        return run_source(source_id, feed=feed, job_id=job_id)
    return result


def tick_sources(db, now):
    from app.jobs.scheduler import schedule_once
    for row in db.execute(text("""SELECT s.id,c.config FROM sources s
        JOIN source_connections c ON c.source_id=s.id WHERE s.is_active""")).mappings():
        for feed in {m["feed"] for m in row["config"]["mappings"]}:
            schedule = row["config"].get("schedules", {}).get(feed, DEFAULT_SCHEDULES[feed])
            key = window(schedule, now)
            if key:
                schedule_once(db, f"source:{row['id']}:{feed}", key, "source.pull",
                              {"source_id": row["id"], "feed": feed})
