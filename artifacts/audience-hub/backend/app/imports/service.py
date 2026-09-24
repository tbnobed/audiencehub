"""CSV import validation, COPY staging, and idempotent relational upserts."""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import re
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import engine
from app.imports.mapping import RECORD_TYPES
from app.imports.validation import map_and_validate_row

MAX_IMPORT_BYTES = int(os.getenv("IMPORT_MAX_BYTES", str(100 * 1024 * 1024)))
CSV_BATCH_SIZE = 500


class ImportProblem(ValueError):
    """Expected, user-correctable import input problem."""


def _warning_category(message: str) -> str:
    lowered = message.casefold()
    if "invalid email" in lowered:
        return "invalid_email"
    if "invalid phone" in lowered:
        return "invalid_phone"
    if "date format coerced" in lowered:
        return "date_format_coerced"
    if "blocklisted identifier" in lowered:
        return "blocklisted_identifier"
    return "other"


def _row_was_normalized(raw: dict[str, str], columns: dict[str, Any],
                        values: dict[str, Any]) -> bool:
    canonical_targets = {
        "email": "email_norm", "phone": "phone_e164",
        "first_name": "first_name", "last_name": "last_name",
        "postal_code": "postal_code",
    }
    for header, target in columns.items():
        if not isinstance(target, str) or target not in canonical_targets:
            continue
        canonical = values.get(canonical_targets[target])
        if canonical is not None and raw.get(header, "") != canonical:
            return True
    return False


def safe_filename(filename: str | None) -> str:
    name = (filename or "upload.csv").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = "upload.csv"
    if not name.casefold().endswith(".csv"):
        raise ImportProblem("Only CSV files are supported")
    return name[:180]


def csv_reader(path: str | Path) -> tuple[list[str], Iterator[dict[str, str]]]:
    """Open a strict UTF-8 CSV file and return headers and its row iterator."""
    handle = open(path, "r", encoding="utf-8-sig", newline="")
    reader = csv.DictReader(handle, strict=True)
    headers = reader.fieldnames
    if not headers or any(not header or not header.strip() for header in headers):
        handle.close()
        raise ImportProblem("CSV must have a non-empty header row")
    cleaned = [header.strip() for header in headers]
    if len(set(cleaned)) != len(cleaned):
        handle.close()
        raise ImportProblem("CSV contains duplicate headers")

    def rows() -> Iterator[dict[str, str]]:
        try:
            for row_number, row in enumerate(reader, start=2):
                if None in row:
                    raise ImportProblem(f"CSV row {row_number} has more values than headers")
                yield {header.strip(): value or "" for header, value in row.items()}
        except csv.Error as exc:
            raise ImportProblem(f"Malformed CSV: {exc}") from exc
        finally:
            handle.close()
    return cleaned, rows()


def inspect_preview(path: str | Path, count: int = 50) -> tuple[list[str], list[dict[str, str]]]:
    headers, rows = csv_reader(path)
    try:
        return headers, [row for _, row in zip(range(count), rows)]
    finally:
        rows.close()


def validate_file(path: str | Path, columns: dict[str, Any], record_type: str,
                  options: dict[str, Any] | None = None, limit: int = 5000) -> dict[str, Any]:
    if record_type not in RECORD_TYPES:
        raise ImportProblem(f"Unsupported record type: {record_type}")
    headers, rows = csv_reader(path)
    unknown = set(columns) - set(headers)
    if unknown:
        raise ImportProblem(f"Mapping contains unknown CSV headers: {', '.join(sorted(unknown))}")
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    valid = examined = 0
    for line, row in enumerate(rows, start=2):
        if examined >= limit:
            break
        examined += 1
        result = map_and_validate_row(row, columns, record_type, options)
        for message in result["errors"]:
            errors.append({"row": line, "severity": "error", "message": message})
        for message in result["warnings"]:
            warnings.append({"row": line, "severity": "warning", "message": message})
        if not result["errors"]:
            valid += 1
    return {"record_type": record_type, "headers": headers, "rows_checked": examined,
            "rows_ok": valid, "rows_rejected": examined - valid,
            "error_count": len(errors), "warning_count": len(warnings),
            "errors": errors, "warnings": warnings}


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _identity_hash(pepper: str, normalized: str) -> str:
    return hmac.new(pepper.encode(), normalized.encode(), hashlib.sha256).hexdigest()


