"""Known-identifier metadata erasure. Never scan authentication or credential stores."""
import copy
import hmac
import json
import re

from sqlalchemy import text

from app.config import get_settings
from app.privacy.locking import privacy_exclusive

REDACTED = "[privacy-redacted]"
FALSE_DEFINITION = {"version": 1, "root": {"op": "and", "rules": [
    {"type": "trait", "field": "gift_count_total", "operator": "eq", "value": 0},
    {"type": "trait", "field": "gift_count_total", "operator": "neq", "value": 0},
]}}
# Deliberate scope: public configuration, operational text and free-form data.
# Not users, sessions, write keys, credential subtrees, or encrypted/hash columns.
# ingestion_receipts is deliberately absent: only source FK + peppered message
# digest, no partner FK, raw identifiers, JSON, or free-form metadata.
METADATA = {
    "segments": {"name", "description", "definition"},
    "activations": {"name", "field_mapping", "column_order"},
    "destinations": {"name", "config"},
    "sources": {"name", "vendor", "settings"},
    "imports": {"filename", "mapping", "warning_counts"},
    "import_batch_diagnostics": {"diagnostics"},
    "audit_log": {"details"},
    "jobs": {"payload", "progress", "error"},
    "deletion_requests": {"notes", "error"},
    "client_errors": {"message", "route", "component_stack"},
    "source_records": {"attributes", "first_name", "last_name", "address1", "city", "region", "postal_code", "country"},
    "profiles": {"first_name", "last_name", "address1", "city", "region", "postal_code", "country", "search_text"},
    "gifts": {"attributes", "fund", "campaign", "channel"},
    "events": {"name", "properties", "context"},
    "consents": {"evidence"},
    "enrichment_attributes": {"label", "description", "enum_values"},
    "enrichment_values": {"value_text", "value_json"},
    "activation_runs": {"error"},
}
SECRET_KEYS = {"secret", "secrets", "password", "token", "access_token", "refresh_token",
               "api_key", "apikey", "authorization", "credentials", "signing_secret",
               "client_secret", "secret_encrypted", "auth", "authentication"}


def alias_pattern(aliases):
    patterns = []
    for kind, value in sorted(set(aliases)):
        if not value or kind not in {"email", "phone", "external", "email_md5", "email_sha256",
                                      "donor_account_number", "hubspot_record_id", "okta_customer_id", "fusionauth_user_id",
                                      "zeta_cdp_id", "zeta_uid"}:
            continue
        if kind == "email":
            # A larger address/domain is not this identifier. File suffixes are
            # allowed because uploaded/export filenames commonly append .csv.
            patterns.append(r"(?<![\w.+@-])" + re.escape(value) +
                            r"(?![\w@+-]|\.(?!(?:csv|txt|json)\b))")
        elif kind == "external":
            # Only source-qualified IDs; never globally redact bare numeric IDs.
            if ":" in value:
                patterns.append(r"(?<![\w:@-])" + re.escape(value) + r"(?![\w:@-])")
        elif kind == "phone":
            digits = re.sub(r"\D", "", value)
            if not digits:
                continue
            formatted = r"[\s().-]*".join(digits)
            if value.startswith("+1") and len(digits) == 11:
                formatted = r"(?:1[\s().-]*)?" + r"[\s().-]*".join(digits[1:])
            patterns.append(r"(?<![\w\d])\+?" + formatted + r"(?![\w\d])")
        else:
            # Crosswalk values and hashes are identifying even without plain
            # contact details. Restrict matches to complete tokens.
            patterns.append(r"(?<![\w:@-])" + re.escape(value) + r"(?![\w:@-])")
    return "|".join(patterns)


def redactor(aliases):
    pattern = alias_pattern(aliases)
    regex = re.compile(pattern, re.IGNORECASE) if pattern else None

    def redact(value):
        if isinstance(value, str):
            return regex.sub(REDACTED, value) if regex else value
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for index, (key, item) in enumerate(value.items()):
                normalized_key = key.casefold().replace("-", "_")
                if normalized_key in SECRET_KEYS or normalized_key.endswith(("_password", "_secret", "_token", "_api_key")):
                    result[key] = item
                    continue
                clean_key = redact(key)
                if clean_key in result:
                    clean_key += f"#{index}"
                result[clean_key] = redact(item)
            return result
        return value
    return pattern, redact


