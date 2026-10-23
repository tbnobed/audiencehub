import hashlib
import json
from sqlalchemy import text
from app.identity.locking import acquire_import_lock
from app.identity.normalize import normalize_email, normalize_phone
from app.jobs.queue import enqueue
from app.models import Event
from app.privacy.service import digest


def anonymous_key(source_id, value):
    return "ah:" + hashlib.sha256(f"{source_id}:{value}".encode()).hexdigest()


def message_contact(message):
    traits = dict(message["traits"])
    # Provider IDs are interchangeable login references, not device IDs.
    for key in ("okta_customer_id", "fusionauth_user_id"):
        if not traits.get(key) and isinstance(message["properties"].get(key), str):
            traits[key] = message["properties"][key]
    if (message["type"] == "track" and message["name"].strip().lower().replace(" ", "_")
            in {"donation", "donation_success", "signup", "sign_up", "login", "account_created"}):
        for key in ("email", "phone"):
            if not traits.get(key) and isinstance(message["properties"].get(key), str):
                traits[key] = message["properties"][key]
    return traits


def identities(source, message):
    traits = message_contact(message)
    values = {("external", f'{source.key}:{message["messageId"]}')}
    if message["anonymousId"]:
        values.add(("anonymous_id", anonymous_key(source.id, message["anonymousId"])))
    if message["userId"]:
        values.add(("user_id", f'{source.key}:{message["userId"]}'))
        values.add(("external", f'{source.key}:user:{message["userId"]}'))
    for kind, normalize in (("email", normalize_email), ("phone", normalize_phone)):
        if value := normalize(traits.get(kind)):
            values.add((kind, value))
    for kind in ("okta_customer_id", "fusionauth_user_id"):
        if isinstance(traits.get(kind), str) and traits[kind].strip():
            values.add((kind, traits[kind].strip()))
    return values


def login_profile(db, traits):
    """Attach viewing/login facts to an EXISTING person with either provider.

    Both keys, when supplied, must match the same unique current person.
    Public event traffic never creates a person or authenticated device link.
    """
    owner = None
    for kind in ("okta_customer_id", "fusionauth_user_id"):
        value = traits.get(kind)
        if not isinstance(value, str) or not value.strip():
            continue
        rows = db.scalars(text("""
          SELECT DISTINCT p.id FROM identity_xref x
          JOIN active_profiles p ON p.master_id=x.master_id
          WHERE x.key_type=:kind AND x.key_value=:value
            AND NOT EXISTS(SELECT 1 FROM identifier_blocklist b
              WHERE b.type=x.key_type AND b.value=x.key_value)
        """), {"kind": kind, "value": value.strip()}).all()
        if len(rows) != 1 or (owner is not None and owner != rows[0]):
            return None
        owner = rows[0]
    return owner


def suppressed(db, values):
    from app.keyring import pii_hashes
    pairs = sorted((kind, hashed) for kind, value in values for hashed in pii_hashes(value))
    return db.scalar(text("""SELECT EXISTS(
      SELECT 1 FROM suppressions s JOIN
      unnest(CAST(:types AS text[]),CAST(:hashes AS text[])) v(kind,hash)
      ON s.type=v.kind AND s.value_hash=v.hash)"""),
      {"types": [k for k, v in pairs], "hashes": [v for k, v in pairs]})


def ingest(db, source, messages):
    """Caller transaction includes receipts, source records, events and wakeup."""
    acquire_import_lock(db)
    accepted, rejected, changed = 0, [], False
    for index, message in messages:
        if suppressed(db, identities(source, message)):
            rejected.append({"index": index, "reason": "Identity is suppressed"})
            continue
        receipt = db.scalar(text("""INSERT INTO ingestion_receipts(source_id,message_hash)
          VALUES(:source,:hash) ON CONFLICT DO NOTHING RETURNING source_id"""),
          {"source": source.id, "hash": digest("ingestion-message:" + message["messageId"])})
        # Accepted includes valid duplicate deliveries, but never writes again.
        accepted += 1
        if receipt is None:
            continue
        changed = True
        from app.meta_attribution import capture
        capture(db, source.id, message)
        # Giving/signup/login can identify with an explicitly provided contact,
        # using the SAME normal resolver path, never a click ID.
        traits = message_contact(message)
        identifying_conversion = (
            message["type"] == "track" and message["name"].strip().lower().replace(" ", "_")
            in {"donation", "donation_success", "signup", "sign_up", "login", "account_created"}
            and (normalize_email(traits.get("email")) or normalize_phone(traits.get("phone"))))
        if message["type"] == "identify" or identifying_conversion:
            attrs = {"identify": True, "user_id": message["userId"],
                     "anonymous_id": anonymous_key(source.id, message["anonymousId"]) if message["anonymousId"] else None}
            attrs.update({key: traits[key].strip() for key in ("okta_customer_id", "fusionauth_user_id")
                          if isinstance(traits.get(key), str) and traits[key].strip()})
            # Each identify delivery is a contact observation. This preserves
            # prior aliases and permits multiple devices without a mapping table.
            external = "identify:" + digest(message["messageId"])
            db.execute(text("""INSERT INTO source_records(source_id,external_id,email_norm,phone_e164,
              first_name,last_name,attributes,raw_hash) VALUES(:source,:external,:email,:phone,
              :first,:last,CAST(:attrs AS jsonb),:hash)"""),
              {"source": source.id, "external": external,
               "email": normalize_email(traits.get("email")), "phone": normalize_phone(traits.get("phone")),
               "first": traits.get("firstName"), "last": traits.get("lastName"),
               "attrs": json.dumps(attrs), "hash": digest("ingestion-message:" + message["messageId"])})
        profile_id = login_profile(db, traits)
        if profile_id is not None:
            db.execute(text("""INSERT INTO trait_dirty_profiles(profile_id,dirtied_at)
              VALUES(:id,clock_timestamp()) ON CONFLICT(profile_id)
              DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at"""), {"id": profile_id})
        db.add(Event(source_id=source.id, profile_id=profile_id, type=message["type"], name=message["name"],
                     anonymous_id=message["anonymousId"], user_id=message["userId"],
                     message_id=message["messageId"], occurred_at=message["occurred_at"],
                     received_at=message["received_at"], properties=message["properties"],
                     context=message["context"]))
    if changed:
        # No raw identifiers, source record IDs, or partner IDs in durable jobs.
        # No active-job dedupe: a concurrent running job must not swallow a wakeup.
        enqueue(db, "ingestion.resolve", {"source_id": source.id})
    db.flush()
    return {"accepted": accepted, "rejected": rejected}


