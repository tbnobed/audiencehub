"""Privacy writes.

execute_deletion is caller-transactional: the entire matched identity closure,
including every individual partner, commits atomically. Export/upload revocation
is fail-closed; files removed before a later rollback remain unavailable, never
reappear as a privacy leak. Retrying an interrupted request is safe.
"""
import hashlib
import hmac
import json
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

from sqlalchemy import text
from app.config import get_settings
from app.identity.normalize import normalize_email, normalize_phone
from app.privacy.locking import privacy_exclusive

CHANNELS = ("email", "sms", "phone", "mail", "ads_personalization")
KNOWN_REFERENCES = {
    "profiles": {"merged_into_id"}, "source_records": {"profile_id"},
    "identifiers": {"profile_id"}, "profile_merges": {"winner_id", "loser_id"},
    "gifts": {"profile_id"}, "events": {"profile_id"}, "consents": {"profile_id"},
    "enrichment_values": {"profile_id"}, "profile_traits": {"profile_id"},
    "trait_dirty_profiles": {"profile_id"}, "segment_membership": {"profile_id"},
    "client_errors": {"profile_id"}, "activation_run_members": {"profile_id"},
    "activation_last_members": {"profile_id"},
    "dashboard_donor_daily": {"profile_id"},
    "identity_merge_journal": {"winner_id", "loser_id", "new_profile_id"},
    "identity_xref": {"master_id"},
    "identity_addresses": {"profile_id"}, "identity_devices": {"profile_id"},
    "identity_pairs": {"left_id", "right_id"},
}
PROFILE_KEYS = {"profile_id", "profile_ids", "matched_profile_ids", "winner_id", "loser_id", "merged_into_id"}


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hmac.new(get_settings().pii_hash_pepper.encode(), value.encode(), hashlib.sha256).hexdigest()


def normalize_identifier(kind, value):
    normalized = normalize_email(value) if kind == "email" else normalize_phone(value)
    if not normalized:
        raise ValueError("Enter a valid email address or phone number.")
    return normalized


def audit(db, actor, action, entity_type, entity_id, counts=None):
    db.execute(text("""INSERT INTO audit_log(user_id,actor_type,action,entity_type,entity_id,details)
      VALUES(:actor,:actor_type,:action,:kind,:id,CAST(:details AS jsonb))"""),
      {"actor": actor, "actor_type": "user" if actor else "system", "action": action,
       "kind": entity_type, "id": str(entity_id), "details": json.dumps(counts or {})})


def manual_source(db):
    source_id = db.scalar(text("""INSERT INTO sources(key,name,kind,record_types,priority,settings,is_active)
      VALUES('privacy_manual','Manual privacy decisions','csv',ARRAY['consent'],0,
       jsonb_build_object('authoritative_for_consent',true,'internal_privacy',true),false)
      ON CONFLICT(key) DO UPDATE SET is_active=false,
        settings=sources.settings || jsonb_build_object('authoritative_for_consent',true)
      WHERE sources.settings->>'internal_privacy'='true'
      RETURNING id"""))
    if source_id is None:
        raise ValueError("The reserved privacy source key is already in use; an admin must resolve the conflict.")
    return source_id


def partner_consents(db, profile_id):
    if not db.scalar(text("SELECT EXISTS(SELECT 1 FROM active_profiles WHERE id=:id)"), {"id": profile_id}):
        raise LookupError("Partner not found.")
    rows = {r["channel"]: dict(r) for r in db.execute(text(
        "SELECT channel,status,captured_at,source_id FROM consents WHERE profile_id=:id"
    ), {"id": profile_id}).mappings()}
    return [rows.get(c, {"channel": c, "status": "unknown", "captured_at": None, "source_id": None}) for c in CHANNELS]