def redact_definition(definition, redact):
    """Keep unaffected rules; never interpret replacement text as a predicate.

    Empty/scalar affected predicates fail closed in their local boolean context:
    id IS NULL normally, id IS NOT NULL beneath an odd number of NOT groups.
    Both are valid single-rule DSL nodes, even at the depth/rule-count limit.
    """
    def visit(node, negated=False):
        if "rules" in node:
            result = copy.deepcopy(node)
            polarity = negated ^ bool(node.get("negate"))
            result["rules"] = [visit(child, polarity) for child in node["rules"]]
            return result
        if redact(node) == node:
            return copy.deepcopy(node)
        value = node.get("value")
        if node.get("operator") in ("in", "not_in") and isinstance(value, list):
            remaining = [item for item in value if redact(item) == item]
            rest = {key: item for key, item in node.items() if key != "value"}
            if remaining and redact(rest) == rest:
                return {**copy.deepcopy(rest), "value": remaining}
        return {"type": "profile", "field": "id",
                "operator": "is_not_null" if negated else "is_null"}
    return {**copy.deepcopy(definition), "root": visit(definition["root"])}


def redact_known_metadata(db, aliases):
    """Caller commits. Returns aggregate counts only; does not erase profiles.

    Aliases must come from the authorized deletion's identity closure. For a
    completed request, use repair_completed_request below, which verifies its
    stored HMAC before accepting an identifier supplied by an operator.
    """
    from app.privacy.service import schema_inventory, remove_owned_file, audit
    from app.segments.service import graph_lock, references, request_materialization
    from app.activations.security import decrypt

    pattern, redact = redactor(aliases)
    if not pattern:
        return {}
    privacy_exclusive(db)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"))
    graph_lock(db)
    quote = db.get_bind().dialect.identifier_preparer.quote
    counts, touched = {}, {}
    changed_definitions = set()
    inventory = schema_inventory(db)
    id_tables = {column["table_name"] for column in inventory if column["column_name"] == "id"}
    for column in inventory:
        table, name, typ = column["table_name"], column["column_name"], column["data_type"]
        if name not in METADATA.get(table, set()):
            continue
        identifier = "id" if table in id_tables else "NULL"
        rows = db.execute(text(
            f"SELECT tableoid::oid,ctid::text,{identifier},{quote(name)} FROM {quote(table)} "
            f"WHERE CAST({quote(name)} AS text) ~* :pattern"
        ).execution_options(stream_results=True, yield_per=250), {"pattern": pattern.replace(r"\b", r"\y")}).yield_per(250)
        for oid, ctid, row_id, old in rows:
            new = redact(old)
            if new == old:
                continue
            touched.setdefault(table, set()).add(row_id)
            if table == "segments" and name == "definition":
                changed_definitions.add(row_id)
                new = redact_definition(old, redact)
            assignment = "CAST(:value AS jsonb)" if typ in ("json", "jsonb") else ":value"
            db.execute(text(f"UPDATE {quote(table)} SET {quote(name)}={assignment} "
                            "WHERE tableoid=:oid AND ctid=CAST(:ctid AS tid)"),
                       {"value": json.dumps(new) if typ in ("json", "jsonb") else new,
                        "oid": oid, "ctid": ctid})
            key = f"{table}.{name}"
            counts[key] = counts.get(key, 0) + 1
        rows.close()

    # Dependencies remain intact and running. Refresh them and flag review:
    # exclusions of a smaller audience can legitimately gain members.
    definitions = db.execute(text("SELECT id,definition FROM segments")).all() if changed_definitions else []
    while changed_definitions:
        dependents = {sid for sid, definition in definitions if isinstance(definition, dict)
                      and isinstance(definition.get("root"), dict) and references(definition) & changed_definitions}
        additional = dependents - changed_definitions
        if not additional:
            break
        changed_definitions.update(additional)
    if changed_definitions:
        db.execute(text("UPDATE segments SET updated_at=now() WHERE id=ANY(:ids)"),
                   {"ids": sorted(changed_definitions)})
        for segment_id in sorted(changed_definitions):
            if db.scalar(text("SELECT status FROM segments WHERE id=:id"),
                         {"id": segment_id}) != "archived":
                request_materialization(db, segment_id)
    segment_ids = sorted(touched.get("segments", set()) | changed_definitions)
    for segment_id in segment_ids:
        # The immutable audit event is also the durable review issue. Only IDs
        # and booleans, never old/new definitions, names or identifier values.
        audit(db, None, "privacy.segment.review_required", "segment", segment_id,
              {"definition_changed": segment_id in changed_definitions})
    destination_ids = sorted(touched.get("destinations", set()))
    activation_ids = set(touched.get("activations", set()))
    activation_ids.update(db.scalars(text("""SELECT id FROM activations
      WHERE segment_id=ANY(:segments) OR destination_id=ANY(:destinations)"""),
      {"segments": segment_ids, "destinations": destination_ids}))
    if touched.get("jobs"):
        db.execute(text("""UPDATE jobs SET status=CASE WHEN status IN ('queued','running')
          THEN 'cancelled' ELSE status END WHERE id=ANY(:ids)"""), {"ids": sorted(touched["jobs"])})
    # A changed current mapping is not enough: older encrypted snapshots may
    # still contain a removed constant or audience name.
    runs = db.execute(text("""SELECT id,activation_id,file_path,snapshot_encrypted
      FROM activation_runs WHERE snapshot_encrypted IS NOT NULL OR file_path IS NOT NULL OR activation_id=ANY(:ids)"""),
      {"ids": sorted(activation_ids)}).all()
    for run_id, activation_id, path, encrypted in runs:
        snapshot = decrypt(encrypted) if encrypted else None
        if activation_id not in activation_ids and redact(snapshot) == snapshot and redact(path) == path:
            continue
        from app.activations.service import purge_run_artifacts
        purge_run_artifacts(run_id)
        remove_owned_file(path, get_settings().export_dir)
        db.execute(text("""UPDATE activation_runs SET file_path=NULL,file_sha256=NULL,
          snapshot_encrypted=NULL,revoked_at=COALESCE(revoked_at,now()),
          error='Export revoked after an approved privacy request.' WHERE id=:id"""), {"id": run_id})
        counts["exports_revoked"] = counts.get("exports_revoked", 0) + 1
    # Metadata (including an uploaded filename/header) can itself contain PII.
    for imported in db.execute(text("""SELECT id,file_path,error_file_path FROM imports
      WHERE id=ANY(:ids) OR file_path ~* :pattern OR error_file_path ~* :pattern"""),
      {"ids": sorted(touched.get("imports", set())), "pattern": pattern.replace(r"\b", r"\y")}).mappings():
        if (imported["id"] not in touched.get("imports", set())
                and redact(imported["file_path"]) == imported["file_path"]
                and redact(imported["error_file_path"]) == imported["error_file_path"]):
            continue
        remove_owned_file(imported["file_path"], get_settings().upload_dir)
        remove_owned_file(imported["error_file_path"], get_settings().upload_dir)
        db.execute(text("UPDATE imports SET file_path='',error_file_path=NULL WHERE id=:id"), {"id": imported["id"]})
    return counts