def _is_hard_bounce(values: dict[str, Any]) -> bool:
    value = values.get("attributes", {}).get("hard_bounce")
    return value is True or (isinstance(value, str) and value.strip().casefold() in {
        "true", "yes", "y", "1", "on",
    })


def _register_hard_bounce_suppressions(db: Session, hashes: set[str]) -> None:
    if not hashes:
        return
    db.execute(text("""
        INSERT INTO suppressions (type, value_hash, reason)
        VALUES ('email', :value_hash, 'hard_bounce')
        ON CONFLICT (type, value_hash) DO UPDATE
          SET reason=EXCLUDED.reason
          WHERE suppressions.reason IS DISTINCT FROM EXCLUDED.reason
    """), [{"value_hash": value_hash} for value_hash in hashes])


def _identifiers_blocked_batch(
    db: Session,
    identities: set[tuple[str | None, str | None]],
    pepper: str,
) -> dict[tuple[str | None, str | None], tuple[bool, bool]]:
    """Resolve suppression and blocklist status for a batch in two DB queries."""
    emails = {email for email, _phone in identities if email}
    phones = {phone for _email, phone in identities if phone}
    if not emails and not phones:
        return {identity: (False, False) for identity in identities}
    email_hash_values: dict[str, set[str]] = {}
    phone_hash_values: dict[str, set[str]] = {}
    for email in emails:
        for value in {email, email.casefold()}:
            email_hash_values.setdefault(_identity_hash(pepper, value), set()).add(email)
    for phone in phones:
        phone_hash_values.setdefault(_identity_hash(pepper, phone), set()).add(phone)

    suppressed_emails: set[str] = set()
    suppressed_phones: set[str] = set()
    if email_hash_values or phone_hash_values:
        rows = db.execute(text("""
            SELECT type, value_hash FROM suppressions
            WHERE (type='email' AND value_hash=ANY(CAST(:email_hashes AS text[])))
               OR (type='phone' AND value_hash=ANY(CAST(:phone_hashes AS text[])))
        """), {"email_hashes": list(email_hash_values),
               "phone_hashes": list(phone_hash_values)}).mappings()
        for row in rows:
            if row["type"] == "email":
                suppressed_emails.update(email_hash_values[row["value_hash"]])
            else:
                suppressed_phones.update(phone_hash_values[row["value_hash"]])

    blocked_emails: set[str] = set()
    blocked_phones: set[str] = set()
    blocklist_rows = db.execute(text("""
        SELECT type, lower(value) AS value FROM identifier_blocklist
        WHERE (type='email' AND (
                 lower(value)=ANY(CAST(:emails AS text[]))
                 OR value IN ('*@test.com', 'noemail@*')))
           OR (type='phone' AND value=ANY(CAST(:phones AS text[])))
    """), {"emails": list(emails), "phones": list(phones)}).mappings()
    wildcard_email_rules: set[str] = set()
    for row in blocklist_rows:
        if row["type"] == "email":
            if row["value"] in {"*@test.com", "noemail@*"}:
                wildcard_email_rules.add(row["value"])
            else:
                blocked_emails.add(row["value"])
        else:
            blocked_phones.add(row["value"])

    results = {}
    for email, phone in identities:
        suppressed = email in suppressed_emails or phone in suppressed_phones
        lowered_email = email.casefold() if email else ""
        blocked_email = lowered_email in blocked_emails
        if "*@test.com" in wildcard_email_rules and lowered_email.endswith("@test.com"):
            blocked_email = True
        if "noemail@*" in wildcard_email_rules and lowered_email.startswith("noemail@"):
            blocked_email = True
        repeated_phone = False
        if phone:
            national = re.sub(r"\D", "", phone)
            if national.startswith("1") and len(national) == 11:
                national = national[1:]
            repeated_phone = len(national) == 10 and len(set(national)) == 1
        blocked = blocked_email or bool(phone and (
            repeated_phone or phone in blocked_phones
        ))
        results[(email, phone)] = (suppressed, blocked)
    return results


