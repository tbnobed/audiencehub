"""Admin-only resumable browser uploads, SFTP selection and migration status."""
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Header
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.activations import security
from app.auth.deps import require_role
from app.db import session_scope
from app.models import User, AuditLog
from app.source_connectors import SourceError, sftp_connection
from app.zeta_files import CHUNK_BYTES, checked_path, remote_listing
from app.zeta_migrations import (
    catalog, create_session, session_view, migration, begin_file,
    validate_counts, enqueue_migration,
)

router = APIRouter(prefix="/api/zeta-migrations", tags=["zeta-migrations"])
jobs_router = APIRouter()


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: int
    name: str = Field(default="Zeta exit migration", min_length=1, max_length=200)
    audience_counts: dict[str, StrictInt]


class FileStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str
    size: int = Field(ge=0, le=2*1024**4)
    fingerprint: str = Field(default="", max_length=256)
    target_type: str = "auto"


class Counts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience_counts: dict[str, StrictInt]


class RemoteSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(min_length=1, max_length=500)


def input_error(error):
    return HTTPException(422, str(error))


@router.get("/meta")
def meta(user: User = Depends(require_role("admin"))):
    return {"audiences": [{"name": name, "zeta_count": count} for name, count in catalog().items()]}


@router.get("")
def sessions(user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    ids = db.scalars(text("SELECT id FROM zeta_migrations ORDER BY id DESC LIMIT 50"))
    return {"items": [session_view(db, item) for item in ids]}


@router.post("", status_code=201)
def create(data: NewSession, user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    try:
        key = create_session(db, data.source_id, data.audience_counts, data.name, user.id)
        db.add(AuditLog(user_id=user.id, actor_type="user", action="zeta.migration.create",
                        entity_type="source", entity_id=str(data.source_id), details={"migration_id": key}))
        db.commit()
        return session_view(db, key)
    except SourceError as error:
        raise input_error(error) from None


@router.get("/{migration_id}")
def detail(migration_id: int, user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    try:
        return session_view(db, migration_id)
    except SourceError:
        raise HTTPException(404, "Migration not found") from None


@router.post("/{migration_id}/files", status_code=201)
def start_file(migration_id: int, data: FileStart, user: User = Depends(require_role("admin")),
               db: Session = Depends(session_scope)):
    try:
        file = begin_file(db, migration_id, data.filename, data.size, data.fingerprint, data.target_type)
        return {"id": file["id"], "filename": file["filename"], "size": file["size"],
                "offset": file["offset_bytes"], "status": file["status"],
                "target_type": file["target_type"], "fingerprint": file["fingerprint"]}
    except SourceError as error:
        raise input_error(error) from None


@router.patch("/{migration_id}/files/{file_id}")
async def upload_chunk(migration_id: int, file_id: int, request: Request,
                       upload_offset: int = Header(alias="Upload-Offset"),
                       user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    try:
        from app.privacy.locking import privacy_shared
        privacy_shared(db)
        session = migration(db, migration_id, lock=True)
        if session["status"] != "draft":
            raise SourceError("Files are immutable once import starts.")
        file = db.execute(text("""SELECT * FROM zeta_migration_files
            WHERE id=:file AND migration_id=:session FOR UPDATE"""),
                          {"file": file_id, "session": migration_id}).mappings().first()
        if not file:
            raise HTTPException(404, "Upload not found")
        if upload_offset != file["offset_bytes"]:
            raise HTTPException(409, "Offset changed; refresh the migration and resume at its saved offset")
        if file["status"] in {"revoked", "expired"}:
            raise SourceError("Upload was revoked by privacy or retention policy. Start a new session.")
        if file["status"] != "uploading":
            return {"id": file_id, "offset": file["offset_bytes"], "status": file["status"]}
        path, received = checked_path(file["file_path"]), 0
        with path.open("r+b") as out:
            # Recover a lost response/connection: uncommitted filesystem bytes
            # are discarded using the durable DB offset, never guessed.
            out.truncate(upload_offset)
            out.seek(upload_offset)
            async for chunk in request.stream():
                received += len(chunk)
                if received > CHUNK_BYTES or upload_offset + received > file["size"]:
                    out.truncate(upload_offset)
                    raise HTTPException(413, "Chunk exceeds 8 MiB or the declared file size")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        offset = upload_offset + received
        status = "uploaded" if offset == file["size"] else "uploading"
        db.execute(text("UPDATE zeta_migration_files SET offset_bytes=:offset,status=:status WHERE id=:id"),
                   {"id": file_id, "offset": offset, "status": status})
        db.commit()
        return {"id": file_id, "offset": offset, "status": status}
    except SourceError as error:
        raise input_error(error) from None


@router.post("/{migration_id}/run", status_code=202)
def run(migration_id: int, user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    try:
        session = session_view(db, migration_id)
        if session["batch_id"]:
            return enqueue_migration(db, migration_id, parity=True)
        files = session["files"]
        if not files or any(file["offset"] != file["size"] or file["status"] not in {"uploaded", "prepared"} for file in files):
            raise SourceError("Complete all uploads before starting.")
        return enqueue_migration(db, migration_id)
    except SourceError as error:
        raise input_error(error) from None


@router.get("/{migration_id}/drop-files")
def drop_files(migration_id: int, user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    try:
        row = migration(db, migration_id)
        encrypted = db.scalar(text("SELECT credentials FROM source_connections WHERE source_id=:id"),
                              {"id": row["source_id"]})
        with sftp_connection(security.decrypt(encrypted)) as remote:
            return {"items": remote_listing(remote)}
    except SourceError as error:
        raise input_error(error) from None
    except Exception:
        raise HTTPException(422, "Cannot list the SFTP drop; configure its credentials, pinned host key and folders in Sources") from None


@router.post("/{migration_id}/sftp", status_code=202)
def start_sftp(migration_id: int, data: RemoteSelection,
               user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    try:
        from app.zeta_files import DROP_FOLDERS, filename
        for path in data.paths:
            if not any(path.startswith(folder) and "/" not in path[len(folder):] for folder in DROP_FOLDERS):
                raise SourceError("Select only files from the configured Zeta drop folders.")
            filename(path.rsplit("/", 1)[-1])
        return enqueue_migration(db, migration_id, paths=list(dict.fromkeys(data.paths)))
    except SourceError as error:
        raise input_error(error) from None


@router.post("/{migration_id}/parity", status_code=202)
def parity(migration_id: int, data: Counts, user: User = Depends(require_role("admin")),
           db: Session = Depends(session_scope)):
    try:
        validate_counts(data.audience_counts)
        row = migration(db, migration_id, lock=True)
        if not row["batch_id"]:
            raise SourceError("Run the migration before requesting parity.")
        db.execute(text("UPDATE zeta_migrations SET audience_counts=CAST(:counts AS jsonb) WHERE id=:id"),
                   {"id": migration_id, "counts": json.dumps(data.audience_counts)})
        return enqueue_migration(db, migration_id, parity=True)
    except SourceError as error:
        raise input_error(error) from None


@jobs_router.get("/api/source-jobs")
def source_jobs(user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    rows = db.execute(text("""SELECT j.id AS job_id,s.id AS source_id,s.name AS source_name,
        j.status,j.created_at,j.started_at,j.finished_at,j.progress,j.error IS NOT NULL AS has_error,
        c.imports_total,c.imports_completed,c.rows_total,c.rows_ok,c.rows_rejected
        FROM jobs j JOIN sources s ON s.id::text=j.payload->>'source_id'
        LEFT JOIN LATERAL (
          SELECT count(*) AS imports_total,count(*) FILTER(WHERE status='completed') AS imports_completed,
            COALESCE(sum(rows_total),0) AS rows_total,COALESCE(sum(rows_ok),0) AS rows_ok,
            COALESCE(sum(rows_rejected),0) AS rows_rejected FROM imports
          WHERE identity_batch_id::text=j.progress->>'batch_id' AND source_id=s.id
        ) c ON true
        WHERE j.type='source.pull' ORDER BY j.id DESC LIMIT 50""")).mappings()
    items = []
    for row in rows:
        item = {key: row[key] for key in ("job_id", "source_id", "source_name", "status",
                                         "created_at", "started_at", "finished_at")}
        progress = row["progress"] or {}
        item.update(phase=progress.get("phase", "importing" if progress.get("batch_id") else "queued"),
                    rows_read=int(progress.get("rows_read", 0)),
                    counts={key: int(row[key]) for key in
                            ("imports_total", "imports_completed", "rows_total", "rows_ok", "rows_rejected")},
                    error="Source pull failed. Review source configuration and retry." if row["has_error"] else None)
        items.append(item)
    return {"items": items}
