"""Submit deterministic seed CSVs to the same job pipeline used by uploads."""

import hashlib
import csv
import io
import json
import os
import shutil
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
    "people_feed.csv": ("people_feed", "people_feed", "contact"),
    "donor_direct.csv": ("donor_direct", "donor_direct", "contact"),
    "hubspot.csv": ("hubspot", "hubspot", "contact"),
    "okta_tbn_plus.csv": ("okta_tbn_plus", "okta_tbn_plus", "contact"),
    "donation_events_gifts.csv": ("donation_events", "donation_events", "gift"),
    "donation_events.csv": ("donation_events", "donation_events", "event"),
    "vod_viewed_events.csv": ("vod_viewed_events", "vod_viewed_events", "event"),
    "model_scores.csv": ("model_scores", "model_scores", "enrichment"),
}
LEGACY_SEED_SOURCES = {
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


def _mapping(headers: list[str], record_type: str, *,
             reference_source: str = "donor_crm") -> dict:
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
        for header in ("vendor", "license_id", "append_date", "sensitive"):
            if header in headers:
                columns[header] = header
        if "external_id" in headers:
            columns["external_id"] = "external_id"
        for header, data_type in (
            ("hh_income_band", "enum"), ("age_band", "enum"),
            ("interests", "enum"), ("donor_propensity_score", "number"),
        ):
            if header in headers:
                columns[header] = {"enrichment": header, "data_type": data_type}
    if record_type in {"gift", "event", "enrichment"} and any(
        target == "contact_external_id" for target in columns.values()
    ):
        options["reference_source"] = reference_source
    elif (record_type == "contact" and "contact_external_id" in headers
          and reference_source != "donor_crm"):
        # Secondary contacts still resolve by their existing identity keys.
        # Keep the primary namespace explicit without changing contact mapping.
        options["reference_source"] = reference_source
    return {"columns": validate_mapping(columns, record_type, headers), "options": options}


def import_seed_files(files: Mapping[str, Path]) -> list[int]:
    """Create source/import rows, enqueue real import.run jobs, and await completion."""
    if set(files) == set(SEED_SOURCES):
        sources = SEED_SOURCES
    elif set(files) == set(LEGACY_SEED_SOURCES):
        sources = LEGACY_SEED_SOURCES
    else:
        raise ValueError("Seed loader requires exactly the audit source manifest or five legacy CSV files")
    # Validate the complete file manifest before publishing any database work.
    for filename in sources:
        if not Path(files[filename]).resolve().is_file():
            raise FileNotFoundError(files[filename])
    upload_dir = Path(get_settings().upload_dir).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    import_ids: dict[str, int] = {}
    # No runnable work is published until every member has been registered.
    with Session(engine) as db:
        batch_id = db.scalar(text("INSERT INTO identity_batches DEFAULT VALUES RETURNING id"))
        db.commit()
    for filename, (source_key, source_name, record_type) in sources.items():
        file = files[filename]
        path = Path(file).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        headers, rows = csv_reader(path)
        try:
            count = sum(1 for _ in rows)
        finally:
            rows.close()
        mapping = _mapping(
            headers, record_type,
            reference_source="donor_direct" if sources is SEED_SOURCES else "donor_crm",
        )
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
                       "record_types": list(dict.fromkeys(
                           kind for key, _, kind in sources.values() if key == source_key
                       )),
                       "priority": len(sources) * 10}).scalar_one()
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
            db.execute(text("UPDATE imports SET status='running', identity_batch_id=:batch WHERE id=:id"),
                       {"id": import_id, "batch": batch_id})
            db.commit()
            import_ids[filename] = import_id
            print(f"Queued {filename}: import {import_id}, {count:,} rows", flush=True)

    with Session(engine) as db:
        for import_id in import_ids.values():
            db.execute(text("UPDATE imports SET identity_batch_id=:batch WHERE id=:id"),
                       {"batch": batch_id, "id": import_id})
            enqueue(db, "import.run", {"import_id": import_id},
                    dedupe_key=f"import:{import_id}", max_attempts=1)
        db.execute(text("UPDATE identity_batches SET phase='importing' WHERE id=:id"), {"id": batch_id})
        enqueue(db, "identity.resolve_batch", {"batch_id": batch_id},
                dedupe_key=f"identity:batch:{batch_id}")
        db.commit()

    from app.load_status import run_load_status

    if run_load_status(
        engine, wait=True, import_ids=list(import_ids.values()),
        timeout=int(os.environ.get("SEED_LOAD_TIMEOUT_SECONDS", "7200")),
    ):
        raise RuntimeError("Seed load failed; see load-status summary above.")

    # Also refresh on an idempotent reload (all manifest members may be skipped).
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
                    stats["files"][filename].get("messiness_injected", {}).get("unrecoverable_row", 0) > 0
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
            totals = db.execute(text("""
                SELECT count(*) AS source_records,
                       count(DISTINCT profile_id) AS resulting_profiles
                FROM source_records WHERE last_import_id=ANY(:ids)
            """), {"ids": list(import_ids.values())}).mappings().one()
            stats["load_summary"] = {
                "source_file_records": sum(
                    item["rows_generated"] for item in stats["files"].values()
                ),
                "source_records": totals["source_records"],
                "expected_profiles": stats.get("requested_people"),
                "resulting_profiles": totals["resulting_profiles"],
            }
        summary = stats["load_summary"]
        print(
            f"Seed records: {summary['source_file_records']:,} source file records; "
            f"{summary['source_records']:,} stored source records; "
            f"expected profiles={summary['expected_profiles']}; "
            f"resulting profiles={summary['resulting_profiles']:,}",
            flush=True,
        )
        with stats_path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(stats, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
    return list(import_ids.values())