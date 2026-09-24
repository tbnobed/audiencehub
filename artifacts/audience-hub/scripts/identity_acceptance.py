#!/usr/bin/env python3
"""Read-only identity-resolution acceptance report for generated seed data.

Run from ``artifacts/audience-hub/backend`` after importing and resolving the
seed files, for example:

    DATABASE_URL="$DATABASE_URL" python ../scripts/identity_acceptance.py

The script reads ``DATABASE_URL`` without printing it, runs its PostgreSQL
queries in a read-only transaction, and uses a temporary local SQLite database
to keep the ground-truth and profile/person comparisons bounded in memory.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import create_engine, text


SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent / "backend"
DEFAULT_GROUND_TRUTH = BACKEND_DIR / "seed-data" / "acceptance" / "ground_truth.json"
DEFAULT_GENERATOR_STATS = DEFAULT_GROUND_TRUTH.with_name("generator_stats.json")
FILES = {
    "donor_crm_contacts.csv": "donor_crm",
    "giving_platform_gifts.csv": "giving_platform",
    "esp_contacts.csv": "esp",
    "five9_calls.csv": "five9",
    "zeta_enrichment.csv": "zeta_enrichment",
}
DB_BATCH_SIZE = 10_000


def _non_whitespace(stream: TextIO) -> str:
    while True:
        char = stream.read(1)
        if not char or not char.isspace():
            return char


def _read_string(stream: TextIO) -> str:
    """Read one JSON string token (including escapes) and decode it."""
    token = ['"']
    escaped = False
    while True:
        char = stream.read(1)
        if not char:
            raise ValueError("Unexpected end of ground-truth JSON string")
        token.append(char)
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return json.loads("".join(token))


def _read_json_value(stream: TextIO, first: str, retain: bool = True) -> Any:
    """Read one JSON value; containers are scanned without buffering when skipped."""
    if first == '"':
        token = ['"']
        escaped = False
        while True:
            char = stream.read(1)
            if not char:
                raise ValueError("Unexpected end of ground-truth JSON string")
            if retain:
                token.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return json.loads("".join(token)) if retain else None
    if first in "[{":
        opening = first
        closing = "]" if first == "[" else "}"
        depth = 1
        in_string = False
        escaped = False
        token = [first] if retain else None
        while depth:
            char = stream.read(1)
            if not char:
                raise ValueError("Unexpected end of ground-truth JSON container")
            if retain:
                token.append(char)
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
        return json.loads("".join(token)) if retain else None

    token = [first] if retain else None
    while True:
        char = stream.read(1)
        if not char or char.isspace() or char in ",}]":
            if char:
                stream.seek(stream.tell() - 1)
            break
        if retain:
            token.append(char)
    return json.loads("".join(token)) if retain else None


def _iter_ground_truth(path: Path, local: sqlite3.Connection) -> dict[str, Any]:
    """Stream records from the seed format's top-level ``records`` object."""
    metadata: dict[str, Any] = {}
    inserted = 0
    with path.open("r", encoding="utf-8") as stream:
        if _non_whitespace(stream) != "{":
            raise ValueError("Ground-truth JSON root must be an object")
        while True:
            char = _non_whitespace(stream)
            if char == "}":
                break
            if char != '"':
                raise ValueError("Expected a top-level ground-truth JSON key")
            key = _read_string(stream)
            if _non_whitespace(stream) != ":":
                raise ValueError(f"Expected ':' after ground-truth key {key!r}")
            first = _non_whitespace(stream)
            if key != "records":
                if key in {"format_version", "random_seed", "scale", "requested_people", "record_count"}:
                    metadata[key] = _read_json_value(stream, first)
                else:
                    _read_json_value(stream, first, retain=False)
            else:
                if first != "{":
                    raise ValueError("Ground-truth 'records' must be an object")
                batch: list[tuple[str, str, str, str | None]] = []
                while True:
                    record_char = _non_whitespace(stream)
                    if record_char == "}":
                        break
                    if record_char != '"':
                        raise ValueError("Expected a record key in ground-truth records")
                    record_key = _read_string(stream)
                    if _non_whitespace(stream) != ":":
                        raise ValueError("Expected ':' after ground-truth record key")
                    value = _read_json_value(stream, _non_whitespace(stream))
                    if not isinstance(value, dict) or not isinstance(value.get("person_id"), str):
                        raise ValueError(f"Ground-truth record {record_key!r} lacks person_id")
                    if ":" not in record_key:
                        raise ValueError(f"Ground-truth record key has no source prefix: {record_key!r}")
                    source_key, external_id = record_key.split(":", 1)
                    batch.append((source_key, external_id, value["person_id"],
                                  value.get("shared_household_id")))
                    if len(batch) >= DB_BATCH_SIZE:
                        local.executemany(
                            "INSERT OR REPLACE INTO truth(source_key, external_id, person_id, household_id) "
                            "VALUES (?, ?, ?, ?)", batch)
                        inserted += len(batch)
                        batch.clear()
                    separator = _non_whitespace(stream)
                    if separator == "}":
                        break
                    if separator != ",":
                        raise ValueError("Expected ',' or '}' in ground-truth records")
                if batch:
                    local.executemany(
                        "INSERT OR REPLACE INTO truth(source_key, external_id, person_id, household_id) "
                        "VALUES (?, ?, ?, ?)", batch)
                    inserted += len(batch)
                metadata["streamed_record_count"] = inserted
            separator = _non_whitespace(stream)
            if separator == "}":
                break
            if separator != ",":
                raise ValueError("Expected ',' or '}' in ground-truth root")
    local.commit()
    if "streamed_record_count" not in metadata:
        raise ValueError("Ground-truth JSON has no records object")
    return metadata


