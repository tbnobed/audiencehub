"""Durable exit-migration jobs, using the existing COPY and identity barriers."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text, select, func
from sqlalchemy.orm import Session

from app.activations import security
from app.db import engine
from app.jobs.queue import enqueue, heartbeat
from app.source_connectors import SourceError, sftp_connection
from app.source_pulls import run_source, source_lock
from app.zeta_files import CHUNK_BYTES, checked_path, prepare_file, remote_listing, remote_fingerprint


def catalog():
    path = Path(__file__).resolve().parents[2] / "docs" / "ZETA_AUDIENCE_TARGETS.json"
    return json.loads(path.read_text())["audiences"]


def validate_counts(counts):
    if set(counts) != set(catalog()) or any(type(value) is not int or value < 0 for value in counts.values()):
        raise SourceError("Enter a nonnegative integer Zeta count for each of the 23 audiences.")
    return counts


def migration(db, migration_id, *, lock=False):
    row = db.execute(text("SELECT * FROM zeta_migrations WHERE id=:id" + (" FOR UPDATE" if lock else "")),
                     {"id": migration_id}).mappings().first()
    if not row:
        raise SourceError("Migration not found.")
    return row


def session_view(db, migration_id):
    result = dict(migration(db, migration_id))
    result["files"] = [dict(row) for row in db.execute(text("""
        SELECT id,filename,size,offset_bytes AS offset,fingerprint,target_type,status
        FROM zeta_migration_files WHERE migration_id=:id ORDER BY id
    """), {"id": migration_id}).mappings()]
    return result


def create_session(db, source_id, counts, name="Zeta exit migration", user_id=None):
    validate_counts(counts)
    row = db.execute(text("SELECT id,kind,is_active FROM sources WHERE id=:id"),
                     {"id": source_id}).mappings().first()
    if not row or not row["is_active"] or row["kind"] not in {"zeta_exit", "sftp_jsonl"}:
        raise SourceError("Select an active Zeta exit or SFTP source.")
    # Browser-only sessions do not require remote credentials. Empty encrypted
    # credentials cannot establish an SFTP connection and are never advertised
    # as saved key material.
    db.execute(text("""INSERT INTO source_connections(source_id,config,credentials)
        VALUES (:id,CAST(:config AS jsonb),:credentials) ON CONFLICT(source_id) DO NOTHING"""),
               {"id": source_id, "config": json.dumps({"mappings": [], "schedules": {}}),
                "credentials": security.encrypt({})})
    result = db.scalar(text("""INSERT INTO zeta_migrations(source_id,name,audience_counts,created_by)
        VALUES (:source,:name,CAST(:counts AS jsonb),:user) RETURNING id"""),
                       {"source": source_id, "name": name, "counts": json.dumps(counts), "user": user_id})
    db.commit()
    return result


def begin_file(db, migration_id, name, size, fingerprint="", target="auto", *, remote_path=None, worker=False):
    from app.zeta_files import filename, TARGETS, upload_root
    from app.privacy.locking import privacy_shared
    privacy_shared(db)
    name = filename(name)
    if target not in TARGETS or not 0 <= size <= 2 * 1024**4 or len(fingerprint) > 256:
        raise SourceError("Invalid export size, fingerprint or target type.")
    session = migration(db, migration_id, lock=True)
    old = db.execute(text("""SELECT * FROM zeta_migration_files
        WHERE migration_id=:id AND filename=:name AND fingerprint=:fingerprint FOR UPDATE"""),
                     {"id": migration_id, "name": name, "fingerprint": fingerprint}).mappings().first()
    if old:
        if old["status"] in {"revoked", "expired"}:
            raise SourceError("This upload was revoked by privacy or retention policy. Create a new session.")
        if old["size"] != size or (target != "auto" and old["target_type"] not in ("auto", target)):
            raise SourceError("Resume requires the same immutable file, size and target type.")
        db.commit()
        return dict(old)
    if not worker and session["status"] != "draft":
        raise SourceError("Files are frozen after migration starts. Create a new session for replacement exports.")
    path = upload_root() / f"zeta-{uuid4().hex}-{name}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        file_id = db.scalar(text("""INSERT INTO zeta_migration_files
            (migration_id,filename,size,fingerprint,target_type,file_path,remote_path,status)
            VALUES (:session,:name,:size,:fingerprint,:target,:path,:remote,:status) RETURNING id"""),
                            {"session": migration_id, "name": name, "size": size, "fingerprint": fingerprint,
                             "target": target, "path": str(path), "remote": remote_path,
                             "status": "uploading" if size else "uploaded"})
        db.commit()
        return dict(db.execute(text("SELECT * FROM zeta_migration_files WHERE id=:id"), {"id": file_id}).mappings().one())
    except BaseException:
        db.rollback()
        path.unlink(missing_ok=True)
        raise


def progress(migration_id, job_id, phase, done=0, total=0):
    with Session(engine) as db:
        data = {"phase": phase, "done": done, "total": total}
        if job_id is not None and not heartbeat(db, job_id, data):
            from app.jobs.lifecycle import OwnershipLost
            raise OwnershipLost("Migration job no longer owns its work.")
        db.execute(text("""UPDATE zeta_migrations SET progress=CAST(:data AS jsonb),
            updated_at=now() WHERE id=:id"""), {"id": migration_id, "data": json.dumps(data)})
        db.commit()


def enqueue_migration(db, migration_id, *, parity=False, paths=None):
    row = migration(db, migration_id, lock=True)
    if row["job_id"]:
        active = db.scalar(text("SELECT status FROM jobs WHERE id=:id"), {"id": row["job_id"]})
        if active in {"queued", "running"}:
            raise SourceError("This migration already has a queued or running job.")
    kind = "zeta.parity" if parity else "zeta.import"
    job = enqueue(db, kind, {"migration_id": migration_id, "paths": paths},
                  dedupe_key=f"{kind}:{migration_id}")
    db.execute(text("""UPDATE zeta_migrations SET job_id=:job,status=:status,error=NULL,
        parity=CASE WHEN :parity THEN '{"status":"queued"}'::jsonb ELSE parity END,
        updated_at=now() WHERE id=:id"""),
               {"job": job.id, "status": "processing" if parity else "queued",
                "parity": parity, "id": migration_id})
    db.commit()
    return {"job_id": job.id, "status": "queued"}


def fetch_remote_files(migration_id, job_id, paths):
    with Session(engine) as db:
        session = migration(db, migration_id)
        encrypted = db.scalar(text("SELECT credentials FROM source_connections WHERE source_id=:id"),
                              {"id": session["source_id"]})
    with sftp_connection(security.decrypt(encrypted)) as remote:
        available = {item["path"]: item for item in remote_listing(remote)}
        if any(path not in available for path in paths):
            raise SourceError("Selected SFTP file is missing, unsafe or no longer available.")
        for number, remote_path in enumerate(paths):
            item = available[remote_path]
            with Session(engine) as db:
                file = begin_file(db, migration_id, Path(remote_path).name, item["size"],
                                  remote_fingerprint(item), "auto", remote_path=remote_path, worker=True)
            local = checked_path(file["file_path"])
            offset = file["offset_bytes"]
            with remote.open(remote_path, "rb") as incoming, open(local, "r+b") as out:
                incoming.seek(offset)
                out.truncate(offset)
                out.seek(offset)
                while chunk := incoming.read(CHUNK_BYTES):
                    out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                    offset += len(chunk)
                    if offset > item["size"]:
                        raise SourceError("SFTP file changed while downloading; retry with the original immutable export.")
                    with Session(engine) as db:
                        db.execute(text("UPDATE zeta_migration_files SET offset_bytes=:offset WHERE id=:id"),
                                   {"offset": offset, "id": file["id"]})
                        db.commit()
                    progress(migration_id, job_id, "downloading", offset, item["size"])
            after = remote.lstat(remote_path)
            if after.st_size != item["size"] or after.st_mtime != item["modified"] or offset != item["size"]:
                raise SourceError("SFTP file changed during download. Publish immutable export files with atomic rename.")
            with Session(engine) as db:
                db.execute(text("UPDATE zeta_migration_files SET status='uploaded' WHERE id=:id"), {"id": file["id"]})
                db.commit()


def run_migration(migration_id, job_id=None, paths=None):
    from app.identity.locking import IdentityResolutionDeferred
    from app.jobs.lifecycle import OwnershipLost
    try:
        with source_lock(-migration_id):
            if paths:
                fetch_remote_files(migration_id, job_id, paths)
            with Session(engine) as db:
                row = migration(db, migration_id)
                source_key = db.scalar(text("SELECT key FROM sources WHERE id=:id"), {"id": row["source_id"]})
                files = [dict(item) for item in db.execute(text(
                    "SELECT * FROM zeta_migration_files WHERE migration_id=:id ORDER BY id"),
                    {"id": migration_id}).mappings()]
                if not files or any(file["offset_bytes"] != file["size"] for file in files):
                    raise SourceError("Upload every complete export file before starting the migration.")
                db.execute(text("UPDATE zeta_migrations SET status='importing',error=NULL WHERE id=:id"),
                           {"id": migration_id})
                db.commit()
            manifests, last_pulse = [], [0.0]
            for index, file in enumerate(files):
                def pulse():
                    if time.monotonic() - last_pulse[0] >= 10:
                        progress(migration_id, job_id, "preparing", index, len(files))
                        last_pulse[0] = time.monotonic()
                def save_chunk(items, chunks, target):
                    with Session(engine) as db:
                        db.execute(text("""UPDATE zeta_migration_files SET manifests=CAST(:items AS jsonb),
                            prepared_chunks=:chunks,target_type=:target WHERE id=:id"""),
                                   {"id": file["id"], "items": json.dumps(items), "chunks": chunks, "target": target})
                        db.commit()
                    progress(migration_id, job_id, "preparing", index, len(files))
                manifests.extend(prepare_file(file, source_key, save_chunk, pulse))
                with Session(engine) as db:
                    db.execute(text("UPDATE zeta_migration_files SET status='prepared' WHERE id=:id"), {"id": file["id"]})
                    db.commit()
            progress(migration_id, job_id, "importing", 0, sum(m["count"] for m in manifests))
            result = run_source(row["source_id"], mapping_id=f"migration_{migration_id}",
                                manifests=manifests, job_id=job_id)
            with Session(engine) as db:
                db.execute(text("""UPDATE zeta_migrations SET batch_id=:batch,status='processing',
                    parity='{"status":"waiting"}'::jsonb,updated_at=now() WHERE id=:id"""),
                           {"batch": result["batch_id"], "id": migration_id})
                parity_job = enqueue(db, "zeta.parity", {"migration_id": migration_id}, dedupe_key=f"zeta.parity:{migration_id}")
                db.execute(text("UPDATE zeta_migrations SET job_id=:job WHERE id=:id"),
                           {"job": parity_job.id, "id": migration_id})
                db.commit()
    except (IdentityResolutionDeferred, OwnershipLost):
        raise
    except Exception as error:
        with Session(engine) as db:
            message = (str(error)[:500] if isinstance(error, SourceError)
                       else "Migration failed. Check file type, required Zeta identities and connection settings; retry resumes committed checkpoints.")
            db.execute(text("""UPDATE zeta_migrations SET status='failed',
                error=:error,
                updated_at=now() WHERE id=:id"""), {"id": migration_id, "error": message})
            # SourceError text is from our sanitized connector/parser messages,
            # never raw vendor exceptions or exported row contents.
            db.commit()
        raise SourceError("Zeta migration failed; retry resumes immutable uploads and committed import checkpoints.") from None


def run_parity(migration_id, job_id=None):
    from app.identity.locking import IdentityResolutionDeferred
    from app.jobs.lifecycle import OwnershipLost
    from app.models import Segment
    from app.segments.compiler import compile_definition
    from app.segments.templates import template_definitions
    try:
        with Session(engine) as control:
            row = migration(control, migration_id)
            phase = control.scalar(text("SELECT phase FROM identity_batches WHERE id=:id"), {"id": row["batch_id"]})
            traits_complete = control.scalar(text("""SELECT EXISTS (
                SELECT 1 FROM jobs WHERE type='traits.recompute' AND status='succeeded'
                  AND payload->>'batch_id'=:batch)"""), {"batch": str(row["batch_id"])})
            if not row["batch_id"] or phase != "completed" or not traits_complete:
                raise IdentityResolutionDeferred("Parity waits for migration identity, attachment, traits and rollups.")
            counts = validate_counts(row["audience_counts"])
        reference, items = datetime.now(timezone.utc).date(), []
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with Session(connection) as db:
                for index, template in enumerate(template_definitions()):
                    saved = db.scalar(select(Segment).where(Segment.template_key == template["key"]))
                    if saved is None or template["name"] not in counts:
                        raise SourceError("The audited 23-audience catalog is not installed.")
                    query = compile_definition(db, saved.definition, reference)
                    actual = db.scalar(select(func.count()).select_from(query.subquery()))
                    expected = counts[template["name"]]
                    difference = actual - expected
                    items.append({"name": template["name"], "kinship_count": actual, "zeta_count": expected,
                                  "difference": difference, "percentage": difference * 100 / expected if expected else None})
                    progress(migration_id, job_id, "parity", index + 1, 23)
        if len(items) != 23:
            raise SourceError("Parity requires all 23 audited audiences.")
        with Session(engine) as db:
            report = {"status": "completed", "items": items, "as_of": str(reference),
                      "scope": "All active Kinship profiles; repeatable-read snapshot; current saved template definitions"}
            db.execute(text("""UPDATE zeta_migrations SET parity=CAST(:report AS jsonb),status='completed',
                error=NULL,updated_at=now() WHERE id=:id"""), {"id": migration_id, "report": json.dumps(report)})
            db.commit()
    except (IdentityResolutionDeferred, OwnershipLost):
        raise
    except Exception:
        with Session(engine) as db:
            db.execute(text("""UPDATE zeta_migrations SET status='failed',
                parity='{"status":"failed","error":"Parity failed; verify templates and completed identity/traits jobs."}'::jsonb
                WHERE id=:id"""), {"id": migration_id})
            db.commit()
        raise SourceError("Zeta parity failed; check the 23 saved audiences and completed pipeline jobs.") from None