def backfill(db, source_id):
    """Use resolver-owned identifiers; never bypass blocklisting or merge rules."""
    from app.models import Source
    source = db.get(Source, source_id)
    if source is None:
        return
    # Anonymous IDs use the same source-scoped SHA256 representation as identify.
    db.execute(text("""WITH matches AS (
      SELECT e.id,e.occurred_at, min(i.profile_id) AS profile_id
      FROM events e JOIN identifiers i ON
        (i.type='user_id' AND i.value=:key || ':' || e.user_id) OR
        (i.type='anonymous_id' AND i.value='ah:' ||
          encode(sha256(convert_to(CAST(:source AS text) || ':' || e.anonymous_id,'UTF8')),'hex'))
      JOIN active_profiles p ON p.id=i.profile_id
      WHERE e.source_id=:source AND e.profile_id IS NULL
        AND NULLIF(btrim(e.properties->>'okta_customer_id'),'') IS NULL
        AND NULLIF(btrim(e.properties->>'fusionauth_user_id'),'') IS NULL
        AND NOT EXISTS(SELECT 1 FROM identifier_blocklist b WHERE b.type=i.type AND b.value=i.value)
      GROUP BY e.id,e.occurred_at HAVING count(DISTINCT i.profile_id)=1
    ), changed AS (
      UPDATE events e SET profile_id=m.profile_id FROM matches m
      WHERE e.id=m.id AND e.occurred_at=m.occurred_at RETURNING e.profile_id
    ) INSERT INTO trait_dirty_profiles(profile_id,dirtied_at)
      SELECT profile_id,clock_timestamp() FROM (SELECT DISTINCT profile_id FROM changed) dirty
      ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at"""),
      {"source": source.id, "key": source.key})
    # Existing-person-only attachment for stored viewing/conversion events.
    # Never fall back to one provider when the other supplied key disagrees.
    db.execute(text("""
      WITH keys AS MATERIALIZED (
        SELECT e.id,e.occurred_at,k.kind,k.value,
          count(*) OVER(PARTITION BY e.id,e.occurred_at) AS required
        FROM events e CROSS JOIN LATERAL (VALUES
          ('okta_customer_id',NULLIF(btrim(e.properties->>'okta_customer_id'),'')),
          ('fusionauth_user_id',NULLIF(btrim(e.properties->>'fusionauth_user_id'),''))
        ) k(kind,value)
        WHERE e.source_id=:source AND e.profile_id IS NULL AND k.value IS NOT NULL
      ), matches AS (
        SELECT k.id,k.occurred_at,min(p.id) AS profile_id FROM keys k
        JOIN identity_xref x ON x.key_type=k.kind AND x.key_value=k.value
        JOIN active_profiles p ON p.master_id=x.master_id
        WHERE NOT EXISTS(SELECT 1 FROM identifier_blocklist b
          WHERE b.type=k.kind AND b.value=k.value)
        GROUP BY k.id,k.occurred_at
        HAVING count(DISTINCT p.id)=1 AND count(DISTINCT k.kind)=max(k.required)
      ), changed AS (
        UPDATE events e SET profile_id=m.profile_id FROM matches m
        WHERE e.id=m.id AND e.occurred_at=m.occurred_at RETURNING e.profile_id
      )
      INSERT INTO trait_dirty_profiles(profile_id,dirtied_at)
        SELECT DISTINCT profile_id,now() FROM changed
        ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at
    """), {"source": source_id})
    from app.meta_attribution import attach
    attach(db, source_id)