def _sqlite_database(local: sqlite3.Connection) -> None:
    local.executescript("""
        CREATE TABLE truth (
            source_key TEXT NOT NULL,
            external_id TEXT NOT NULL,
            person_id TEXT NOT NULL,
            household_id TEXT,
            PRIMARY KEY (source_key, external_id)
        );
        CREATE TABLE actual (
            source_key TEXT NOT NULL,
            external_id TEXT NOT NULL,
            profile_id INTEGER NOT NULL,
            email_norm TEXT,
            phone_e164 TEXT,
            user_id TEXT,
            anonymous_id TEXT,
            PRIMARY KEY (source_key, external_id)
        );
        CREATE INDEX actual_profile_idx ON actual(profile_id);
        CREATE TABLE person_households (
            person_id TEXT NOT NULL,
            household_id TEXT NOT NULL,
            PRIMARY KEY(person_id, household_id)
        );
    """)


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is required; database credentials are never printed.")
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://"):]
    return value


def _load_active_records(engine: Any, local: sqlite3.Connection) -> None:
    statement = text("""
        SELECT s.key AS source_key, sr.external_id, sr.profile_id,
               sr.email_norm, sr.phone_e164, sr.attributes
        FROM source_records AS sr
        JOIN sources AS s ON s.id = sr.source_id
        JOIN profiles AS p ON p.id = sr.profile_id
        WHERE p.merged_into_id IS NULL AND p.is_deleted = false
          AND s.key = ANY(:source_keys)
        ORDER BY sr.id
    """)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            result = connection.execution_options(stream_results=True).execute(
                statement, {"source_keys": list(FILES.values())})
            batch: list[tuple[str, str, int, str | None, str | None, str | None, str | None]] = []
            while rows := result.fetchmany(DB_BATCH_SIZE):
                for row in rows:
                    attributes = row.attributes or {}
                    if isinstance(attributes, str):
                        attributes = json.loads(attributes)
                    raw_user_id = attributes.get("user_id")
                    user_id = (f"{row.source_key}:{str(raw_user_id).strip()}"
                               if raw_user_id is not None and
                               0 < len(str(raw_user_id).strip()) <= 200 else None)
                    raw_anonymous_id = attributes.get("anonymous_id")
                    anonymous_id = (str(raw_anonymous_id).strip()
                                    if attributes.get("identify") and raw_anonymous_id is not None
                                    and 0 < len(str(raw_anonymous_id).strip()) <= 200 else None)
                    batch.append((row.source_key, row.external_id, row.profile_id,
                                  row.email_norm, row.phone_e164, user_id, anonymous_id))
                if len(batch) >= DB_BATCH_SIZE:
                    local.executemany(
                        "INSERT OR REPLACE INTO actual(source_key, external_id, profile_id, "
                        "email_norm, phone_e164, user_id, anonymous_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        batch)
                    batch.clear()
            if batch:
                local.executemany(
                    "INSERT OR REPLACE INTO actual(source_key, external_id, profile_id, "
                    "email_norm, phone_e164, user_id, anonymous_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    batch)
            result.close()
            transaction.rollback()
        except Exception:
            transaction.rollback()
            raise
    local.commit()


def _read_db_report(engine: Any) -> dict[str, Any]:
    report: dict[str, Any] = {}
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        report["active_profiles"] = connection.execute(text("""
            SELECT count(*) FROM profiles
            WHERE merged_into_id IS NULL AND is_deleted = false
        """)).scalar_one()
        report["merge_count"] = connection.execute(
            text("SELECT count(*) FROM profile_merges")).scalar_one()
        report["resolution_jobs"] = connection.execute(text("""
            SELECT min(started_at), max(finished_at), count(*)
            FROM jobs WHERE type='identity.resolve_batch'
              AND started_at IS NOT NULL AND finished_at IS NOT NULL
        """)).one()
        report["imports"] = connection.execute(text("""
            SELECT s.key AS source_key, i.filename, i.id, i.status, i.file_sha256,
                   i.rows_total, i.rows_ok, i.rows_rejected, i.warning_count,
                   i.warning_counts, i.rows_normalized, i.rows_deduplicated,
                   i.duration_ms, i.started_at, i.finished_at, i.created_at
            FROM imports AS i JOIN sources AS s ON s.id=i.source_id
            WHERE s.key=ANY(:keys)
            ORDER BY s.key, i.filename, i.created_at, i.id
        """), {"keys": list(FILES.values())}).mappings().all()
        merge_reasons = Counter()
        result = connection.execution_options(stream_results=True).execute(
            text("SELECT reason FROM profile_merges"))
        while rows := result.fetchmany(DB_BATCH_SIZE):
            for row in rows:
                reason = row.reason
                if isinstance(reason, str):
                    try:
                        reason = json.loads(reason)
                    except json.JSONDecodeError:
                        reason = {}
                reason_type = reason.get("type") if isinstance(reason, dict) else None
                merge_reasons[str(reason_type or "unknown")] += 1
        result.close()
        report["merge_reasons"] = merge_reasons
        transaction.rollback()
    return report


def _format_duration(seconds: float | None) -> str:
    return "unknown" if seconds is None else f"{seconds:.3f}s"


def _split_identifier_reason_counts(local: sqlite3.Connection) -> tuple[int, Counter[str]]:
    """Summarize identifier evidence across split people without exposing values."""
    rows = local.execute("""
        WITH person_profiles AS (
            SELECT DISTINCT t.person_id, a.profile_id
            FROM truth AS t JOIN actual AS a
              ON a.source_key=t.source_key AND a.external_id=t.external_id
        ),
        split_people AS (
            SELECT person_id FROM person_profiles
            GROUP BY person_id HAVING count(*) > 1
        ),
        identifier_values AS (
            SELECT DISTINCT t.person_id, a.profile_id, 'email' AS kind, a.email_norm AS value
            FROM truth AS t JOIN actual AS a
              ON a.source_key=t.source_key AND a.external_id=t.external_id
            WHERE a.email_norm IS NOT NULL AND a.email_norm <> ''
            UNION ALL
            SELECT DISTINCT t.person_id, a.profile_id, 'phone', a.phone_e164
            FROM truth AS t JOIN actual AS a
              ON a.source_key=t.source_key AND a.external_id=t.external_id
            WHERE a.phone_e164 IS NOT NULL AND a.phone_e164 <> ''
            UNION ALL
            SELECT DISTINCT t.person_id, a.profile_id, 'user_id', a.user_id
            FROM truth AS t JOIN actual AS a
              ON a.source_key=t.source_key AND a.external_id=t.external_id
            WHERE a.user_id IS NOT NULL AND a.user_id <> ''
            UNION ALL
            SELECT DISTINCT t.person_id, a.profile_id, 'anonymous_id', a.anonymous_id
            FROM truth AS t JOIN actual AS a
              ON a.source_key=t.source_key AND a.external_id=t.external_id
            WHERE a.anonymous_id IS NOT NULL AND a.anonymous_id <> ''
        ),
        spanning_types AS (
            SELECT person_id, kind
            FROM identifier_values
            GROUP BY person_id, kind, value
            HAVING count(DISTINCT profile_id) > 1
        ),
        overlap AS (
            SELECT person_id, group_concat(DISTINCT kind) AS kinds
            FROM spanning_types GROUP BY person_id
        ),
        available AS (
            SELECT person_id, count(*) AS identifier_count
            FROM identifier_values GROUP BY person_id
        ),
        reasons AS (
            SELECT split_people.person_id,
              CASE
                WHEN overlap.kinds IS NOT NULL
                  THEN 'shared persisted identifier spans profiles: ' || overlap.kinds
                WHEN coalesce(available.identifier_count, 0) > 0
                  THEN 'no shared persisted matching identifier observed'
                ELSE 'no persisted matching identifiers available'
              END AS reason
            FROM split_people
            LEFT JOIN overlap USING(person_id)
            LEFT JOIN available USING(person_id)
        )
        SELECT reason, count(*) FROM reasons GROUP BY reason
    """).fetchall()
    counts: Counter[str] = Counter()
    for reason, count in rows:
        prefix = "shared persisted identifier spans profiles: "
        if reason.startswith(prefix):
            reason = prefix + ", ".join(sorted(reason[len(prefix):].split(",")))
        counts[reason] += count
    return sum(counts.values()), counts


def _import_wall_clock(row: dict[str, Any]) -> float | None:
    # Both PostgreSQL now() timestamps are written in the import transaction,
    # so their difference is zero; the worker's monotonic duration is accurate.
    if row.get("duration_ms") is not None:
        return max(0.0, row["duration_ms"] / 1000)
    started, finished = row.get("started_at"), row.get("finished_at")
    if started is None or finished is None:
        return None
    return max(0.0, (finished - started).total_seconds())


def _load_generator_stats(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as stream:
        stats = json.load(stream)
    if not isinstance(stats, dict) or not isinstance(stats.get("files"), dict):
        raise ValueError("generator_stats.json must contain a files object")
    return stats


def _print_generator_import_comparison(
    filename: str, file_stats: dict[str, Any] | None, latest_import: dict[str, Any] | None,
) -> None:
    if not file_stats:
        print("    generator_stats: no per-file stats entry")
        return
    injected = file_stats.get("messiness_injected") or {}
    expected = file_stats.get("expected_handling") or {}
    if not isinstance(injected, dict) or not isinstance(expected, dict):
        print("    generator/import comparison: unsupported (unexpected stats schema)")
        return
    if latest_import is None:
        print("    generator/import comparison: no matching import row")
        return
    generated_rows = file_stats.get("rows_generated")
    if isinstance(generated_rows, int):
        imported_rows = int(latest_import.get("rows_total") or 0)
        print(f"    generated rows={generated_rows:,}; importer rows_total={imported_rows:,} "
              f"(delta={imported_rows - generated_rows:+,})")

    warning_categories = latest_import.get("warning_counts") or {}
    if isinstance(warning_categories, str):
        try:
            warning_categories = json.loads(warning_categories)
        except json.JSONDecodeError:
            warning_categories = {}
    if not isinstance(warning_categories, dict):
        warning_categories = {}

    direct_warning = {
        "blocklisted_junk_email": "blocklisted_identifier",
        "invalid_email": "invalid_email",
        "invalid_phone": "invalid_phone",
        "mixed_date_format": "date_format_coerced",
    }
    direct_deduplicated = {"duplicate_row"}
    for kind, count in sorted(injected.items()):
        handling = expected.get(kind, "unsupported: no expected_handling entry")
        if kind in direct_warning:
            category = direct_warning[kind]
            observed = int(warning_categories.get(category, 0))
            print(f"    injected {kind}={count}: warning category {category} "
                  f"observed={observed} (delta={observed - int(count):+})")
        elif kind in direct_deduplicated:
            observed = int(latest_import.get("rows_deduplicated") or 0)
            print(f"    injected {kind}={count}: rows_deduplicated observed={observed} "
                  f"(delta={observed - int(count):+}); "
                  "aggregate metric, not attributable to this injection alone")
        elif handling == "rejected_and_error_csv":
            observed = int(latest_import.get("rows_rejected") or 0)
            print(f"    injected {kind}={count}: rows_rejected observed={observed}; "
                  "aggregate includes other rejection causes, so per-injection comparison "
                  "is unsupported")
        elif handling == "normalized":
            observed = int(latest_import.get("rows_normalized") or 0)
            print(f"    injected {kind}={count}: rows_normalized observed={observed}; "
                  "per-injection attribution unsupported (row metric can overlap)")
        else:
            print(f"    injected {kind}={count}: expected_handling={handling}; "
                  "no corresponding importer category is supported")
    if not injected:
        print("    generator/import comparison: no messiness injected for this file")


def _print_report(metadata: dict[str, Any], local: sqlite3.Connection,
                  report: dict[str, Any],
                  generator_stats: dict[str, Any] | None = None) -> None:
    true_people = local.execute("SELECT count(DISTINCT person_id) FROM truth").fetchone()[0]
    truth_records = local.execute("SELECT count(*) FROM truth").fetchone()[0]
    matched_records = local.execute("""
        SELECT count(*) FROM truth AS t JOIN actual AS a
          ON a.source_key=t.source_key AND a.external_id=t.external_id
    """).fetchone()[0]
    local.execute("""
        INSERT OR IGNORE INTO person_households(person_id, household_id)
        SELECT DISTINCT person_id, household_id FROM truth
        WHERE household_id IS NOT NULL
    """)
    local.commit()

    active = int(report["active_profiles"])
    delta = active - true_people
    percent_diff = (delta / true_people * 100) if true_people else None
    multi_profiles = local.execute("""
        WITH profile_people AS (
            SELECT DISTINCT a.profile_id, t.person_id
            FROM actual AS a JOIN truth AS t
              ON t.source_key=a.source_key AND t.external_id=a.external_id
        )
        SELECT count(*) FROM (
            SELECT profile_id FROM profile_people
            GROUP BY profile_id HAVING count(*) > 1
        )
    """).fetchone()[0]
    intentional = local.execute("""
        WITH profile_people AS (
            SELECT DISTINCT a.profile_id, t.person_id
            FROM actual AS a JOIN truth AS t
              ON t.source_key=a.source_key AND t.external_id=a.external_id
        ),
        household_members AS (
            SELECT pp.profile_id, pp.person_id, ph.household_id
            FROM profile_people AS pp
            JOIN person_households AS ph USING(person_id)
        ),
        intentional_profiles AS (
            SELECT hm.profile_id
            FROM household_members AS hm
            JOIN profile_people AS pp ON pp.profile_id=hm.profile_id
            GROUP BY hm.profile_id, hm.household_id
             HAVING count(DISTINCT hm.person_id) > 1
                AND count(DISTINCT hm.person_id) = (
                SELECT count(*) FROM profile_people AS all_people
                WHERE all_people.profile_id=hm.profile_id
            )
        )
        SELECT count(DISTINCT profile_id) FROM intentional_profiles
    """).fetchone()[0]
    split_people, split_reasons = _split_identifier_reason_counts(local)
    print("Identity acceptance report (read-only)")
    print(f"Ground truth: {truth_records:,} records; {true_people:,} true people "
          f"(requested_people={metadata.get('requested_people', 'unknown')})")
    print(f"Active profiles: {active:,}; difference={delta:+,} "
          f"({percent_diff:+.2f}% vs true people)" if percent_diff is not None else
          f"Active profiles: {active:,}; difference={delta:+,}; percent difference=unknown")
    print(f"Ground-truth records found on active profiles: {matched_records:,}/{truth_records:,}")
    shared_percent = (multi_profiles / active * 100) if active else None
    print(f"Active profiles with >1 true person: {multi_profiles:,}/"
          f"{active:,} ({shared_percent:.2f}%)" if shared_percent is not None else
          f"Active profiles with >1 true person: {multi_profiles:,}; percent=unknown")
    print(f"  Intentional shared-household profiles: {intentional:,}")
    print(f"  Other/mixed multi-person profiles (possible false merges): "
          f"{max(0, multi_profiles - intentional):,}")
    print(f"True people split across >1 active profile: {split_people:,}")
    print("  Top split identifier evidence (descriptive, not proven causes): " +
          (", ".join(f"{reason}={count:,}" for reason, count in
                     split_reasons.most_common(5)) or "none"))
    print(f"Total recorded profile merges: {report['merge_count']:,}")
    reasons = report["merge_reasons"].most_common(5)
    print("  Merge reason types (not split causes): " +
          (", ".join(f"{name}={count:,}" for name, count in reasons) or "none"))

    started, finished, job_count = report["resolution_jobs"]
    elapsed = (finished - started).total_seconds() if started and finished else None
    print(f"Identity resolution jobs: {job_count:,}; wall-clock span="
          f"{_format_duration(elapsed)} (earliest started_at to latest finished_at)")

    print("Per-file generator/import statistics:")
    if generator_stats is None:
        print(f"  generator_stats unavailable: expected {DEFAULT_GENERATOR_STATS.name} "
              "beside ground_truth.json or provide --generator-stats")
    imports_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in report["imports"]:
        imports_by_source[row["source_key"]].append(dict(row))
    for filename, source_key in FILES.items():
        expected = local.execute(
            "SELECT count(*) FROM truth WHERE source_key=?", (source_key,)).fetchone()[0]
        file_stats = (generator_stats or {}).get("files", {}).get(filename)
        generated_count = file_stats.get("rows_generated") if isinstance(file_stats, dict) else None
        generated_text = f"{generated_count:,}" if isinstance(generated_count, int) else "unknown"
        print(f"  {filename}: generated_rows={generated_text}; "
              f"ground-truth records={expected:,}")
        file_imports = [row for row in imports_by_source[source_key]
                        if Path(row["filename"]).name == filename]
        if not file_imports:
            print("    imports: no matching import rows")
            _print_generator_import_comparison(filename, file_stats, None)
            continue
        for row in file_imports:
            categories = row["warning_counts"] or {}
            if isinstance(categories, str):
                try:
                    categories = json.loads(categories)
                except json.JSONDecodeError:
                    categories = {}
            category_text = ", ".join(
                f"{key}={value}" for key, value in sorted(categories.items())) or "none"
            elapsed = _import_wall_clock(row)
            rate = (row["rows_total"] / elapsed
                    if elapsed is not None and elapsed > 0 else None)
            print(f"    import {row['id']} status={row['status']}: "
                  f"total={row['rows_total']:,}, handled_ok={row['rows_ok']:,}, "
                  f"rejected={row['rows_rejected']:,}, warnings={row['warning_count']:,}, "
                  f"normalized={row['rows_normalized']:,}, "
                  f"deduplicated={row['rows_deduplicated']:,}, "
                  f"warning_categories=[{category_text}], "
                  f"wall_clock={_format_duration(elapsed)}, "
                  f"rate={f'{rate:,.1f} rows/s' if rate is not None else 'unknown'}")

        completed = [row for row in file_imports if row["status"] == "completed"]
        latest_import = completed[-1] if completed else file_imports[-1]
        _print_generator_import_comparison(filename, file_stats, latest_import)

        by_digest: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in file_imports:
            if row["file_sha256"] and row["status"] == "completed":
                by_digest[row["file_sha256"]].append(row)
        repeat_deltas = []
        for repeated in by_digest.values():
            if len(repeated) < 2:
                continue
            previous, latest = repeated[-2:]
            repeat_deltas.append((previous, latest))
        if repeat_deltas:
            for previous, latest in repeat_deltas:
                print(f"    idempotent re-import delta (same content): "
                      f"import {previous['id']} -> {latest['id']}: "
                      f"handled_ok={latest['rows_ok'] - previous['rows_ok']:+,}, "
                      f"rejected={latest['rows_rejected'] - previous['rows_rejected']:+,}, "
                      f"normalized={latest['rows_normalized'] - previous['rows_normalized']:+,}, "
                      f"deduplicated={latest['rows_deduplicated'] - previous['rows_deduplicated']:+,}")
            print("    profile-created/merge delta attributable to re-import: unknown "
                  "(imports are not linked to a complete resolution-job snapshot)")
        else:
            print("    idempotent re-import delta: unknown (fewer than two completed "
                  "imports of identical content)")
    print("Unsupported import categories are called out per injected type; the "
          "database has no structured rejection-category counts.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH,
                        help=f"seed ground_truth.json (default: {DEFAULT_GROUND_TRUTH})")
    parser.add_argument("--generator-stats", type=Path,
                        help="generator_stats.json (default: next to ground_truth.json)")
    args = parser.parse_args()
    path = args.ground_truth.expanduser().resolve()
    if not path.is_file():
        parser.error(f"ground-truth file not found: {path}")
    stats_path = (args.generator_stats.expanduser().resolve() if args.generator_stats
                  else path.with_name("generator_stats.json"))
    try:
        url = _database_url()
        engine = create_engine(url, pool_pre_ping=True, echo=False)
        with tempfile.TemporaryDirectory(prefix="identity-acceptance-") as temp_dir:
            local = sqlite3.connect(str(Path(temp_dir) / "acceptance.sqlite"))
            _sqlite_database(local)
            metadata = _iter_ground_truth(path, local)
            generator_stats = _load_generator_stats(stats_path)
            _load_active_records(engine, local)
            report = _read_db_report(engine)
            _print_report(metadata, local, report, generator_stats)
            local.close()
        engine.dispose()
        return 0
    except Exception as exc:
        # Do not stringify connection errors: drivers sometimes include URLs.
        if isinstance(exc, (FileNotFoundError, ValueError)):
            print(f"Acceptance report failed: {exc}", file=sys.stderr)
        else:
            print(f"Acceptance report failed ({type(exc).__name__}); "
                  "check database connectivity and schema without sharing credentials.",
                  file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())