def _attribute_values(mapped: dict[str, Any]) -> dict[str, Any]:
    attrs = dict(mapped.get("attributes", {}))
    if mapped.get("email_raw"):
        attrs["email_raw"] = mapped["email_raw"]
    if mapped.get("enrichments"):
        attrs["_import_enrichments"] = {
            key: {"data_type": kind, "value": _json_value(value)}
            for key, (kind, value) in mapped["enrichments"].items()
        }
    if mapped.get("consent"):
        attrs["_import_consent"] = mapped["consent"]
    if mapped.get("properties"):
        attrs["_event_properties"] = mapped["properties"]
    return attrs


def _external_id(record_type: str, values: dict[str, Any]) -> str:
    if values.get("external_id"):
        return values["external_id"].strip()
    if record_type == "contact":
        identifying = "|".join(str(values.get(key) or "") for key in
                               ("email_norm", "phone_e164", "first_name", "last_name"))
        if identifying.strip("|"):
            return "auto:" + hashlib.sha256(identifying.casefold().encode()).hexdigest()
    if record_type in {"gift", "event"}:
        return "auto:" + values["raw_hash"]
    if values.get("contact_external_id"):
        return values["contact_external_id"].strip()
    for key in ("email_norm", "phone_e164"):
        if values.get(key):
            return "auto:" + hashlib.sha256(values[key].casefold().encode()).hexdigest()
    return "auto:" + values["raw_hash"]


def _row_payload(
    record_type: str, values: dict[str, Any], reference_source: str | None = None
) -> dict[str, Any]:
    ext_id = _external_id(record_type, values)
    attributes = _attribute_values(values)
    contact_external_id = values.get("contact_external_id")
    if record_type in {"gift", "enrichment"} and reference_source and contact_external_id:
        attributes["_identity_reference"] = {
            "contact_external_id": contact_external_id,
            "source_key": reference_source,
        }
    if record_type == "consent":
        attributes["_import_consent"] = {
            key: _json_value(values.get(key)) for key in ("channel", "status", "captured_at")
        }
    return {
        "external_id": ext_id,
        "profile_id": None,  # Identity resolution is a later milestone.
        "email_norm": values.get("email_norm"),
        "phone_e164": values.get("phone_e164"),
        "first_name": values.get("first_name"),
        "last_name": values.get("last_name"),
        "address1": values.get("address1"),
        "address2": values.get("address2"),
        "city": values.get("city"),
        "region": values.get("region"),
        "postal_code": values.get("postal_code"),
        "country": values.get("country"),
        "attributes": attributes,
        "raw_hash": values["raw_hash"],
        "gift_external_id": values.get("external_id") if record_type == "gift" else ext_id,
        "event_message_id": (values.get("message_id") or values["raw_hash"])
        if record_type == "event" else None,
    }


