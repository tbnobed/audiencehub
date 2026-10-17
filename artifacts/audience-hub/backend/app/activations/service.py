"""Bounded activation execution. Only successful runs advance delta membership."""
import csv
import hashlib
import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.config import get_settings
from app.jobs.queue import enqueue
from app.models import Activation, ActivationRun, AuditLog, Destination, Segment
from app.activations import fields, normalization, security

ADS = {"google_customer_match", "meta_custom_audience"}
CHANNELS = {"email", "sms", "phone", "mail", "ads_personalization"}
GOOGLE_MAP = dict(zip(normalization.GOOGLE_COLUMNS,
                     ("email", "phone", "first_name", "last_name", "country", "postal_code")))
META_MAP = dict(zip(normalization.META_COLUMNS,
                   ("email", "phone", "first_name", "last_name", "postal_code",
                    "city", "region", "country", "id")))


def preset(kind):
    mapping = GOOGLE_MAP if kind == "google_customer_match" else META_MAP if kind == "meta_custom_audience" else {
        "profile_id": "id", "email": "email", "first_name": "first_name", "last_name": "last_name"}
    return {target: {"kind": "profile", "key": key} for target, key in mapping.items()}


def ordered_mapping(activation):
    keys = list(activation.column_order or ()) + list(activation.field_mapping)
    return {key: activation.field_mapping[key] for key in dict.fromkeys(keys) if key in activation.field_mapping}


def validate_provider_mapping(kind, mapping):
    if kind != "meta_custom_audience":
        return
    identifier = mapping.get("external_id") if isinstance(mapping, dict) else None
    if (not isinstance(identifier, dict)
            or identifier.get("kind") != "profile" or identifier.get("key") != "id"
            or set(identifier) - {"kind", "key", "required"}):
        raise ValueError("Meta external_id is fixed to the internal profile ID (profile.id); it cannot be cleared or mapped to another field.")


def audit(db, action, entity, entity_id, user_id=None, **details):
    db.add(AuditLog(user_id=user_id, actor_type="user" if user_id else "system",
                    action=action, entity_type=entity, entity_id=str(entity_id), details=details))


def enqueue_run(db, activation_id, user_id=None, triggered_by="manual", dedupe_key=None):
    activation = db.get(Activation, activation_id)
    if activation is None or not activation.is_active:
        raise ValueError("Activation is inactive or unavailable.")
    destination = db.get(Destination, activation.destination_id)
    segment = db.get(Segment, activation.segment_id)
    if destination is None or not destination.is_active or segment is None or segment.status != "active":
        raise ValueError("Choose an active audience and destination before running.")
    # Copy and encrypt ALL settings, including mapped constants and credentials.
    # Subsequent destination edits cannot silently change an already queued run.
    snapshot = {
        "segment_id": segment.id, "segment_name": segment.name,
        "destination_id": destination.id, "type": destination.type,
        "config": destination.config, "secrets": security.decrypt(destination.secret_encrypted),
        "mapping": ordered_mapping(activation), "consent": activation.required_consent,
    }
    run = ActivationRun(activation_id=activation.id, status="queued", user_id=user_id,
                        triggered_by=triggered_by, snapshot_encrypted=security.encrypt(snapshot))
    db.add(run)
    db.flush()
    if destination.type != "webhook":
        slug = re.sub(r"[^a-z0-9]+", "-", segment.name.lower()).strip("-")[:80] or "partner-audience"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Persist the filename BEFORE writing any bytes. Together with the
        # write-ahead member index, even a killed worker leaves erasure-addressable
        # partial files. Only succeeded runs can download the file.
        run.file_path = str(root_directory() / str(run.id) / f"{slug}-{stamp}.csv")
    # Network retries belong to the bounded sender; no second job-level replay
    # of a partially delivered run. Explicit re-runs receive new run IDs.
    job = enqueue(db, "activation.run", {"run_id": run.id}, max_attempts=1,
                  dedupe_key=dedupe_key or f"activation.run:{run.id}")
    return run, job


