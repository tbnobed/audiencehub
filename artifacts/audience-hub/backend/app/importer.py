"""Submit deterministic seed CSVs to the same job pipeline used by uploads."""

import hashlib
import csv
import io
import json
import os
import shutil
import time
from pathlib import Path
from typing import Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import engine
from app.imports.mapping import suggest_mapping, validate_mapping
from app.imports.service import csv_reader
from app.imports.staging import IMPORT_TABLES, analyze_tables
from app.jobs.queue import enqueue


SEED_SOURCES = {
    "donor_crm_contacts.csv": ("donor_crm", "Donor CRM", "contact"),
    "giving_platform_gifts.csv": ("giving_platform", "Giving Platform", "gift"),
    "esp_contacts.csv": ("esp", "Email Service Provider", "contact"),
    "five9_calls.csv": ("five9", "Five9 Calls", "event"),
    "zeta_enrichment.csv": ("zeta_enrichment", "Zeta Enrichment", "enrichment"),
}


def _error_csv_contains_row_error(path: Path, row_number: int) -> bool:
    """Inspect the tail only; generated unrecoverable rows are appended last."""
    size = path.stat().st_size
    start = max(0, size - 16 * 1024)
    with path.open("rb") as stream:
        stream.seek(start)
        data = stream.read()
    if start:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    for entry in csv.reader(io.StringIO(data.decode("utf-8", errors="replace"))):
        if len(entry) >= 2 and entry[0] == str(row_number) and entry[1] == "error":
            return True
    return False


def _mapping(headers: list[str], record_type: str) -> dict:
    columns = suggest_mapping(headers, record_type)
    options = {}
    if record_type == "contact":
        for header in headers:
            if header in {"email_consent", "sms_consent", "consent_captured_at",
                          "hard_bounce", "bounce_reason"}:
                columns[header] = f"attributes.{header}"
    elif record_type == "event" and "properties" in headers:
        columns["properties"] = "properties"
    elif record_type == "enrichment":
        # An enrichment import has exactly one identifier target.
        columns = {"contact_external_id": "contact_external_id"}
        if "external_id" in headers:
            columns["external_id"] = "external_id"
        for header, data_type in (
            ("hh_income_band", "enum"), ("age_band", "enum"),
            ("interests", "enum"), ("donor_propensity_score", "number"),
        ):
            if header in headers:
                columns[header] = {"enrichment": header, "data_type": data_type}
    if record_type in {"gift", "enrichment"} and any(
        target == "contact_external_id" for target in columns.values()
    ):
        options["reference_source"] = "donor_crm"
    return {"columns": validate_mapping(columns, record_type, headers), "options": options}