def change_consent(db, profile_id, channel, status, note, actor):
    privacy_exclusive(db)
    partner_consents(db, profile_id)
    if len(note.strip()) < 10:
        raise ValueError("A note of at least 10 characters is required for a manual consent change.")
    source = manual_source(db)
    if channel == "email" and status == "opted_in":
        values = aliases(db, [profile_id])
        from app.keyring import pii_hashes
        hashes = [hashed for kind, v in values if kind == "email" for hashed in pii_hashes(v)]
        if db.scalar(text("""SELECT EXISTS(SELECT 1 FROM suppressions
          WHERE type='email' AND value_hash=ANY(:hashes))"""), {"hashes": hashes}):
            raise ValueError("Remove the email suppression before explicitly opting this partner in.")
    db.execute(text("""INSERT INTO consents(profile_id,channel,status,source_id,captured_at,evidence)
      VALUES(:id,:channel,:status,:source,:at,CAST(:evidence AS jsonb))
      ON CONFLICT(profile_id,channel) DO UPDATE SET status=EXCLUDED.status,source_id=EXCLUDED.source_id,
      captured_at=EXCLUDED.captured_at,evidence=EXCLUDED.evidence
      WHERE consent_candidate_wins(consents.status,consents.captured_at,consents.source_id,
        EXCLUDED.status,EXCLUDED.captured_at,EXCLUDED.source_id)"""),
      {"id": profile_id, "channel": channel, "status": status, "source": source, "at": now(),
       "evidence": json.dumps({"manual": True, "note": note.strip(), "actor_id": actor})})
    db.execute(text("""INSERT INTO trait_dirty_profiles(profile_id,dirtied_at) VALUES(:id,clock_timestamp())
      ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at"""), {"id": profile_id})
    audit(db, actor, "consent.change", "profile", profile_id, {"channel": channel, "status": status})
    return next(r for r in partner_consents(db, profile_id) if r["channel"] == channel)


def aliases(db, ids):
    rows = db.execute(text("""SELECT 'email' AS kind,lower(email::text) AS value FROM profiles WHERE id=ANY(:ids)
      UNION SELECT 'phone',phone FROM profiles WHERE id=ANY(:ids)
      UNION SELECT type,value FROM identifiers WHERE profile_id=ANY(:ids) AND type IN ('email','phone','external')
      UNION SELECT CASE x.key_type WHEN 'mobile_phone' THEN 'phone' ELSE x.key_type END,x.key_value
        FROM identity_xref x JOIN profiles p ON p.master_id=x.master_id WHERE p.id=ANY(:ids)
      UNION SELECT 'email',lower(email_norm::text) FROM source_records WHERE profile_id=ANY(:ids)
      UNION SELECT 'phone',phone_e164 FROM source_records WHERE profile_id=ANY(:ids)
      UNION SELECT 'external',s.key || ':' || sr.external_id FROM source_records sr JOIN sources s ON s.id=sr.source_id
        WHERE sr.profile_id=ANY(:ids)
      UNION SELECT 'external',s.key || ':' || g.external_id FROM gifts g JOIN sources s ON s.id=g.source_id
        WHERE g.profile_id=ANY(:ids)
      UNION SELECT 'external',s.key || ':' || e.message_id FROM events e JOIN sources s ON s.id=e.source_id
        WHERE e.profile_id=ANY(:ids) AND e.message_id IS NOT NULL
      UNION SELECT 'external',sr.attributes->'_identity_reference'->>'source_key' || ':' ||
        (sr.attributes->'_identity_reference'->>'contact_external_id') FROM source_records sr
        WHERE sr.profile_id=ANY(:ids)"""), {"ids": ids})
    return {(kind, value) for kind, value in rows if value}


def matching_ids(db, kind, value):
    column = "email_norm" if kind == "email" else "phone_e164"
    profile_col = "email" if kind == "email" else "phone"
    return list(db.scalars(text(f"""WITH RECURSIVE edges(a,b) AS (
        SELECT id,merged_into_id FROM profiles WHERE merged_into_id IS NOT NULL
        UNION SELECT winner_id,loser_id FROM profile_merges
      ), initial(id) AS (
        SELECT id FROM profiles WHERE {profile_col}=:value
        UNION SELECT profile_id FROM identifiers WHERE type=:kind AND value=:value
        UNION SELECT profile_id FROM source_records WHERE {column}=:value AND profile_id IS NOT NULL
        UNION SELECT i.profile_id FROM source_records sr JOIN sources s ON s.id=sr.source_id
          JOIN identifiers i ON
            (i.type='user_id' AND i.value=s.key || ':' || (sr.attributes->>'user_id')) OR
            (i.type='anonymous_id' AND i.value=sr.attributes->>'anonymous_id')
          WHERE s.kind='event_api' AND sr.{column}=:value
      ), closure(id) AS (
        SELECT id FROM initial UNION
        SELECT CASE WHEN e.a=c.id THEN e.b ELSE e.a END FROM closure c
        JOIN edges e ON e.a=c.id OR e.b=c.id
      ) SELECT id FROM closure ORDER BY id"""), {"kind": kind, "value": value}))