def metadata_match_counts(db, aliases):
    """Read-only verification of this module's precise scope; never returns PII."""
    from app.privacy.service import schema_inventory
    from app.activations.security import decrypt
    pattern, redact = redactor(aliases)
    if not pattern:
        return {}
    quote = db.get_bind().dialect.identifier_preparer.quote
    counts = {}
    for column in schema_inventory(db):
        table, name = column["table_name"], column["column_name"]
        if name not in METADATA.get(table, set()):
            continue
        rows = db.execute(text(f"SELECT {quote(name)} FROM {quote(table)} "
                               f"WHERE CAST({quote(name)} AS text) ~* :pattern")
                          .execution_options(stream_results=True, yield_per=250),
                          {"pattern": pattern.replace(r"\b", r"\y")}).yield_per(250)
        found = sum(redact(value) != value for value, in rows)
        rows.close()
        if found:
            counts[f"{table}.{name}"] = found
    for encrypted in db.scalars(text("SELECT snapshot_encrypted FROM activation_runs WHERE snapshot_encrypted IS NOT NULL")):
        snapshot = decrypt(encrypted)
        if redact(snapshot) != snapshot:
            counts["activation_runs.snapshot_encrypted"] = counts.get("activation_runs.snapshot_encrypted", 0) + 1
    for table, columns in (("imports", ("file_path", "error_file_path")), ("activation_runs", ("file_path",))):
        for name in columns:
            found = sum(redact(value) != value for value in db.scalars(text(
                f"SELECT {name} FROM {table} WHERE {name} IS NOT NULL")))
            if found:
                counts[f"{table}.{name}"] = found
    return counts


def repair_completed_request(db, request_id, kind, value):
    """Targeted, audited metadata-only repair; caller commits; no profile recreation."""
    from app.privacy.service import normalize_identifier, digest, audit
    from app.keyring import pii_matches
    privacy_exclusive(db)
    request = db.execute(text("SELECT * FROM deletion_requests WHERE id=:id FOR UPDATE"),
                         {"id": request_id}).mappings().first()
    normalized = normalize_identifier(kind, value)
    if (not request or request["status"] != "completed" or not request["approved_by"]
            or not request["approved_at"] or request["identifier_type"] != kind
            or not pii_matches(normalized, request["identifier_value_hash"])):
        raise ValueError("Metadata repair requires the matching identifier of a completed deletion request.")
    counts = redact_known_metadata(db, {(kind, normalized)})
    audit(db, None, "deletion.metadata_repair", "deletion_request", request_id, counts)
    return counts