def import_seed_files(files: Mapping[str, Path]) -> None:
    """Create source/import rows, enqueue real import.run jobs, and await completion."""
    if set(files) != set(SEED_SOURCES):
        raise ValueError("Seed loader requires exactly the five generated CSV files")
    upload_dir = Path(get_settings().upload_dir).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    submitted: list[tuple[int, str]] = []
    import_ids: dict[str, int] = {}
    for filename, file in files.items():
        source_key, source_name, record_type = SEED_SOURCES[filename]
        path = Path(file).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        headers, rows = csv_reader(path)
        try:
            count = sum(1 for _ in rows)
        finally:
            rows.close()
        mapping = _mapping(headers, record_type)
        checksum = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        digest = checksum.hexdigest()
        destination = upload_dir / f"seed-{digest[:16]}-{filename}"
        if not destination.is_file():
            shutil.copyfile(path, destination)
        with Session(engine) as db:
            source_id = db.execute(text("SELECT id FROM sources WHERE key=:key"),
                                   {"key": source_key}).scalar()
            if source_id is None:
                source_id = db.execute(text("""
                    INSERT INTO sources (key, name, kind, record_types, priority, is_active)
                    VALUES (:key, :name, 'csv', :record_types, :priority, true)
                    RETURNING id
                """), {"key": source_key, "name": source_name,
                       "record_types": [record_type],
                       "priority": len(SEED_SOURCES) * 10}).scalar_one()
            previous = db.execute(text("""
                SELECT id FROM imports WHERE source_id=:source_id AND file_sha256=:digest
                  AND record_type=:record_type AND status='completed' LIMIT 1
            """), {"source_id": source_id, "digest": digest,
                   "record_type": record_type}).scalar()
            if previous is not None:
                print(f"{filename}: unchanged (already completed import {previous})", flush=True)
                import_ids[filename] = previous
                db.commit()
                continue
            import_id = db.execute(text("""
                INSERT INTO imports
                  (source_id, filename, file_path, file_sha256, record_type,
                   mapping, status, rows_total, rows_ok, rows_rejected)
                VALUES (:source_id, :filename, :path, :digest, :record_type,
                        CAST(:mapping AS jsonb), 'running', :count, 0, 0)
                RETURNING id
            """), {"source_id": source_id, "filename": filename,
                   "path": str(destination), "digest": digest,
                   "record_type": record_type, "mapping": json.dumps(mapping),
                   "count": count}).scalar_one()
            enqueue(db, "import.run", {"import_id": import_id},
                    dedupe_key=f"import:{import_id}", max_attempts=1)
            db.execute(text("UPDATE imports SET status='running' WHERE id=:id"),
                       {"id": import_id})
            db.commit()
            submitted.append((import_id, filename))
            import_ids[filename] = import_id
            print(f"Queued {filename}: import {import_id}, {count:,} rows", flush=True)

    pending = dict(submitted)
    deadline = time.monotonic() + int(os.environ.get("SEED_LOAD_TIMEOUT_SECONDS", "7200"))
    while pending:
        with Session(engine) as db:
            rows = db.execute(text("""
                SELECT i.id, i.status, i.rows_total, i.rows_ok, i.rows_rejected,
                       i.warning_count, i.warning_counts, i.rows_normalized,
                       i.rows_deduplicated, i.duration_ms, i.error_file_path,
                       j.status AS job_status
                FROM imports AS i
                LEFT JOIN jobs AS j ON j.type='import.run'
                  AND j.payload->>'import_id'=i.id::text
                WHERE i.id = ANY(:ids)
            """), {"ids": list(pending)}).mappings()
            for row in rows:
                if row["status"] in {"completed", "failed"} or row["job_status"] == "failed":
                    filename = pending.pop(row["id"])
                    status = "failed" if row["job_status"] == "failed" else row["status"]
                    if status == "failed" and row["status"] != "failed":
                        db.execute(text("UPDATE imports SET status='failed' WHERE id=:id"),
                                   {"id": row["id"]})
                        db.commit()
                    print(f"{filename}: {status} "
                          f"(ok={row['rows_ok']:,}, rejected={row['rows_rejected']:,}, "
                          f"warnings={row['warning_count']:,}, total={row['rows_total']:,}, "
                          f"duration={(row['duration_ms'] or 0) / 1000:.3f}s)", flush=True)
                    if status != "completed":
                        raise RuntimeError(f"Seed import {row['id']} failed: {filename}")
        if pending:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Seed imports did not finish: {list(pending)}")
            time.sleep(2)

    # Also refresh on an idempotent seed reload (all five imports may be skipped).
    with Session(engine) as db:
        analyze_tables(db, IMPORT_TABLES)
        db.commit()

    stats_path = next(iter(files.values())).resolve().parent / "generator_stats.json"
    if stats_path.is_file():
        with stats_path.open(encoding="utf-8") as stream:
            stats = json.load(stream)
        with Session(engine) as db:
            for filename, import_id in import_ids.items():
                row = db.execute(text("""
                    SELECT status, rows_total, rows_ok, rows_rejected, warning_count,
                           warning_counts, rows_normalized, rows_deduplicated,
                           duration_ms, error_file_path
                    FROM imports WHERE id=:id
                """), {"id": import_id}).mappings().first()
                if not row:
                    raise RuntimeError(f"Import metrics missing for {filename} (import {import_id})")
                error_file_available = bool(row["error_file_path"] and
                                            Path(row["error_file_path"]).is_file())
                error_file_has_errors = bool(error_file_available and row["rows_rejected"])
                unrecoverable_row_in_error_csv = bool(
                    error_file_available and _error_csv_contains_row_error(
                        Path(row["error_file_path"]), row["rows_total"] + 1
                    )
                )
                elapsed = (row["duration_ms"] or 0) / 1000
                has_injected_unrecoverable = (
                    stats["files"][filename]["messiness_injected"].get("unrecoverable_row", 0) > 0
                )
                stats["files"][filename]["import_handling"] = {
                    "status": row["status"],
                    "rows_total": row["rows_total"],
                    "rows_ok": row["rows_ok"],
                    "rows_rejected": row["rows_rejected"],
                    "warning_count": row["warning_count"],
                    "warning_counts": row["warning_counts"] or {},
                    "rows_normalized": row["rows_normalized"],
                    "rows_deduplicated": row["rows_deduplicated"],
                    "duration_seconds": elapsed,
                    "rows_per_second": row["rows_total"] / elapsed if elapsed else None,
                    "error_csv_available": error_file_available,
                    "error_csv_contains_error": error_file_has_errors,
                    "unrecoverable_row_rejected": (
                        row["rows_rejected"] > 0 if has_injected_unrecoverable else None
                    ),
                    "unrecoverable_row_in_error_csv": (
                        unrecoverable_row_in_error_csv if has_injected_unrecoverable else None
                    ),
                }
        with stats_path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(stats, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")