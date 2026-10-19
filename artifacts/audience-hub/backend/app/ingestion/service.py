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


def identities(source, message):
    traits = message["traits"]
    values = {("external", f'{source.key}:{message["messageId"]}')}
    if message["anonymousId"]:
        values.add(("anonymous_id", anonymous_key(source.id, message["anonymousId"])))
    if message["userId"]:
        values.add(("user_id", f'{source.key}:{message["userId"]}'))
        values.add(("external", f'{source.key}:user:{message["userId"]}'))
    for kind, normalize in (("email", normalize_email), ("phone", normalize_phone)):
        if value := normalize(traits.get(kind)):
            values.add((kind, value))
    return values


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
        if message["type"] == "identify":
            traits = message["traits"]
            attrs = {"identify": True, "user_id": message["userId"],
                     "anonymous_id": anonymous_key(source.id, message["anonymousId"]) if message["anonymousId"] else None}
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
        db.add(Event(source_id=source.id, type=message["type"], name=message["name"],
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
        AND NOT EXISTS(SELECT 1 FROM identifier_blocklist b WHERE b.type=i.type AND b.value=i.value)
      GROUP BY e.id,e.occurred_at HAVING count(DISTINCT i.profile_id)=1
    ), changed AS (
      UPDATE events e SET profile_id=m.profile_id FROM matches m
      WHERE e.id=m.id AND e.occurred_at=m.occurred_at RETURNING e.profile_id
    ) INSERT INTO trait_dirty_profiles(profile_id,dirtied_at)
      SELECT profile_id,clock_timestamp() FROM (SELECT DISTINCT profile_id FROM changed) dirty
      ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at"""),
      {"source": source.id, "key": source.key})