def _upsert_source_records(db: Session, source_id: int, import_id: int,
                           records: list[dict[str, Any]]) -> dict[str, int]:
    if not records:
        return {}
    records = list({row["external_id"]: row for row in records}.values())
    statement = text("""
        INSERT INTO source_records
          (source_id, external_id, profile_id, email_norm, phone_e164, first_name, last_name,
           address1, address2, city, region, postal_code, country, attributes, raw_hash, last_import_id)
        VALUES
          (:source_id, :external_id, NULL, :email_norm, :phone_e164, :first_name, :last_name,
           :address1, :address2, :city, :region, :postal_code, :country,
           CAST(:attributes AS jsonb), :raw_hash, :last_import_id)
        ON CONFLICT (source_id, external_id) DO UPDATE SET
          email_norm=EXCLUDED.email_norm, phone_e164=EXCLUDED.phone_e164,
          first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name,
          address1=EXCLUDED.address1, address2=EXCLUDED.address2, city=EXCLUDED.city,
          region=EXCLUDED.region, postal_code=EXCLUDED.postal_code, country=EXCLUDED.country,
          attributes=EXCLUDED.attributes, raw_hash=EXCLUDED.raw_hash,
          last_import_id=EXCLUDED.last_import_id, updated_at=now()
        WHERE source_records.raw_hash IS DISTINCT FROM EXCLUDED.raw_hash
    """)
    params = []
    for row in records:
        params.append({
            "source_id": source_id, "last_import_id": import_id,
            **{key: row.get(key) for key in ("external_id", "email_norm", "phone_e164",
               "first_name", "last_name", "address1", "address2", "city", "region",
               "postal_code", "country", "raw_hash")},
            "attributes": json.dumps(row["attributes"], default=_json_value),
        })
    db.execute(statement, params)
    ids = db.execute(text(
        "SELECT external_id, id FROM source_records WHERE source_id=:source_id "
        "AND external_id = ANY(:external_ids)"
    ), {"source_id": source_id, "external_ids": [row["external_id"] for row in records]})
    return {external_id: record_id for external_id, record_id in ids}


def _insert_gifts(db: Session, source_id: int, records: list[dict[str, Any]]) -> None:
    items = list({(row["gift_external_id"]): row for row in records
                  if row["record_type"] == "gift"}.values())
    if not items:
        return
    params = []
    for row in items:
        v = row["values"]
        params.append({
            "source_id": source_id, "external_id": row["gift_external_id"],
            "source_record_id": row["source_record_id"], "amount": v["amount"],
            "gift_date": v["gift_date"], "fund": v.get("fund"), "campaign": v.get("campaign"),
            "appeal_code": v.get("appeal_code"), "channel": v.get("channel"),
            "payment_method": v.get("payment_method"), "is_recurring": v.get("is_recurring", False),
            "recurring_plan_id": v.get("recurring_plan_id"),
            "attributes": json.dumps(_attribute_values(v), default=_json_value),
        })
    db.execute(text("""
        INSERT INTO gifts
          (source_id, external_id, profile_id, source_record_id, amount, currency, gift_date,
           fund, campaign, appeal_code, channel, payment_method, is_recurring, recurring_plan_id, attributes)
        VALUES
          (:source_id, :external_id, NULL, :source_record_id, :amount, 'USD', :gift_date,
           :fund, :campaign, :appeal_code, :channel, :payment_method, :is_recurring, :recurring_plan_id,
           CAST(:attributes AS jsonb))
        ON CONFLICT (source_id, external_id) DO UPDATE SET
          source_record_id=EXCLUDED.source_record_id, amount=EXCLUDED.amount, gift_date=EXCLUDED.gift_date,
          fund=EXCLUDED.fund, campaign=EXCLUDED.campaign, appeal_code=EXCLUDED.appeal_code,
          channel=EXCLUDED.channel, payment_method=EXCLUDED.payment_method,
          is_recurring=EXCLUDED.is_recurring, recurring_plan_id=EXCLUDED.recurring_plan_id,
          attributes=EXCLUDED.attributes, updated_at=now()
        WHERE (gifts.source_record_id, gifts.amount, gifts.gift_date, gifts.fund, gifts.campaign,
               gifts.appeal_code, gifts.channel, gifts.payment_method, gifts.is_recurring,
               gifts.recurring_plan_id, gifts.attributes)
          IS DISTINCT FROM
              (EXCLUDED.source_record_id, EXCLUDED.amount, EXCLUDED.gift_date, EXCLUDED.fund,
               EXCLUDED.campaign, EXCLUDED.appeal_code, EXCLUDED.channel, EXCLUDED.payment_method,
               EXCLUDED.is_recurring, EXCLUDED.recurring_plan_id, EXCLUDED.attributes)
    """), params)