def add_suppression(db, kind, value, actor):
    privacy_exclusive(db)
    normalized = normalize_identifier(kind, value)
    hashed = digest(normalized)
    row = db.execute(text("""INSERT INTO suppressions(type,value_hash,reason) VALUES(:kind,:hash,'manual')
      ON CONFLICT(type,value_hash) DO UPDATE SET reason=suppressions.reason RETURNING *"""),
      {"kind": kind, "hash": hashed}).mappings().one()
    if kind == "email":
        for pid in matching_ids(db, kind, normalized):
            if db.scalar(text("SELECT EXISTS(SELECT 1 FROM active_profiles WHERE id=:id)"), {"id": pid}):
                change_consent(db, pid, "email", "opted_out", "Manual email suppression.", actor)
    audit(db, actor, "suppression.create", "suppression", row["id"], {"type": kind})
    return dict(row)


def remove_suppression(db, suppression_id, actor):
    privacy_exclusive(db)
    row = db.execute(text("SELECT * FROM suppressions WHERE id=:id FOR UPDATE"), {"id": suppression_id}).mappings().first()
    if not row:
        raise LookupError("Suppression not found.")
    if row["reason"] == "deletion_request":
        raise ValueError("Deletion-request suppressions cannot be removed.")
    db.execute(text("DELETE FROM suppressions WHERE id=:id"), {"id": suppression_id})
    audit(db, actor, "suppression.remove", "suppression", suppression_id)


def schema_inventory(db):
    """Inspect every physical table in our schema, excluding partition children."""
    return db.execute(text("""SELECT c.relname AS table_name,a.attname AS column_name,t.typname AS data_type,
      EXISTS(SELECT 1 FROM pg_constraint f WHERE f.contype='f' AND f.conrelid=c.oid
        AND a.attnum=ANY(f.conkey) AND f.confrelid='profiles'::regclass) AS profile_fk
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
      JOIN pg_type t ON t.oid=a.atttypid
      WHERE n.nspname=current_schema() AND c.relkind IN ('r','p') AND NOT c.relispartition
      ORDER BY c.relname,a.attnum""")).mappings().all()


def check_inventory(db):
    rows = schema_inventory(db)
    for row in rows:
        table, column = row["table_name"], row["column_name"]
        if row["profile_fk"] or column in PROFILE_KEYS:
            if column not in KNOWN_REFERENCES.get(table, set()) and not (
                table == "deletion_requests" and column == "matched_profile_ids"
            ):
                raise ValueError("Unreviewed partner reference in database schema; privacy execution refused.")
    return rows


def counts_for(db, ids):
    counts = {"profiles": len(ids)}
    present = {r["table_name"] for r in check_inventory(db)}
    quote = db.get_bind().dialect.identifier_preparer.quote
    for table, columns in KNOWN_REFERENCES.items():
        if table == "identity_xref":
            continue
        if table == "profiles" or table not in present:
            continue
        predicate = " OR ".join(f"{quote(c)}=ANY(:ids)" for c in sorted(columns))
        counts[table] = db.scalar(text(f"SELECT count(*) FROM {quote(table)} WHERE {predicate}"), {"ids": ids})
    return counts


def preview_deletion(db, kind, value):
    ids = matching_ids(db, kind, normalize_identifier(kind, value))
    return {"matched_partners": len(ids), "counts": counts_for(db, ids),
            "backup_keep_days": get_settings().backup_keep_days}


def create_deletion(db, kind, value, actor):
    privacy_exclusive(db)
    normalized = normalize_identifier(kind, value)
    ids = matching_ids(db, kind, normalized)
    check_inventory(db)
    row = db.execute(text("""INSERT INTO deletion_requests(requested_by,identifier_type,identifier_value_hash,
      matched_profile_ids,status,result_counts,identifier_encrypted)
      VALUES(:actor,:kind,:hash,:ids,'pending',CAST(:counts AS json),:encrypted) RETURNING *"""),
      {"actor": actor, "kind": kind, "hash": digest(normalized), "ids": ids,
       "counts": json.dumps(counts_for(db, ids)),
       "encrypted": Fernet(get_settings().fernet_key.encode()).encrypt(normalized.encode())}).mappings().one()
    audit(db, actor, "deletion.request", "deletion_request", row["id"], {"matched_partners": len(ids)})
    return dict(row)


