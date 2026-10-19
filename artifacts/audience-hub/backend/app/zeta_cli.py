"""Server equivalents for the admin source and migration screens."""
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import engine
from app.jobs.queue import enqueue
from app.models import Source
from app.source_api import ConnectionTest, test_connection
from app.source_connectors import SourceError
from app.source_pulls import collect, connection_row, json_default
from app.zeta_files import CHUNK_BYTES, checked_path
from app.zeta_migrations import begin_file, catalog, create_session, enqueue_migration, session_view


def sources_command(command, source_id, mapping=None):
    with Session(engine) as db:
        if command == "test":
            return test_connection(source_id, ConnectionTest(), user=None, db=db)
        row = connection_row(db, source_id)
        if row["kind"] == "hubspot":
            if command == "preview":
                from app.hubspot_source import preview
                return preview(row)
            mapping = mapping or "contacts"
        if mapping not in {m["id"] for m in row["config"]["mappings"]}:
            raise SourceError("Mapping not found.")
        if command == "preview":
            return {"items": collect(row, dry_run=True, mapping_id=mapping)}
        job = enqueue(db, "source.pull", {"source_id": source_id, "mapping_id": mapping},
                      dedupe_key=f"source:{source_id}:{mapping}")
        db.commit()
        return {"job_id": job.id, "status": job.status}


def import_folder(folder, source_id=None, counts=None):
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise SourceError("Export folder does not exist.")
    files = sorted(path for path in folder.rglob("*") if path.is_file() and not path.is_symlink()
                   and path.suffix.lower() in {".csv", ".jsonl", ".ndjson"})
    if not files:
        raise SourceError("Folder contains no CSV or JSONL exports.")
    inventory = [(str(path.relative_to(folder)), path.stat().st_size, path.stat().st_mtime_ns) for path in files]
    name = "Zeta folder " + hashlib.sha256(json.dumps(inventory).encode()).hexdigest()[:20]
    with Session(engine) as db:
        if source_id is None:
            source = db.scalar(select(Source).where(Source.key == "zeta_exit_migration"))
            if source is None:
                source = Source(key="zeta_exit_migration", name="Zeta exit migration", kind="zeta_exit",
                                record_types=["contact", "event", "gift", "resource", "enrichment"],
                                priority=50, settings={}, is_active=True)
                db.add(source)
                db.commit()
                db.refresh(source)
            source_id = source.id
        previous = db.scalar(text("SELECT id FROM zeta_migrations WHERE source_id=:source AND name=:name ORDER BY id DESC LIMIT 1"),
                             {"source": source_id, "name": name})
        key = previous or create_session(db, source_id, counts if counts is not None else catalog(), name)
        current = session_view(db, key)
        if current["status"] == "failed" and current["batch_id"]:
            return {"migration_id": key, **enqueue_migration(db, key, parity=True)}
        if current["status"] not in {"draft", "failed"}:
            return current
        for incoming_path in files:
            info = incoming_path.stat()
            fingerprint = hashlib.sha256(f"{incoming_path}:{info.st_size}:{info.st_mtime_ns}".encode()).hexdigest()
            file = begin_file(db, key, incoming_path.name, info.st_size, fingerprint)
            offset = file["offset_bytes"]
            with incoming_path.open("rb") as incoming, checked_path(file["file_path"]).open("r+b") as output:
                incoming.seek(offset)
                output.truncate(offset)
                output.seek(offset)
                while block := incoming.read(CHUNK_BYTES):
                    output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                    offset += len(block)
                    db.execute(text("UPDATE zeta_migration_files SET offset_bytes=:offset WHERE id=:id"),
                               {"id": file["id"], "offset": offset})
                    db.commit()
            after = incoming_path.stat()
            if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                raise SourceError("Local export changed during snapshot. Keep exit files immutable.")
            db.execute(text("UPDATE zeta_migration_files SET status='uploaded' WHERE id=:id"), {"id": file["id"]})
            db.commit()
        return {"migration_id": key, **enqueue_migration(db, key)}


def print_result(value):
    print(json.dumps(value, indent=2, default=json_default))