def _insert_events(db: Session, source_id: int, records: list[dict[str, Any]]) -> None:
    items = list({(row["event_message_id"], row["values"]["occurred_at"]): row for row in records
                  if row["record_type"] == "event"}.values())
    if not items:
        return
    params = []
    for row in items:
        v = row["values"]
        params.append({
            "source_id": source_id, "message_id": row["event_message_id"],
            "type": v.get("type") or "track", "name": v["name"],
            "occurred_at": v["occurred_at"], "received_at": v.get("received_at"),
            "properties": json.dumps(v.get("properties", {}), default=_json_value),
            "context": json.dumps(v.get("context", {}), default=_json_value),
        })
    db.execute(text("""
        INSERT INTO events
          (source_id, type, name, properties, context, occurred_at, received_at, message_id)
        VALUES (:source_id, :type, :name, CAST(:properties AS jsonb), CAST(:context AS jsonb),
                :occurred_at, COALESCE(:received_at, :occurred_at), :message_id)
        ON CONFLICT (source_id, message_id, occurred_at) DO UPDATE SET
          type=EXCLUDED.type, name=EXCLUDED.name, properties=EXCLUDED.properties,
          context=EXCLUDED.context, received_at=EXCLUDED.received_at
        WHERE (events.type, events.name, events.properties, events.context, events.received_at)
          IS DISTINCT FROM
          (EXCLUDED.type, EXCLUDED.name, EXCLUDED.properties, EXCLUDED.context, EXCLUDED.received_at)
    """), params)


def _register_enrichments(db: Session, source_id: int, records: list[dict[str, Any]]) -> None:
    keyed: dict[str, str] = {}
    for row in records:
        for key, (data_type, _) in row["values"].get("enrichments", {}).items():
            if key in keyed and keyed[key] != data_type:
                raise ImportProblem(f"Enrichment {key} has conflicting data types within import")
            keyed[key] = data_type
    for key, data_type in keyed.items():
        db.execute(text("""
            INSERT INTO enrichment_attributes (source_id, key, label, data_type, is_active)
            VALUES (:source_id, :key, :label, :data_type, true)
            ON CONFLICT (source_id, key) DO UPDATE SET data_type=EXCLUDED.data_type,
              label=EXCLUDED.label, is_active=true, updated_at=now()
            WHERE enrichment_attributes.data_type IS DISTINCT FROM EXCLUDED.data_type
        """), {"source_id": source_id, "key": key, "label": key.replace("_", " ").title(),
               "data_type": data_type})
    insert_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for row in records:
        if not row["profile_id"]:
            continue
        for key, (data_type, value) in row["values"].get("enrichments", {}).items():
            insert_by_key[(row["profile_id"], key)] = {
                "profile_id": row["profile_id"], "source_id": source_id, "key": key,
                "data_type": data_type, "value": value,
            }
    insert_rows = list(insert_by_key.values())
    if not insert_rows:
        return
    db.execute(text("""
        INSERT INTO enrichment_values
          (profile_id, source_id, attribute_key, value_text, value_num, value_bool, value_date, imported_at)
        VALUES
          (:profile_id, :source_id, :key,
           CASE WHEN :data_type IN ('text','enum') THEN CAST(:value AS text) END,
           CASE WHEN :data_type='number' THEN CAST(:value AS numeric) END,
           CASE WHEN :data_type='boolean' THEN CAST(:value AS boolean) END,
           CASE WHEN :data_type='date' THEN CAST(:value AS date) END, now())
        ON CONFLICT (profile_id, source_id, attribute_key) DO UPDATE SET
          value_text=EXCLUDED.value_text, value_num=EXCLUDED.value_num,
          value_bool=EXCLUDED.value_bool, value_date=EXCLUDED.value_date, imported_at=now()
    """), [{
        **item, "value": _json_value(item["value"])
    } for item in insert_rows])