def approve_deletion(db, request_id, actor):
    from app.jobs.queue import enqueue
    privacy_exclusive(db)
    row = db.execute(text("SELECT * FROM deletion_requests WHERE id=:id FOR UPDATE"), {"id": request_id}).mappings().first()
    if not row:
        raise LookupError("Deletion request not found.")
    if not db.scalar(text("SELECT EXISTS(SELECT 1 FROM users WHERE id=:id AND role='admin' AND is_active)"), {"id": actor}):
        raise ValueError("An active admin must approve this request.")
    if get_settings().deletion_two_person and actor == row["requested_by"]:
        raise ValueError("A different admin must approve this deletion request.")
    if row["status"] not in ("pending", "approved", "failed"):
        raise ValueError("This deletion request cannot be approved.")
    db.execute(text("""UPDATE deletion_requests SET approved_by=:actor,approved_at=now(),
      status='approved',error=NULL WHERE id=:id"""), {"actor": actor, "id": request_id})
    job = enqueue(db, "deletion.execute", {"request_id": request_id}, dedupe_key=f"deletion:{request_id}")
    audit(db, actor, "deletion.approve", "deletion_request", request_id)
    return job.id if hasattr(job, "id") else job


def has_reference(value, ids, parent=None):
    if isinstance(value, dict):
        return any(has_reference(v, ids, k) for k, v in value.items())
    if isinstance(value, list):
        return any(has_reference(v, ids, parent) for v in value)
    return parent in PROFILE_KEYS and str(value) in {str(i) for i in ids}


def scrub_references(value, ids, parent=None):
    if isinstance(value, dict):
        return {k: scrub_references(v, ids, k) for k, v in value.items()
                if not (k in PROFILE_KEYS and not isinstance(v, (dict, list)) and str(v) in {str(i) for i in ids})}
    if isinstance(value, list):
        return [scrub_references(v, ids, parent) for v in value
                if not (parent in PROFILE_KEYS and not isinstance(v, (dict, list)) and str(v) in {str(i) for i in ids})]
    return value


def owned_file(value, root):
    if not value:
        return None
    root = Path(root).resolve()
    raw = Path(value)
    path = raw.resolve()
    if not path.is_relative_to(root) or raw.is_symlink() or path == root:
        raise ValueError("Privacy file revocation refused an unsafe stored path.")
    return path


def remove_owned_file(value, root):
    path = owned_file(value, root)
    if path is None:
        return
    path.unlink(missing_ok=True)


