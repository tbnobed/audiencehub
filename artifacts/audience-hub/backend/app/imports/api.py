"""Authenticated CSV import endpoints. Include ``router`` from app.main."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
import phonenumbers

from app.auth.deps import require_role
from app.config import get_settings
from app.db import session_scope
from app.imports.mapping import RECORD_TYPES, suggest_mapping, validate_mapping
from app.imports.service import (
    MAX_IMPORT_BYTES, ImportProblem, _warning_category, inspect_preview,
    safe_filename, validate_file,
)
from app.jobs.queue import enqueue
from app.models import User

router = APIRouter(prefix="/api/imports", tags=["imports"])


class MappingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: dict[str, Any]
    options: dict[str, Any] = Field(default_factory=dict)


def _http_input_error(error: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


def _get_import(db: Session, import_id: int):
    row = db.execute(text("""
        SELECT i.*, job.id AS job_id, job.status AS job_status, job.progress AS progress
        FROM imports AS i
        LEFT JOIN LATERAL (
          SELECT id, status, progress FROM jobs
          WHERE type='import.run' AND payload->>'import_id'=i.id::text
          ORDER BY id DESC LIMIT 1
        ) AS job ON true
        WHERE i.id=:id
    """), {"id": import_id}).mappings().first()
    if not row:
        raise HTTPException(404, detail="Import not found")
    return row


def _json_import(row) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
    result.pop("file_path", None)
    result.pop("error_file_path", None)
    return result


def _local_file(import_row, column: str) -> Path:
    path_string = import_row.get(column)
    if not path_string:
        raise HTTPException(404, detail="No error report is available")
    base = Path(get_settings().upload_dir).resolve()
    path = Path(path_string).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(404, detail="Import file is unavailable")
    return path


@router.post("")
async def upload_import(request: Request, user: User = Depends(require_role("analyst")),
                        db: Session = Depends(session_scope)):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_IMPORT_BYTES + 1024 * 1024:
                raise HTTPException(413, detail="Upload exceeds configured maximum size")
        except ValueError:
            raise HTTPException(400, detail="Invalid Content-Length")
    try:
        form = await request.form()
    except Exception as exc:
        raise HTTPException(400, detail="Invalid multipart form; CSV multipart support is required") from exc
    source_raw = form.get("source_id")
    record_type = str(form.get("record_type") or "")
    upload = form.get("file")
    if not source_raw or not record_type or upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, detail="Multipart fields source_id, record_type, and file are required")
    try:
        source_id = int(str(source_raw))
    except ValueError as exc:
        raise HTTPException(422, detail="source_id must be an integer") from exc
    if record_type not in RECORD_TYPES:
        raise HTTPException(422, detail=f"Unsupported record_type: {record_type}")
    source = db.execute(text(
        "SELECT id, record_types, is_active FROM sources WHERE id=:id"
    ), {"id": source_id}).mappings().first()
    if not source or not source["is_active"]:
        raise HTTPException(404, detail="Active source not found")
    if source["record_types"] and record_type not in source["record_types"]:
        raise HTTPException(422, detail="Source does not accept this record type")
    confirm = str(form.get("confirm_duplicate") or request.query_params.get("confirm_duplicate") or "").casefold() in {"true", "1", "yes"}
    try:
        filename = safe_filename(getattr(upload, "filename", None))
    except ImportProblem as exc:
        raise _http_input_error(exc) from exc

    settings = get_settings()
    directory = Path(settings.upload_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{uuid.uuid4().hex}-{filename}"
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_IMPORT_BYTES:
                    raise HTTPException(413, detail="Upload exceeds configured maximum size")
                digest.update(chunk)
                output.write(chunk)
        # Fully consume once to validate CSV structure and accurately persist the
        # file row count before exposing it as an import.
        row_count = 0
        from app.imports.service import csv_reader
        _, all_rows = csv_reader(destination)
        for _ in all_rows:
            row_count += 1
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except (ImportProblem, UnicodeDecodeError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(422, detail=str(exc)) from exc

    checksum = digest.hexdigest()
    duplicate = db.execute(text(
        "SELECT id FROM imports WHERE source_id=:source_id AND file_sha256=:sha "
        "ORDER BY id DESC LIMIT 1"
    ), {"source_id": source_id, "sha": checksum}).scalar()
    if duplicate and not confirm:
        destination.unlink(missing_ok=True)
        raise HTTPException(409, detail={
            "code": "duplicate_upload", "message": "This file was previously imported for this source",
            "duplicate_import_id": duplicate, "confirmation_required": True,
        })
    try:
        import_id = db.execute(text("""
            INSERT INTO imports
              (source_id, filename, file_path, file_sha256, record_type, status,
               rows_total, rows_ok, rows_rejected, created_by)
            VALUES (:source_id, :filename, :file_path, :sha, :record_type, 'uploaded',
                    :rows_total, 0, 0, :created_by)
            RETURNING id
        """), {"source_id": source_id, "filename": filename, "file_path": str(destination),
               "sha": checksum, "record_type": record_type, "rows_total": row_count,
               "created_by": user.id}).scalar_one()
        db.commit()
    except Exception:
        db.rollback()
        destination.unlink(missing_ok=True)
        raise
    return {"id": import_id, "filename": filename, "file_sha256": checksum,
            "file_size": size, "rows_total": row_count,
            "duplicate_of": duplicate, "status": "uploaded"}


@router.get("")
def list_imports(limit: int = 50, cursor: int | None = None,
                 user: User = Depends(require_role("analyst")),
                 db: Session = Depends(session_scope)):
    limit = min(max(limit, 1), 100)
    stmt = """
        SELECT i.*, job.id AS job_id, job.status AS job_status, job.progress AS progress
        FROM imports AS i
        LEFT JOIN LATERAL (
          SELECT id, status, progress FROM jobs
          WHERE type='import.run' AND payload->>'import_id'=i.id::text
          ORDER BY id DESC LIMIT 1
        ) AS job ON true
    """
    params: dict[str, Any] = {"limit": limit + 1}
    if cursor is not None:
        stmt += " WHERE i.id < :cursor"
        params["cursor"] = cursor
    stmt += " ORDER BY i.id DESC LIMIT :limit"
    rows = db.execute(text(stmt), params).mappings().all()
    next_cursor = rows[limit]["id"] if len(rows) > limit else None
    return {"items": [_json_import(row) for row in rows[:limit]], "next_cursor": next_cursor}


@router.get("/{import_id}")
def get_import(import_id: int, user: User = Depends(require_role("analyst")),
               db: Session = Depends(session_scope)):
    return _json_import(_get_import(db, import_id))


@router.get("/{import_id}/preview")
def preview_import(import_id: int, user: User = Depends(require_role("analyst")),
                   db: Session = Depends(session_scope)):
    row = _get_import(db, import_id)
    try:
        headers, preview = inspect_preview(row["file_path"], 50)
        suggestions = suggest_mapping(headers, row["record_type"])
    except (ImportProblem, UnicodeDecodeError, OSError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    saved = row["mapping"] or {}
    return {"id": import_id, "filename": row["filename"], "record_type": row["record_type"],
            "headers": headers, "suggested_mapping": suggestions,
            "mapping": saved.get("columns"), "options": saved.get("options", {}),
            "rows": preview}


@router.put("/{import_id}/mapping")
def set_mapping(import_id: int, body: MappingBody,
                user: User = Depends(require_role("analyst")),
                db: Session = Depends(session_scope)):
    row = _get_import(db, import_id)
    if row["status"] in {"running", "completed", "cancelled"}:
        raise HTTPException(409, detail="Mapping cannot be changed in this import state")
    try:
        headers, _ = inspect_preview(row["file_path"], 0)
        columns = validate_mapping(body.columns, row["record_type"], headers)
        allowed_options = {"date_format", "datetime_format", "phone_region"}
        unsupported = set(body.options) - allowed_options
        if unsupported:
            raise ValueError(f"Unsupported mapping options: {', '.join(sorted(unsupported))}")
        if body.options.get("phone_region") and not re_fullmatch_region(body.options["phone_region"]):
            raise ValueError("phone_region must be a two-letter region code")
    except (ImportProblem, ValueError, OSError) as exc:
        raise _http_input_error(exc) from exc
    db.execute(text(
        "UPDATE imports SET mapping=CAST(:mapping AS jsonb), status='mapped' WHERE id=:id"
    ), {"mapping": json.dumps({"columns": columns, "options": body.options}), "id": import_id})
    db.commit()
    return {"id": import_id, "status": "mapped", "mapping": columns, "options": body.options}


def re_fullmatch_region(region: Any) -> bool:
    return isinstance(region, str) and region.upper() in phonenumbers.SUPPORTED_REGIONS


@router.post("/{import_id}/validate")
def validate_import(import_id: int, user: User = Depends(require_role("analyst")),
                    db: Session = Depends(session_scope)):
    row = _get_import(db, import_id)
    if row["status"] in {"running", "completed", "cancelled"}:
        raise HTTPException(409, detail="Import cannot be validated in this state")
    mapping = row["mapping"] or {}
    if not mapping.get("columns"):
        raise HTTPException(409, detail="Set a column mapping before validation")
    try:
        result = validate_file(row["file_path"], mapping["columns"], row["record_type"],
                              mapping.get("options", {}), limit=5000)
    except (ImportProblem, UnicodeDecodeError, OSError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    warning_counts: dict[str, int] = {}
    for warning in result["warnings"]:
        category = _warning_category(warning["message"])
        warning_counts[category] = warning_counts.get(category, 0) + 1
    db.execute(text(
        "UPDATE imports SET rows_ok=:ok, rows_rejected=:rejected, warning_count=:warnings, "
        "warning_counts=CAST(:warning_counts AS jsonb), status='mapped' WHERE id=:id"
    ), {"ok": result["rows_ok"], "rejected": result["rows_rejected"],
        "warnings": result["warning_count"],
        "warning_counts": json.dumps(warning_counts), "id": import_id})
    db.commit()
    return result


@router.post("/{import_id}/run", status_code=202)
def start_import(import_id: int, user: User = Depends(require_role("analyst")),
                 db: Session = Depends(session_scope)):
    row = _get_import(db, import_id)
    if row["status"] == "running":
        raise HTTPException(409, detail="Import is already running")
    if row["status"] not in {"mapped", "uploaded", "failed"}:
        raise HTTPException(409, detail="Import cannot be run in this state")
    if not (row["mapping"] or {}).get("columns"):
        raise HTTPException(409, detail="Set a column mapping before running the import")
    job = enqueue(db, "import.run", {"import_id": import_id},
                  dedupe_key=f"import:{import_id}", max_attempts=1)
    db.execute(text("UPDATE imports SET status='running' WHERE id=:id"), {"id": import_id})
    db.commit()
    return {"id": import_id, "job_id": job.id, "status": "running"}


@router.get("/{import_id}/errors.csv")
def download_errors(import_id: int, user: User = Depends(require_role("analyst")),
                    db: Session = Depends(session_scope)):
    row = _get_import(db, import_id)
    path = _local_file(row, "error_file_path")
    return FileResponse(path, media_type="text/csv",
                        filename=f"import-{import_id}-errors.csv")