def _upsert_consents(db: Session, source_id: int, records: list[dict[str, Any]]) -> None:
    latest: dict[tuple[int, str], dict[str, Any]] = {}
    for row in records:
        values = row["values"]
        if row["record_type"] != "consent" or not row["profile_id"]:
            continue
        item = {
            "profile_id": row["profile_id"], "source_id": source_id,
            "channel": values["channel"], "status": values["status"],
            "captured_at": values["captured_at"],
            "evidence": json.dumps({"source_record_id": row["source_record_id"]}),
        }
        key = (item["profile_id"], item["channel"])
        previous = latest.get(key)
        if not previous or item["captured_at"] > previous["captured_at"] or (
                item["captured_at"] == previous["captured_at"]
                and item["status"] == "opted_out" and previous["status"] != "opted_out"):
            latest[key] = item
    params = list(latest.values())
    if not params:
        return
    db.execute(text("""
        INSERT INTO consents (profile_id, channel, status, source_id, captured_at, evidence)
        VALUES (:profile_id, :channel, :status, :source_id, :captured_at, CAST(:evidence AS jsonb))
        ON CONFLICT (profile_id, channel) DO UPDATE SET status=EXCLUDED.status,
          source_id=EXCLUDED.source_id, captured_at=EXCLUDED.captured_at, evidence=EXCLUDED.evidence
        WHERE consents.captured_at < EXCLUDED.captured_at
           OR (consents.captured_at = EXCLUDED.captured_at
               AND EXCLUDED.status='opted_out' AND consents.status <> 'opted_out')
    """), params)


class _ErrorCsv:
    def __init__(self, directory: Path, import_id: int):
        self.path = directory / f"import-{import_id}-errors.csv"
        self.handle = self.path.open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.handle, fieldnames=("row", "severity", "message"))
        self.writer.writeheader()
        self.count = 0

    def write(self, row: dict[str, Any]) -> None:
        self.writer.writerow(row)
        self.count += 1

    def finish(self) -> str | None:
        self.handle.flush()
        self.handle.close()
        if not self.count:
            self.path.unlink(missing_ok=True)
            return None
        return str(self.path)