def revoke_import_history(db, ids, identities):
    """Revoke historical files, not just the mutable last_import_id pointer.

    Imports predate provenance history. Conservatively revoke every retained
    upload/diagnostic for an affected source, since overwritten identifiers can
    no longer be proved absent from its older uploads. Stream other sources'
    retained CSVs for known normalized identifiers and explicit reference keys.
    Never load an entire upload into memory or delete outside UPLOAD_DIR.
    """
    from app.imports.mapping import suggest_mapping
    from app.imports.service import csv_reader

    affected = set(db.scalars(text("""
      SELECT source_id FROM source_records WHERE profile_id=ANY(:ids)
      UNION SELECT source_id FROM gifts WHERE profile_id=ANY(:ids)
      UNION SELECT source_id FROM events WHERE profile_id=ANY(:ids)
      UNION SELECT s.id FROM identifiers i JOIN sources s
        ON i.type='external' AND left(i.value,length(s.key)+1)=s.key || ':'
        WHERE i.profile_id=ANY(:ids)
    """), {"ids": ids}))
    emails = {value for kind, value in identities if kind == "email" and value}
    phones = {value for kind, value in identities if kind == "phone" and value}
    external = {value for kind, value in identities if kind == "external" and value}

    def contains_target(path, imported):
        headers, rows = csv_reader(path)
        mapping = imported["mapping"] or {}
        columns = mapping.get("columns") or suggest_mapping(headers, imported["record_type"])
        reference_source = (mapping.get("options") or {}).get("reference_source")
        with closing(rows):
            for row in rows:
                for header, raw in row.items():
                    value = str(raw or "").strip()
                    if not value:
                        continue
                    if emails and "@" in value and (
                        normalize_email(value) in emails or any(email in value.casefold() for email in emails)
                    ):
                        return True
                    if phones and any(char.isdigit() for char in value) and normalize_phone(value) in phones:
                        return True
                    if phones and any(normalize_phone(candidate) in phones for candidate in
                           re.findall(r"(?<!\w)\+?\d[\d ()\-.]{6,}\d(?!\w)", value)):
                        return True
                    target = columns.get(header)
                    if target == "external_id" and f"{imported['source_key']}:{value}" in external:
                        return True
                    if target == "contact_external_id" and reference_source and f"{reference_source}:{value}" in external:
                        return True
        return False

    revoke = []
    rows = db.execute(text("""SELECT i.id,i.source_id,i.record_type,i.mapping,i.file_path,
      i.error_file_path,s.key AS source_key FROM imports i JOIN sources s ON s.id=i.source_id
      WHERE NULLIF(i.file_path,'') IS NOT NULL OR NULLIF(i.error_file_path,'') IS NOT NULL
      ORDER BY i.id""")).mappings()
    for imported in rows:
        paths = [owned_file(imported[column], get_settings().upload_dir)
                 for column in ("file_path", "error_file_path")]
        contains = imported["source_id"] in affected
        if not contains:
            contains = any(path is not None and path.exists() and contains_target(path, imported)
                           for path in paths)
        if contains:
            revoke.append((imported["id"], paths))
    # Verify all paths/scans before removing anything. An unlink failure rolls
    # back metadata/erasure; files already removed remain unavailable on retry.
    for import_id, paths in revoke:
        for path in paths:
            remove_owned_file(path, get_settings().upload_dir)
        db.execute(text("UPDATE imports SET file_path='',error_file_path=NULL WHERE id=:id"),
                   {"id": import_id})
    # Raw browser/SFTP exit files precede their COPY manifests. They are also
    # uploads, not a second ungoverned archive of deleted identities.
    from app.zeta_files import records
    from app.source_rows import ZETA_ALIASES
    from app.source_connectors import SourceError
    real_keys = {(row[0], row[1]) for row in db.execute(text("""
        SELECT key_type,key_value FROM identity_xref WHERE master_id IN
          (SELECT master_id FROM profiles WHERE id=ANY(:ids))"""), {"ids": ids})}
    def raw_matches(value, key=""):
        if isinstance(value, dict):
            return any(raw_matches(v, k.lower().lstrip("$").split(".")[-1]) for k, v in value.items())
        if isinstance(value, list):
            return any(raw_matches(v, key) for v in value)
        if value is None:
            return False
        value = str(value)
        if value.startswith(("{", "[")):
            try:
                return raw_matches(json.loads(value))
            except ValueError:
                pass
        canonical = ZETA_ALIASES.get(key, key)
        normalized = value.lower() if canonical == "zeta_uid" else value.strip()
        return ((canonical, normalized) in real_keys
                or (key in {"email", "email_address"} and normalize_email(value) in emails)
                or (key in {"phone", "mobile_phone", "phone_number"} and normalize_phone(value) in phones)
                or (key in {"id", "external_id", "contact_external_id", "user_id"}
                    and any(value == ext.split(":", 1)[-1] for ext in external)))
    for upload in db.execute(text("""SELECT f.id,f.file_path,f.status,m.source_id FROM zeta_migration_files f
        JOIN zeta_migrations m ON m.id=f.migration_id WHERE NULLIF(f.file_path,'') IS NOT NULL
        ORDER BY f.id FOR UPDATE OF f""")).mappings():
        path = owned_file(upload["file_path"], get_settings().upload_dir)
        contains = upload["source_id"] in affected
        if not contains and path and path.exists():
            try:
                contains = any(has_reference(row, ids) or raw_matches(row) for row in records(path))
            except SourceError:
                # An incomplete upload cannot establish absence. Its original
                # remains with the uploader, but the server copy is revoked.
                contains = True
        if contains:
            remove_owned_file(path, get_settings().upload_dir)
            db.execute(text("""UPDATE zeta_migration_files SET file_path='',manifests='[]'::jsonb,
                status='revoked' WHERE id=:id"""), {"id": upload["id"]})
    # HubSpot stages canonical CSVs before they become imports. These files
    # must not survive erasure or permit replay from a failed checkpoint.
    for pull in db.execute(text("""SELECT id,source_id,manifests FROM hubspot_pulls
        WHERE manifests<>'[]'::jsonb ORDER BY id FOR UPDATE""")).mappings():
        revoke = pull["source_id"] in affected
        for manifest in pull["manifests"]:
            path = owned_file(manifest["path"], get_settings().upload_dir)
            if not revoke and path and path.exists():
                try:
                    _, rows = csv_reader(path)
                    with closing(rows):
                        revoke = any(raw_matches(row) for row in rows)
                except (SourceError, ValueError, OSError):
                    revoke = True  # Cannot establish absence: fail closed.
            if revoke:
                break
        if revoke:
            for manifest in pull["manifests"]:
                remove_owned_file(owned_file(manifest["path"], get_settings().upload_dir), get_settings().upload_dir)
            db.execute(text("""UPDATE hubspot_pulls SET manifests='[]'::jsonb,after_id='0',
                event_cursor=NULL,consent_after_id=0,phase='revoked',updated_at=now() WHERE id=:id"""), {"id": pull["id"]})