def trait_barrier(db):
    from app.identity.locking import IdentityResolutionDeferred
    from app.jobs.handlers import SegmentTraitsRefreshRequired
    if not db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))")):
        raise IdentityResolutionDeferred("Activation waits for partner traits.")
    if db.scalar(text("SELECT EXISTS(SELECT 1 FROM jobs WHERE type='traits.recompute' AND status IN ('queued','running'))")):
        raise IdentityResolutionDeferred("Activation waits for partner traits.")
    latest = db.scalar(text("SELECT status FROM jobs WHERE type='traits.recompute' AND payload->>'mode'='full' ORDER BY id DESC LIMIT 1"))
    if latest in ("failed", "cancelled"):
        raise SegmentTraitsRefreshRequired("Repair the failed full traits refresh before running activations.")


def root_directory():
    root = Path(get_settings().export_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root.resolve()


def run_directory(run_id):
    root = root_directory()
    path = root / str(int(run_id))
    if path.is_symlink():
        raise security.DeliveryError("Export directory failed its safety check.")
    path.mkdir(exist_ok=True, mode=0o700)
    if path.resolve().parent != root:
        raise security.DeliveryError("Export directory failed its safety check.")
    return path


def safe_file(run):
    if run.revoked_at or not run.file_path or run.status != "succeeded":
        raise ValueError("Export is unavailable or has been revoked.")
    if not run.finished_at or run.finished_at < datetime.now(timezone.utc) - timedelta(days=get_settings().export_retention_days):
        raise ValueError("Export has expired.")
    root = root_directory()
    path = Path(run.file_path)
    if path.is_symlink() or path.parent.is_symlink() or path.parent != root / str(run.id):
        raise ValueError("Export path failed its safety check.")
    if path.resolve().parent != root / str(run.id) or not path.is_file():
        raise ValueError("Export is unavailable.")
    return path


def suppressed_ids(db, rows, channel):
    from app.identity.normalize import normalize_email, normalize_phone
    pepper = get_settings().pii_hash_pepper.encode()
    lookup = {}
    for row in rows:
        for kind, value in (("email", normalize_email(row.get("email"))), ("phone", normalize_phone(row.get("phone")))):
            if value:
                digest = hmac.new(pepper, value.encode(), hashlib.sha256).hexdigest()
                lookup.setdefault((kind, digest), set()).add(row["id"])
    if not lookup:
        return set()
    result = set()
    for suppression in db.execute(text("SELECT type,value_hash,reason FROM suppressions WHERE value_hash=ANY(:hashes)"),
                                  {"hashes": list({key[1] for key in lookup})}).mappings():
        # Non-deletion email suppressions do not suppress unrelated channels.
        if suppression["reason"] == "deletion_request" or (
                channel == "email" and suppression["type"] == "email"):
            result.update(lookup.get((suppression["type"], suppression["value_hash"]), ()))
    return result


def _rows(db, ids=None):
    # Keyset batches avoid accumulating an audience or keeping a server cursor
    # open while issuing per-batch enrichment/suppression queries.
    after = 0
    while True:
        batch = list(db.execute(text("""SELECT p.*,to_jsonb(t) AS traits FROM active_profiles p
          JOIN activation_work w ON w.profile_id=p.id
          LEFT JOIN profile_traits t ON t.profile_id=p.id
          WHERE p.id>:after ORDER BY p.id LIMIT 500"""), {"after": after}).mappings())
        if not batch:
            return
        yield batch
        after = batch[-1]["id"]


def track_members_durably(db, run, *, previous=False):
    """Write-ahead erasure index survives a worker/database-transaction crash.

    No PII leaves the process before all affected IDs are committed here. The
    caller holds the shared privacy lock and a NO KEY UPDATE run lock, so FK
    insertion is compatible and deletion cannot interleave. Run creation must
    already have committed through the queue boundary.
    """
    with Session(db.get_bind()) as tracking:
        if tracking.get(ActivationRun, run.id) is None:
            raise ValueError("Queue the activation run before executing it.")
        after = 0
        while True:
            if previous:
                ids = db.scalars(text("""SELECT profile_id FROM activation_last_members
                  WHERE activation_id=:activation AND profile_id>:after ORDER BY profile_id LIMIT 1000"""),
                  {"activation": run.activation_id, "after": after}).all()
            else:
                ids = db.scalars(text("""SELECT profile_id FROM activation_work
                  WHERE profile_id>:after ORDER BY profile_id LIMIT 1000"""), {"after": after}).all()
            if not ids:
                break
            tracking.execute(text("""INSERT INTO activation_run_members(run_id,profile_id)
              SELECT :run,unnest(CAST(:ids AS bigint[])) ON CONFLICT DO NOTHING"""),
              {"run": run.id, "ids": ids})
            after = ids[-1]
        tracking.commit()


def prepare(db, run, snapshot):
    validate_provider_mapping(snapshot["type"], snapshot["mapping"])
    from app.segments.service import materialize_for_activation
    trait_barrier(db)
    materialize_for_activation(db, snapshot["segment_id"])
    fields.validate_mapping(db, snapshot["mapping"], snapshot["config"].get("include_pii", True))
    db.execute(text("CREATE TEMP TABLE activation_work(profile_id bigint PRIMARY KEY) ON COMMIT DROP"))
    db.execute(text("""INSERT INTO activation_work SELECT m.profile_id FROM segment_membership m
      JOIN active_profiles p ON p.id=m.profile_id WHERE m.segment_id=:id"""), {"id": snapshot["segment_id"]})
    run.profiles_selected = db.scalar(text("SELECT count(*) FROM activation_work"))
    consent = snapshot["consent"]
    run.profiles_excluded_consent = 0
    if consent:
        run.profiles_excluded_consent = db.execute(text("""DELETE FROM activation_work w
          WHERE NOT EXISTS(SELECT 1 FROM consents c WHERE c.profile_id=w.profile_id
            AND c.channel=:channel AND c.status='opted_in')"""), {"channel": consent}).rowcount
    run.profiles_excluded_suppressed = run.profiles_excluded_missing_fields = run.profiles_sent = 0
    kind, mapping = snapshot["type"], snapshot["mapping"]
    for batch in _rows(db):
        suppressed = suppressed_ids(db, batch, consent)
        enrichment = fields.enrichments(db, [row["id"] for row in batch])
        missing = set()
        for row in batch:
            if row["id"] in suppressed:
                continue
            values, required_missing = fields.mapped(row, mapping, enrichment)
            if kind in ADS:
                names = GOOGLE_MAP if kind == "google_customer_match" else META_MAP
                mapped_partner = {names[target]: value for target, value in values.items()}
                required_missing |= not (normalization.email(mapped_partner.get("email"))
                                         or normalization.phone(mapped_partner.get("phone")))
            if required_missing:
                missing.add(row["id"])
        run.profiles_excluded_suppressed += len(suppressed)
        run.profiles_excluded_missing_fields += len(missing)
        if suppressed or missing:
            db.execute(text("DELETE FROM activation_work WHERE profile_id=ANY(:ids)"),
                       {"ids": sorted(suppressed | missing)})
    db.flush()
    track_members_durably(db, run)


def export_csv(db, run, snapshot):
    kind, mapping = snapshot["type"], snapshot["mapping"]
    columns = normalization.GOOGLE_COLUMNS if kind == "google_customer_match" else normalization.META_COLUMNS if kind == "meta_custom_audience" else list(mapping)
    directory = run_directory(run.id)
    final = Path(run.file_path)
    if final.parent != directory or final.is_symlink():
        raise security.DeliveryError("Export filename failed its safety check.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    created = completed = False
    try:
        descriptor = os.open(final, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, delimiter=snapshot["config"].get("delimiter", ","))
            writer.writerow([normalization.csv_safe(c) for c in columns])
            for batch in _rows(db):
                enrichment = fields.enrichments(db, [r["id"] for r in batch])
                for row in batch:
                    values, _ = fields.mapped(row, mapping, enrichment)
                    if kind in ADS:
                        names = GOOGLE_MAP if kind == "google_customer_match" else META_MAP
                        partner = {names[target]: value for target, value in values.items()}
                        result = normalization.google(partner) if kind == "google_customer_match" else normalization.meta(partner)
                    else:
                        result = [values[c] for c in columns]
                    writer.writerow([normalization.csv_safe(v) for v in result])
                    run.profiles_sent += 1
            stream.flush()
            os.fsync(stream.fileno())
        hasher = hashlib.sha256()
        with final.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                hasher.update(block)
        run.file_path, run.file_sha256 = str(final), hasher.hexdigest()
        completed = True
    finally:
        if created and not completed:
            final.unlink(missing_ok=True)


def send_webhook(db, run, snapshot):
    config, secrets = snapshot["config"], snapshot["secrets"]
    delta = config.get("mode", "full") == "delta"
    # Track removed IDs as well so deletion invalidates every affected run.
    if delta:
        track_members_durably(db, run, previous=True)
    db.execute(text("""CREATE TEMP TABLE activation_send(profile_id bigint PRIMARY KEY,removed boolean NOT NULL)
       ON COMMIT DROP"""))
    db.execute(text("""INSERT INTO activation_send SELECT w.profile_id,false FROM activation_work w
      WHERE NOT :delta OR NOT EXISTS(SELECT 1 FROM activation_last_members m
        WHERE m.activation_id=:activation AND m.profile_id=w.profile_id)"""),
               {"delta": delta, "activation": run.activation_id})
    if delta:
        db.execute(text("""INSERT INTO activation_send
          SELECT m.profile_id,true FROM activation_last_members m WHERE m.activation_id=:activation
          AND NOT EXISTS(SELECT 1 FROM activation_work w WHERE w.profile_id=m.profile_id)"""),
                   {"activation": run.activation_id})
    size = config.get("batch_size", 500)
    total = db.scalar(text("SELECT count(*) FROM activation_send"))
    batches = max(1, (total + size - 1) // size)
    after = 0
    for number in range(1, batches + 1):
        selected = list(db.execute(text("""SELECT s.profile_id,s.removed,p.*,to_jsonb(t) AS traits
          FROM activation_send s LEFT JOIN active_profiles p ON p.id=s.profile_id
          LEFT JOIN profile_traits t ON t.profile_id=p.id WHERE s.profile_id>:after
          ORDER BY s.profile_id LIMIT :size"""), {"after": after, "size": size}).mappings())
        enrichment = fields.enrichments(db, [r["id"] for r in selected if r["id"] is not None])
        added, removed = [], []
        for row in selected:
            if row["removed"]:
                removed.append({"profile_id": row["profile_id"]})
            else:
                values, _ = fields.mapped(row, snapshot["mapping"], enrichment)
                added.append(dict(values, profile_id=row["id"]))
        payload = {"activation_id": run.activation_id, "run_id": run.id,
                   "segment": {"id": snapshot["segment_id"], "name": snapshot["segment_name"]},
                   "mode": config.get("mode", "full"), "batch": number, "batches": batches,
                   "added": added, "removed": removed}
        security.deliver(config["url"], payload, secrets["signing_secret"],
                         secrets.get("headers", {}), run.id, number,
                         config.get("allow_internal_network", False))
        run.profiles_sent += len(added)
        if selected:
            after = selected[-1]["profile_id"]
    # Only after every batch succeeded may the last-successful baseline move.
    db.execute(text("DELETE FROM activation_last_members WHERE activation_id=:id"), {"id": run.activation_id})
    db.execute(text("""INSERT INTO activation_last_members(activation_id,profile_id)
      SELECT :id,profile_id FROM activation_work"""), {"id": run.activation_id})


def execute(db, run_id):
    """Caller supplies a transaction; handled delivery failures commit truthful state."""
    from app.privacy.locking import privacy_shared
    from app.identity.locking import acquire_resolver_lock
    privacy_shared(db)
    # Keep survivor values and membership stable while exporting. Nonblocking
    # acquisition yields to an active import/resolver instead of deadlocking.
    acquire_resolver_lock(db)
    run = db.scalar(select(ActivationRun).where(ActivationRun.id == run_id).with_for_update(key_share=True))
    if run is None:
        raise ValueError("Activation run not found.")
    if run.revoked_at:
        if run.status in ("queued", "running"):
            run.status, run.finished_at = "cancelled", datetime.now(timezone.utc)
        return run
    if run.status in ("succeeded", "failed", "cancelled"):
        return run
    snapshot = security.decrypt(run.snapshot_encrypted)
    if not snapshot:
        raise security.DeliveryError("Activation snapshot is unavailable.")
    activation = db.get(Activation, run.activation_id)
    destination = db.get(Destination, snapshot["destination_id"])
    if not activation or not activation.is_active or not destination or not destination.is_active:
        run.status, run.finished_at = "cancelled", datetime.now(timezone.utc)
        run.error = "Activation or destination was deactivated before delivery."
        audit(db, "activation.run", "activation", run.activation_id, run.user_id,
              run_id=run.id, status="cancelled")
        return run
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"activation:{run.activation_id}"})
    run.status, run.started_at = "running", run.started_at or datetime.now(timezone.utc)
    prepare(db, run, snapshot)
    try:
        if snapshot["type"] == "webhook":
            send_webhook(db, run, snapshot)
        else:
            export_csv(db, run, snapshot)
        run.status = "succeeded"
    except security.DeliveryError as error:
        run.status = "failed"
        run.error = str(error) + " Some batches may have reached the destination; review its receipt before re-running."
    except OSError:
        run.status = "failed"
        run.error = "Export could not be written safely. Check storage capacity and permissions before re-running."
    run.finished_at = datetime.now(timezone.utc)
    audit(db, "activation.run", "activation", run.activation_id, run.user_id,
          run_id=run.id, status=run.status, selected=run.profiles_selected,
          excluded_consent=run.profiles_excluded_consent,
          excluded_suppressed=run.profiles_excluded_suppressed,
          excluded_missing_fields=run.profiles_excluded_missing_fields, sent=run.profiles_sent)
    db.flush()
    return run


def run_activation(run_id, job_id=None):
    from app.db import engine
    from app.identity.locking import IdentityResolutionDeferred
    try:
        with Session(engine) as db:
            run = db.get(ActivationRun, run_id)
            if run and run.status == "queued":
                run.status, run.started_at = "running", datetime.now(timezone.utc)
                db.commit()
        with Session(engine) as db:
            run = execute(db, run_id)
            db.commit()
            if run.status == "failed":
                raise security.DeliveryError("Activation delivery failed; inspect its run history.")
    except IdentityResolutionDeferred:
        with Session(engine) as db:
            run = db.get(ActivationRun, run_id)
            if run and run.status == "running":
                run.status = "queued"
                db.commit()
        raise
    except Exception:
        with Session(engine) as db:
            run = db.get(ActivationRun, run_id)
            if run and run.status not in ("succeeded", "failed", "cancelled"):
                run.status, run.finished_at = "failed", datetime.now(timezone.utc)
                run.error = "Activation failed safely; check audience, traits, mapping and destination settings."
                audit(db, "activation.run", "activation", run.activation_id, run.user_id,
                      run_id=run.id, status="failed")
                db.commit()
        raise


def expire_exports(db):
    """Only files referenced by expired run rows; never traverse arbitrary paths."""
    from app.privacy.locking import privacy_exclusive
    privacy_exclusive(db)
    deadline = datetime.now(timezone.utc) - timedelta(days=get_settings().export_retention_days)
    root = root_directory()
    for run in db.scalars(select(ActivationRun).where(ActivationRun.finished_at < deadline,
                                                    ActivationRun.file_path.is_not(None))):
        path = Path(run.file_path)
        if path.parent != root / str(run.id) or path.parent.is_symlink() or path.is_symlink():
            raise ValueError("An expired export failed its safety check.")
        path.unlink(missing_ok=True)
        run.file_path = run.file_sha256 = None
        run.revoked_at = datetime.now(timezone.utc)
        run.snapshot_encrypted = None