def run_import(import_id: int, progress_callback: Callable[[dict[str, Any]], None] | None = None,
               db: Session | None = None) -> dict[str, Any]:
    """Execute an import. Pass a worker-owned SQLAlchemy Session when available.

    The job runner should call this for ``import.run``. The optional callback is
    invoked with ``{"done", "total", "message"}`` as batches complete.
    """
    started = time.perf_counter()
    owns_session = db is None
    if owns_session:
        db = Session(engine)
    assert db is not None
    error_sink: _ErrorCsv | None = None
    try:
        imported = db.execute(text(
            "SELECT id, source_id, filename, file_path, record_type, mapping, rows_total "
            "FROM imports WHERE id=:id FOR UPDATE"
        ), {"id": import_id}).mappings().first()
        if not imported:
            raise ImportProblem("Import not found")
        record_type = imported["record_type"]
        mapping = imported["mapping"] or {}
        columns = mapping.get("columns", {})
        options = mapping.get("options", {})
        path = Path(imported["file_path"])
        if not path.is_file():
            raise ImportProblem("Uploaded CSV is no longer available")
        headers, rows = csv_reader(path)
        if set(columns) - set(headers):
            raise ImportProblem("Saved mapping does not match the uploaded CSV headers")
        total = imported["rows_total"] or 0
        if not total:
            total = sum(1 for _ in rows)
            headers, rows = csv_reader(path)
        db.execute(text(
            "UPDATE imports SET status='running', started_at=now(), rows_total=:total "
            "WHERE id=:id"
        ), {"id": import_id, "total": total})
        db.flush()

        # Use PostgreSQL COPY for a durable, bounded-memory staging pass. Each
        # row is JSON so downstream record-type validation can evolve safely.
        conn = db.connection()
        raw_connection = conn.connection.driver_connection
        with raw_connection.cursor() as cursor:
            cursor.execute("""
                CREATE TEMP TABLE ah_import_staging (
                  record_number bigint NOT NULL, raw_row jsonb NOT NULL, raw_hash text NOT NULL
                ) ON COMMIT DROP
            """)
            with cursor.copy(
                "COPY ah_import_staging (record_number, raw_row, raw_hash) FROM STDIN"
            ) as copy:
                for record_number, row in enumerate(rows, start=2):
                    raw = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    copy.write_row((record_number, raw, hashlib.sha256(raw.encode()).hexdigest()))
            cursor.execute("CREATE INDEX ON ah_import_staging (record_number)")
        db.flush()

        settings = get_settings()
        pepper = settings.pii_hash_pepper
        Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
        error_sink = _ErrorCsv(Path(settings.upload_dir), import_id)
        # Register every hard-bounced address before accepting any row. This
        # makes suppression independent of CSV order, including duplicate
        # contacts earlier in the same file.
        bounce_hashes: set[str] = set()
        bounce_last_record_number = 1
        while True:
            staged_bounces = db.execute(text("""
                SELECT record_number, raw_row FROM ah_import_staging
                WHERE record_number > :last_record_number
                ORDER BY record_number LIMIT :limit
            """), {"last_record_number": bounce_last_record_number,
                   "limit": CSV_BATCH_SIZE}).mappings().all()
            if not staged_bounces:
                break
            bounce_last_record_number = staged_bounces[-1]["record_number"]
            for staged_row in staged_bounces:
                raw = staged_row["raw_row"]
                if isinstance(raw, str):
                    raw = json.loads(raw)
                values = map_and_validate_row(raw, columns, record_type, options)["values"]
                email = values.get("email_norm")
                if _is_hard_bounce(values) and email:
                    bounce_hashes.add(_identity_hash(pepper, email))
        _register_hard_bounce_suppressions(db, bounce_hashes)

        accepted = rejected = seen = 0
        warning_count = normalized_count = deduplicated_count = 0
        warning_counts: dict[str, int] = {}
        last_record_number = 1
        for _ in range(0, total, CSV_BATCH_SIZE):
            staged = db.execute(text("""
                SELECT record_number, raw_row FROM ah_import_staging
                WHERE record_number > :last_record_number
                ORDER BY record_number LIMIT :limit
            """), {"last_record_number": last_record_number, "limit": CSV_BATCH_SIZE}).mappings().all()
            if not staged:
                break
            last_record_number = staged[-1]["record_number"]
            processed: list[dict[str, Any]] = []
            source_rows: list[dict[str, Any]] = []
            batch_rows: list[tuple[int, dict[str, str], dict[str, Any], dict[str, Any]]] = []
            identities: set[tuple[str | None, str | None]] = set()
            for staged_row in staged:
                seen += 1
                raw = staged_row["raw_row"]
                if isinstance(raw, str):
                    raw = json.loads(raw)
                converted = map_and_validate_row(raw, columns, record_type, options)
                line = staged_row["record_number"]
                values = converted["values"]
                batch_rows.append((line, raw, converted, values))
                if converted["errors"] or (
                    record_type == "contact" and _is_hard_bounce(values)
                ):
                    continue
                identities.add((values.get("email_norm"), values.get("phone_e164")))

            identity_checks = _identifiers_blocked_batch(db, identities, pepper)
            for line, raw, converted, values in batch_rows:
                for message in converted["errors"]:
                    error_sink.write({"row": line, "severity": "error", "message": message})
                for message in converted["warnings"]:
                    error_sink.write({"row": line, "severity": "warning", "message": message})
                    warning_count += 1
                    category = _warning_category(message)
                    warning_counts[category] = warning_counts.get(category, 0) + 1
                if converted["errors"]:
                    rejected += 1
                    continue
                if record_type == "contact" and _is_hard_bounce(values):
                    rejected += 1
                    error_sink.write({"row": line, "severity": "error",
                                      "message": "Hard-bounce row was not imported"})
                    continue
                if _row_was_normalized(raw, columns, values):
                    normalized_count += 1
                identity_key = (values.get("email_norm"), values.get("phone_e164"))
                suppressed, blocklisted = identity_checks[identity_key]
                if suppressed:
                    rejected += 1
                    error_sink.write({"row": line, "severity": "error",
                                      "message": "Identifier is suppressed; row was not imported"})
                    continue
                if blocklisted:
                    values["email_norm"] = None
                    values["phone_e164"] = None
                    error_sink.write({"row": line, "severity": "warning",
                                      "message": "Blocklisted identifier excluded from identity matching"})
                    warning_count += 1
                    warning_counts["blocklisted_identifier"] = (
                        warning_counts.get("blocklisted_identifier", 0) + 1
                    )
                item = _row_payload(record_type, values, options.get("reference_source"))
                item.update(record_type=record_type, values=values, line=line)
                processed.append(item)
                source_rows.append(item)
            # Count duplicate external IDs collapsed by this batch's unique-key
            # upsert without retaining identifiers across the entire file.
            ids_in_batch: set[str] = set()
            for item in processed:
                if item["external_id"] in ids_in_batch:
                    deduplicated_count += 1
                else:
                    ids_in_batch.add(item["external_id"])
            id_map = _upsert_source_records(db, imported["source_id"], import_id, source_rows)
            record_ids = list(id_map.values())
            profiles: dict[int, int | None] = {}
            if record_ids:
                profiles = {record_id: profile_id for record_id, profile_id in db.execute(text(
                    "SELECT id, profile_id FROM source_records WHERE id=ANY(:ids)"
                ), {"ids": record_ids})}
            for item in processed:
                item["source_record_id"] = id_map[item["external_id"]]
                item["profile_id"] = profiles.get(item["source_record_id"])
            _insert_gifts(db, imported["source_id"], processed)
            _insert_events(db, imported["source_id"], processed)
            _register_enrichments(db, imported["source_id"], processed)
            _upsert_consents(db, imported["source_id"], processed)
            accepted += len(processed)
            db.execute(text(
                "UPDATE imports SET rows_ok=:ok, rows_rejected=:rejected, "
                "warning_count=:warning_count, warning_counts=CAST(:warning_counts AS jsonb), "
                "rows_normalized=:normalized, rows_deduplicated=:deduplicated WHERE id=:id"
            ), {"ok": accepted, "rejected": rejected, "warning_count": warning_count,
                "warning_counts": json.dumps(warning_counts), "normalized": normalized_count,
                "deduplicated": deduplicated_count, "id": import_id})
            db.flush()
            if progress_callback:
                progress_callback({"done": min(seen, total), "total": total,
                                   "message": f"Processed {min(seen, total)} of {total} rows"})
        error_path = error_sink.finish()
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        db.execute(text("""
            UPDATE imports SET status='completed', rows_total=:total, rows_ok=:ok,
              rows_rejected=:rejected, warning_count=:warning_count,
              warning_counts=CAST(:warning_counts AS jsonb), rows_normalized=:normalized,
              rows_deduplicated=:deduplicated, duration_ms=:duration_ms,
              error_file_path=:error_path, finished_at=now()
            WHERE id=:id
        """), {"total": seen, "ok": accepted, "rejected": rejected,
               "warning_count": warning_count, "warning_counts": json.dumps(warning_counts),
               "normalized": normalized_count, "deduplicated": deduplicated_count,
               "duration_ms": elapsed_ms, "error_path": error_path, "id": import_id})
        if record_type in {"contact", "consent", "gift", "event"}:
            from app.jobs.queue import enqueue

            enqueue(db, "identity.resolve_batch", {}, dedupe_key="identity")
        db.commit()
        return {"rows_total": seen, "rows_ok": accepted, "rows_rejected": rejected,
                "warning_count": warning_count, "warning_counts": warning_counts,
                "rows_normalized": normalized_count, "rows_deduplicated": deduplicated_count,
                "duration_ms": elapsed_ms}
    except Exception as exc:
        db.rollback()
        # Never persist raw exception text from database errors, which can
        # contain values. Keep the import status useful without exposing PII.
        try:
            db.execute(text(
                "UPDATE imports SET status='failed', duration_ms=:duration_ms, "
                "finished_at=now() WHERE id=:id"
            ), {"id": import_id,
                "duration_ms": max(0, int((time.perf_counter() - started) * 1000))})
            db.commit()
        except Exception:
            db.rollback()
        if isinstance(exc, ImportProblem):
            raise
        raise
    finally:
        if error_sink is not None and not error_sink.handle.closed:
            error_sink.handle.close()
        if owns_session:
            db.close()