def expire_import_uploads(db):
    from app.hubspot_source import expire_checkpoints
    expire_checkpoints(db, get_settings().upload_retention_days)
    """Caller commits; safely expire completed imports and their diagnostics."""
    days = get_settings().upload_retention_days
    if days < 1:
        raise ValueError("UPLOAD_RETENTION_DAYS must be at least one day.")
    privacy_exclusive(db)
    imports = db.execute(text("""SELECT id,file_path,error_file_path FROM imports
      WHERE status='completed' AND finished_at < now()-(:days * interval '1 day')
        AND (NULLIF(file_path,'') IS NOT NULL OR NULLIF(error_file_path,'') IS NOT NULL)
      ORDER BY id FOR UPDATE"""), {"days": days}).mappings().all()
    # Validate every path before deleting any of this batch's files.
    paths = [(row["id"], [owned_file(row[column], get_settings().upload_dir)
                         for column in ("file_path", "error_file_path")]) for row in imports]
    for import_id, files in paths:
        for file in files:
            remove_owned_file(file, get_settings().upload_dir)
        db.execute(text("UPDATE imports SET file_path='',error_file_path=NULL WHERE id=:id"), {"id": import_id})
    for upload in db.execute(text("""SELECT f.id,f.file_path FROM zeta_migration_files f
        JOIN zeta_migrations m ON m.id=f.migration_id
        WHERE m.status='completed' AND m.updated_at < now()-(:days * interval '1 day')
          AND NULLIF(f.file_path,'') IS NOT NULL FOR UPDATE OF f"""), {"days": days}).mappings():
        remove_owned_file(upload["file_path"], get_settings().upload_dir)
        db.execute(text("""UPDATE zeta_migration_files SET file_path='',manifests='[]'::jsonb,
            status='expired' WHERE id=:id"""), {"id": upload["id"]})
    if paths:
        audit(db, None, "imports.retention", "imports", None, {"imports_revoked": len(paths)})
    return {"imports_revoked": len(paths)}


