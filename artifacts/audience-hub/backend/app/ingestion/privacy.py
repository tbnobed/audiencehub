"""Erase pre-resolution contacts and anonymous history under privacy_exclusive.

No additional identity store: source_records/identifiers remain authoritative.
Retry receipts contain only peppered message digests and deliberately survive
erasure to keep old retries from recreating data.
"""
from sqlalchemy import text
from app.ingestion.service import anonymous_key


def erase_unresolved_and_aliases(db, ids, kind, value):
    db.execute(text("DROP TABLE IF EXISTS pg_temp.ingestion_erasure_keys, pg_temp.ingestion_erasure_events"))
    # Snapshot aliases before deleting anything. Temporary tables disappear with
    # the transaction and never become durable job payloads or audit metadata.
    db.execute(text("""CREATE TEMP TABLE ingestion_erasure_keys ON COMMIT DROP AS
      WITH RECURSIVE closure(id) AS (
        SELECT sr.id FROM source_records sr JOIN sources s ON s.id=sr.source_id
        WHERE s.kind IN ('event_api','meta_lead_ads') AND
          (sr.profile_id=ANY(:ids) OR
           (:kind='email' AND sr.email_norm=:value) OR
           (:kind='phone' AND sr.phone_e164=:value) OR EXISTS(
             SELECT 1 FROM identifiers i WHERE i.profile_id=ANY(:ids) AND
               ((i.type='user_id' AND i.value=s.key || ':' || (sr.attributes->>'user_id')) OR
                (i.type='anonymous_id' AND i.value=sr.attributes->>'anonymous_id'))))
        UNION
        SELECT other.id FROM closure c JOIN source_records known ON known.id=c.id
        JOIN source_records other ON other.source_id=known.source_id AND
          (other.attributes->>'user_id'=known.attributes->>'user_id' OR
           other.attributes->>'anonymous_id'=known.attributes->>'anonymous_id')
      )
      SELECT DISTINCT sr.source_id,s.key AS source_key,
        sr.attributes->>'anonymous_id' AS anonymous_key,
        sr.attributes->>'user_id' AS user_id
      FROM source_records sr JOIN sources s ON s.id=sr.source_id
      JOIN closure c ON c.id=sr.id"""),
      {"ids": ids, "kind": kind, "value": value})
    aliases = set(db.execute(text("""SELECT type,value FROM identifiers
      WHERE profile_id=ANY(:ids) AND type IN ('anonymous_id','user_id')"""), {"ids": ids}).all())
    aliases.update(db.execute(text("""SELECT k.kind,k.value FROM meta_verified_keys v
      JOIN source_records s ON s.id=v.source_record_id CROSS JOIN LATERAL
      (VALUES('meta_app_user_id',v.meta_app_user_id),('meta_psid',v.meta_psid)) k(kind,value)
      WHERE k.value IS NOT NULL AND (s.profile_id=ANY(:ids)
        OR (:kind='email' AND s.email_norm=:value) OR (:kind='phone' AND s.phone_e164=:value))"""),
      {"ids": ids, "kind": kind, "value": value}).all())
    # The provider lead ID is personal capture provenance. Remove it from job
    # history as well as the lead/source rows; already executing deliveries
    # cannot write after erasure because contact() rechecks suppression.
    db.execute(text("""UPDATE jobs j SET
      status=CASE WHEN j.status IN ('queued','running') THEN 'cancelled' ELSE j.status END,
      finished_at=coalesce(j.finished_at,now()),payload=j.payload-'lead_id',error=NULL,progress='{}'
      WHERE j.type='meta.lead' AND EXISTS(
        SELECT 1 FROM meta_leads l JOIN source_records s ON s.id=l.source_record_id
        WHERE l.source_id::text=j.payload->>'source_id' AND l.lead_id=j.payload->>'lead_id'
          AND (s.profile_id=ANY(:ids) OR (:kind='email' AND s.email_norm=:value)
            OR (:kind='phone' AND s.phone_e164=:value)))"""),
      {"ids": ids, "kind": kind, "value": value})
    db.execute(text("""DELETE FROM meta_attribution_sessions a
      WHERE a.profile_id=ANY(:ids) OR EXISTS(
        SELECT 1 FROM ingestion_erasure_keys k WHERE k.source_id=a.source_id AND k.anonymous_key=a.session_key)
      OR EXISTS(SELECT 1 FROM identifiers i WHERE i.profile_id=ANY(:ids)
        AND i.type='anonymous_id' AND i.value=a.session_key)"""), {"ids": ids})
    for row in db.execute(text("SELECT * FROM ingestion_erasure_keys")).mappings():
        if row["anonymous_key"]:
            aliases.add(("anonymous_id", row["anonymous_key"]))
        if row["user_id"]:
            aliases.add(("user_id", f'{row["source_key"]}:{row["user_id"]}'))
    db.execute(text("""CREATE TEMP TABLE ingestion_erasure_events ON COMMIT DROP AS
      SELECT e.id,e.occurred_at,e.source_id,s.key AS source_key,e.anonymous_id,e.user_id,e.message_id
      FROM events e JOIN sources s ON s.id=e.source_id
      WHERE e.profile_id=ANY(:ids) OR EXISTS(
        SELECT 1 FROM ingestion_erasure_keys k WHERE k.source_id=e.source_id AND
        ((k.user_id IS NOT NULL AND k.user_id=e.user_id) OR
         (k.anonymous_key IS NOT NULL AND k.anonymous_key='ah:' ||
          encode(sha256(convert_to(e.source_id::text || ':' || e.anonymous_id,'UTF8')),'hex'))))
      OR EXISTS(SELECT 1 FROM identifiers i WHERE i.profile_id=ANY(:ids) AND
        ((i.type='user_id' AND i.value=s.key || ':' || e.user_id) OR
         (i.type='anonymous_id' AND i.value='ah:' ||
          encode(sha256(convert_to(e.source_id::text || ':' || e.anonymous_id,'UTF8')),'hex'))))"""),
      {"ids": ids})
    for row in db.execute(text("SELECT * FROM ingestion_erasure_events")).mappings():
        if row["anonymous_id"]:
            aliases.add(("anonymous_id", anonymous_key(row["source_id"], row["anonymous_id"])))
        if row["user_id"]:
            aliases.add(("user_id", f'{row["source_key"]}:{row["user_id"]}'))
        if row["message_id"]:
            aliases.add(("external", f'{row["source_key"]}:{row["message_id"]}'))
    db.execute(text("""DELETE FROM events e USING ingestion_erasure_events k
      WHERE e.id=k.id AND e.occurred_at=k.occurred_at AND e.profile_id IS NULL"""))
    # Do not remove resolved records early: normal privacy teardown needs their
    # aliases, provenance, and FK ordering. Pending API contacts have no child FK.
    pending = db.execute(text("""DELETE FROM source_records sr USING sources s
      WHERE sr.source_id=s.id AND s.kind IN ('event_api','meta_lead_ads') AND sr.profile_id IS NULL
        AND sr.last_import_id IS NULL AND
        ((:kind='email' AND sr.email_norm=:value) OR (:kind='phone' AND sr.phone_e164=:value)
          OR EXISTS(SELECT 1 FROM ingestion_erasure_keys k WHERE k.source_id=sr.source_id AND
            (k.user_id=sr.attributes->>'user_id' OR k.anonymous_key=sr.attributes->>'anonymous_id')))
      RETURNING sr.email_norm,sr.phone_e164,s.key,sr.external_id"""), {"kind": kind, "value": value})
    for email, phone, source_key, external in pending:
        if email:
            aliases.add(("email", email))
        if phone:
            aliases.add(("phone", phone))
        aliases.add(("external", f"{source_key}:{external}"))
    return aliases