def execute_deletion(db, request_id):
    """Caller commits; failure rolls back all relational erasure, never its audit."""
    from app.identity.locking import acquire_resolver_lock, IdentityResolutionDeferred
    privacy_exclusive(db)
    if db.scalar(text("""SELECT EXISTS(SELECT 1 FROM imports WHERE status='running')
        OR EXISTS(SELECT 1 FROM zeta_migrations m JOIN jobs j ON j.id=m.job_id
            WHERE j.type='zeta.import' AND j.status IN ('queued','running'))
        OR EXISTS(SELECT 1 FROM jobs WHERE type='hubspot.finish' AND status IN ('queued','running'))
        OR EXISTS(SELECT 1 FROM jobs j JOIN sources s ON s.id::text=j.payload->>'source_id'
            WHERE j.type='source.pull' AND s.kind='hubspot' AND j.status IN ('queued','running'))""")):
        raise IdentityResolutionDeferred("Privacy execution waits for active imports to finish.")
    acquire_resolver_lock(db)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"))
    # A killed worker can leave committed Splink scratch tables outside the
    # application schema. Erasure includes those, not just FK-discovered rows.
    from app.identity.graph_jobs import purge_scratch
    purge_scratch(db)
    request = db.execute(text("SELECT * FROM deletion_requests WHERE id=:id FOR UPDATE"), {"id": request_id}).mappings().first()
    if not request:
        raise LookupError("Deletion request not found.")
    if request["status"] == "completed":
        return request["result_counts"]
    if request["status"] not in ("approved", "failed") or not request["approved_by"]:
        raise ValueError("Deletion requires approval before execution.")
    if get_settings().deletion_two_person and request["approved_by"] == request["requested_by"]:
        raise ValueError("Deletion requires two distinct admins.")
    if not db.scalar(text("SELECT EXISTS(SELECT 1 FROM users WHERE id=:id AND role='admin' AND is_active)"),
                     {"id": request["approved_by"]}):
        raise ValueError("The approving admin is no longer active.")
    inventory = check_inventory(db)
    if not request["identifier_encrypted"]:
        raise ValueError("This legacy privacy request must be recreated before safe execution.")
    try:
        from app.keyring import fernet_ring
        value = fernet_ring().decrypt(bytes(request["identifier_encrypted"])).decode()
    except (InvalidToken, UnicodeError):
        raise ValueError("Privacy request could not be verified with the configured encryption key.") from None
    from app.keyring import pii_matches, pii_hashes
    if not pii_matches(value, request["identifier_value_hash"]):
        raise ValueError("Privacy request could not be verified with the configured suppression key.")
    ids = sorted(set(request["matched_profile_ids"]) |
                 set(matching_ids(db, request["identifier_type"], value)))
    # Expand identities which were merged after request creation.
    if ids:
        ids = list(db.scalars(text("""WITH RECURSIVE closure(id) AS (
          SELECT unnest(CAST(:ids AS bigint[])) UNION SELECT p.id FROM profiles p JOIN closure c
          ON p.merged_into_id=c.id OR p.id=(SELECT merged_into_id FROM profiles WHERE id=c.id)
        ) SELECT id FROM closure"""), {"ids": ids}))
    counts = counts_for(db, ids)
    tombstones = aliases(db, ids) | {(request["identifier_type"], value)}
    from app.ingestion.privacy import erase_unresolved_and_aliases
    tombstones |= erase_unresolved_and_aliases(db, ids, request["identifier_type"], value)
    for kind, value in tombstones:
        hashed = digest(value) if value is not None else request["identifier_value_hash"]
        db.execute(text("""INSERT INTO suppressions(type,value_hash,reason) VALUES(:kind,:hash,'deletion_request')
          ON CONFLICT(type,value_hash) DO UPDATE SET reason='deletion_request'"""), {"kind": kind, "hash": hashed})
        db.execute(text("""UPDATE deletion_requests SET identifier_encrypted=NULL,
          matched_profile_ids='{}'::bigint[],
          status=CASE WHEN status IN ('pending','approved','failed') THEN 'cancelled' ELSE status END
          WHERE id<>:request AND identifier_type=:kind AND identifier_value_hash=ANY(:hashes)"""),
          {"request": request_id, "kind": kind,
           "hashes": pii_hashes(value) if value is not None else [hashed]})
    tables = {r["table_name"] for r in inventory}
    if "activation_run_members" in tables:
        exports = db.execute(text("""SELECT DISTINCT r.id,r.file_path FROM activation_runs r
          JOIN activation_run_members m ON m.run_id=r.id WHERE m.profile_id=ANY(:ids)"""), {"ids": ids}).all()
        for run_id, file_path in exports:
            from app.activations.service import purge_run_artifacts
            purge_run_artifacts(run_id)
            remove_owned_file(file_path, get_settings().export_dir)
            db.execute(text("""UPDATE activation_runs SET file_path=NULL,file_sha256=NULL,
              snapshot_encrypted=NULL,revoked_at=now(),
              error='Export revoked after an approved privacy request.' WHERE id=:id"""), {"id": run_id})
    revoke_import_history(db, ids, tombstones)
    # Delete reversible snapshots before generic metadata processing. Keeping
    # even a scrubbed journal could resurrect an erased source/event on unmerge.
    identity_purge = """
      DELETE FROM identity_merge_journal j
      WHERE winner_id=ANY(:ids) OR loser_id=ANY(:ids) OR new_profile_id=ANY(:ids)
        OR EXISTS (
          SELECT 1 FROM jsonb_array_elements(
            COALESCE(j.sides->'winner'->'source_records','[]'::jsonb) ||
            COALESCE(j.sides->'loser'->'source_records','[]'::jsonb)) r
          JOIN source_records s ON s.id=(r->>'id')::bigint
          WHERE s.profile_id=ANY(:ids)
        );
      DELETE FROM master_id_aliases WHERE retired_id IN
        (SELECT master_id FROM profiles WHERE id=ANY(:ids))
        OR survivor_id IN (SELECT master_id FROM profiles WHERE id=ANY(:ids));
      DELETE FROM identity_xref WHERE master_id IN
        (SELECT master_id FROM profiles WHERE id=ANY(:ids))
    """
    for statement in identity_purge.split(";"):
        if statement.strip():
            db.execute(text(statement), {"ids": ids})
    from app.privacy.metadata import redact_known_metadata
    counts.update(redact_known_metadata(db, tombstones))
    # Remove keyed JSON references in every table, not only today's jobs/audit payloads.
    quote = db.get_bind().dialect.identifier_preparer.quote
    for item in inventory:
        if item["data_type"] not in ("json", "jsonb"):
            continue
        table, column = item["table_name"], item["column_name"]
        rows = db.execute(text(
            f"SELECT tableoid::oid,ctid::text,{quote(column)} FROM {quote(table)} "
            f"WHERE CAST({quote(column)} AS text) ~ :keys"
        ).execution_options(stream_results=True, yield_per=500),
            {"keys": '"(' + "|".join(sorted(PROFILE_KEYS)) + ')":'}).yield_per(500)
        for table_oid, ctid, value in rows:
            if has_reference(value, ids):
                if table == "jobs":
                    db.execute(text("""UPDATE jobs SET payload='{}',progress='{}',status='cancelled',
                      error='Cancelled by an approved privacy request.',dedupe_key=NULL
                      WHERE tableoid=:oid AND ctid=CAST(:ctid AS tid)"""), {"oid": table_oid, "ctid": ctid})
                else:
                    db.execute(text(f"UPDATE {quote(table)} SET {quote(column)}=CAST(:value AS {item['data_type']}) "
                                    "WHERE tableoid=:oid AND ctid=CAST(:ctid AS tid)"),
                               {"oid": table_oid, "ctid": ctid, "value": json.dumps(scrub_references(value, ids))})
        rows.close()
    db.execute(text("DELETE FROM audit_log WHERE entity_type IN ('profile','partner') AND entity_id=ANY(:ids)"),
               {"ids": [str(i) for i in ids]})
    # Historical requests retain counts, never erased IDs.
    db.execute(text("""UPDATE deletion_requests SET matched_profile_ids=ARRAY(
      SELECT unnest(matched_profile_ids) EXCEPT SELECT unnest(CAST(:ids AS bigint[])))
      WHERE matched_profile_ids && CAST(:ids AS bigint[])"""), {"ids": ids})
    # Dependency order covers source-record referencing diagnostics and merge links.
    order = ("identity_pairs", "identity_devices", "identity_addresses",
             "activation_run_members", "activation_last_members", "segment_membership",
             "consents", "enrichment_values", "profile_traits", "trait_dirty_profiles",
             "dashboard_donor_daily", "client_errors", "events", "gifts", "identifiers", "source_records", "profile_merges")
    for table in order:
        if table in tables:
            predicates = " OR ".join(f"{quote(c)}=ANY(:ids)" for c in sorted(KNOWN_REFERENCES[table]))
            db.execute(text(f"DELETE FROM {quote(table)} WHERE {predicates}"), {"ids": ids})
    db.execute(text("UPDATE profiles SET merged_into_id=NULL WHERE merged_into_id=ANY(:ids)"), {"ids": ids})
    db.execute(text("DELETE FROM profiles WHERE id=ANY(:ids)"), {"ids": ids})
    db.execute(text("""UPDATE deletion_requests SET status='completed',executed_at=now(),
      matched_profile_ids='{}'::bigint[],result_counts=CAST(:counts AS json),error=NULL WHERE id=:id"""),
      {"counts": json.dumps(counts), "id": request_id})
    db.execute(text("UPDATE deletion_requests SET identifier_encrypted=NULL WHERE id=:id"), {"id": request_id})
    audit(db, None, "deletion.execute", "deletion_request", request_id, counts)
    from app.jobs.queue import enqueue
    enqueue(db, "traits.recompute", {"mode": "full"}, dedupe_key=f"traits:after-deletion:{request_id}")
    return counts
