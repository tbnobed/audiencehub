"""Lead/Messenger observations use normal import suppression and identity rules."""
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.activations import security
from app.db import engine
from app.identity.locking import acquire_import_lock
from app.identity.normalize import normalize_email, normalize_phone
from app.ingestion.service import suppressed
from app.jobs.queue import enqueue
from app.meta_client import Meta, MetaError
from app.models import Source
from app.privacy.service import digest

CANONICAL = {"email", "phone", "first_name", "last_name", "address1", "address2",
             "city", "region", "postal_code", "country"}
DEFAULT_CONFIG = {
    "page_id": "", "app_id": "", "graph_version": "v25.0", "backfill_days": 30,
    "field_mapping": {"email": "email", "phone_number": "phone", "first_name": "first_name",
                      "last_name": "last_name", "city": "city", "state": "region", "zip_code": "postal_code"},
    "consent_form_id": "", "consent_field": "", "consent_text": "", "consent_channel": "email",
    "consent_true_values": ["true", "yes", "1"], "ad_account_id": "",
}


def connection(db, source_id):
    row = db.execute(text("""SELECT m.*,s.is_active,s.name AS source_name FROM meta_connections m
      JOIN sources s ON s.id=m.source_id WHERE m.source_id=:id AND s.kind='meta_lead_ads'"""),
      {"id": source_id}).mappings().first()
    if not row or not row["is_active"]:
        raise MetaError("Configure an active Meta Lead Ads source first.")
    return dict(row), security.decrypt(row["credentials"])


def config_valid(config):
    import re
    if set(config) - set(DEFAULT_CONFIG):
        raise MetaError("Unsupported Meta configuration field.")
    result = {**DEFAULT_CONFIG, **config}
    for key in ("page_id", "app_id"):
        if not re.fullmatch(r"\d{1,40}", str(result[key])):
            raise MetaError("Meta Page and app IDs must be numeric.")
        result[key] = str(result[key])
    if not re.fullmatch(r"v\d{2}\.0", str(result["graph_version"])):
        raise MetaError("Pin a supported Meta Graph version, for example v25.0.")
    if not isinstance(result["ad_account_id"], str) or (
            result["ad_account_id"] and not re.fullmatch(r"(act_)?\d{1,40}", result["ad_account_id"])):
        raise MetaError("Invalid Meta ad account ID.")
    if type(result["backfill_days"]) is not int or not 1 <= result["backfill_days"] <= 90:
        raise MetaError("Backfill must cover between 1 and 90 days.")
    mapping = result["field_mapping"]
    if (not isinstance(mapping, dict) or not 1 <= len(mapping) <= 50
            or any(not isinstance(k, str) or not 1 <= len(k) <= 200
                   or not isinstance(v, str) or v not in CANONICAL for k, v in mapping.items())
            or len(set(mapping.values())) != len(mapping)):
        raise MetaError("Map unique form fields only to approved canonical contact fields.")
    if result["consent_channel"] not in {"email", "sms", "ads_personalization", "phone", "mail"}:
        raise MetaError("Unsupported consent channel.")
    for key, size in (("consent_field", 200), ("consent_text", 10000)):
        if not isinstance(result[key], str) or len(result[key]) > size:
            raise MetaError("Invalid consent field/text.")
    if bool(result["consent_field"]) != bool(result["consent_text"].strip()):
        raise MetaError("Provide both the checkbox field and its exact consent text, or neither.")
    if result["consent_field"] and not re.fullmatch(r"\d{1,40}", str(result["consent_form_id"])):
        raise MetaError("Bind consent text to its exact Meta form ID; it must not be reused across unrelated forms.")
    result["consent_form_id"] = str(result["consent_form_id"])
    if (not isinstance(result["consent_true_values"], list) or not 1 <= len(result["consent_true_values"]) <= 20
            or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in result["consent_true_values"])):
        raise MetaError("Define explicit affirmative consent values.")
    return result


def contact(db, source_id, external, values, attributes, *, app_user_id=None, psid=None):
    acquire_import_lock(db)
    source = db.query(Source).filter(Source.id == source_id, Source.is_active.is_(True)).with_for_update(read=True).one_or_none()
    if source is None:
        raise MetaError("Meta source is disabled.")
    email, phone = normalize_email(values.get("email")), normalize_phone(values.get("phone"))
    keys = {("external", f"{source.key}:{external}")}
    if email:
        keys.add(("email", email))
    if phone:
        keys.add(("phone", phone))
    if app_user_id:
        keys.add(("meta_app_user_id", app_user_id))
    if psid:
        if not (email or phone):
            raise MetaError("A Page-scoped identity requires a person-provided email or phone.")
        keys.add(("meta_psid", psid))
    if suppressed(db, keys):
        return None
    # The source-record external ID provides delivery idempotency, not a new
    # crosswalk key. Only the private verified-keys relation supplies Meta keys.
    rid = db.scalar(text("""INSERT INTO source_records(source_id,external_id,email_norm,phone_e164,
      first_name,last_name,address1,address2,city,region,postal_code,country,attributes,raw_hash)
      VALUES(:source,:external,:email,:phone,:first_name,:last_name,:address1,:address2,:city,
        :region,:postal_code,:country,CAST(:attrs AS jsonb),:hash)
      ON CONFLICT(source_id,external_id) DO UPDATE SET
      email_norm=coalesce(EXCLUDED.email_norm,source_records.email_norm),
      phone_e164=coalesce(EXCLUDED.phone_e164,source_records.phone_e164),
      first_name=coalesce(EXCLUDED.first_name,source_records.first_name),
      last_name=coalesce(EXCLUDED.last_name,source_records.last_name),
      attributes=source_records.attributes || EXCLUDED.attributes,
      updated_at=now(),resolved_at=NULL RETURNING id"""),
      {"source": source_id, "external": external, "email": email, "phone": phone,
       **{k: values.get(k) for k in CANONICAL - {"email", "phone"}},
       "attrs": json.dumps(attributes), "hash": digest(f"meta:{source_id}:{external}")})
    if app_user_id or psid:
        db.execute(text("""INSERT INTO meta_verified_keys(source_record_id,meta_app_user_id,meta_psid)
          VALUES(:id,:app,:psid) ON CONFLICT(source_record_id) DO NOTHING"""),
          {"id": rid, "app": app_user_id, "psid": psid})
    db.execute(text("""INSERT INTO meta_pending_contacts(source_record_id) VALUES(:id)
      ON CONFLICT DO NOTHING"""), {"id": rid})
    enqueue(db, "ingestion.resolve", {"source_id": source_id})
    return rid


def lead_fields(lead):
    fields = {}
    for item in lead.get("field_data", []):
        if isinstance(item.get("name"), str) and isinstance(item.get("values"), list):
            fields[item["name"]] = str(item["values"][0]) if item["values"] else ""
    for item in lead.get("custom_disclaimer_responses", []):
        key = item.get("checkbox_key")
        if key:
            fields[str(key)] = str(item.get("is_checked", item.get("value", "")))
    return fields


def process_lead(source_id, lead_id, job_id=None):
    with Session(engine) as db:
        row, credentials = connection(db, source_id)
        if db.scalar(text("SELECT status FROM meta_leads WHERE source_id=:s AND lead_id=:l"),
                     {"s": source_id, "l": lead_id}) in {"completed", "suppressed"}:
            return
        config = row["config"]
        db.commit()
        with Meta(config, credentials) as client:
            lead = client.request("GET", "/" + str(lead_id), {
                "fields": "id,created_time,form_id,field_data,custom_disclaimer_responses"})
            form_id = str(lead.get("form_id", ""))
            # Confirm the form belongs to the connected Page. A signed webhook
            # or caller-supplied lead ID must not import a different Page's leads.
            forms = list(client.pages(f'/{config["page_id"]}/leadgen_forms', {"fields": "id,name,questions", "limit": 100}))
            form = next((f for f in forms if str(f["id"]) == form_id), None)
            if form is None:
                raise MetaError("Lead form does not belong to the connected Page.")
        fields = lead_fields(lead)
        values = {canonical: fields.get(field) for field, canonical in config["field_mapping"].items()}
        captured = datetime.fromisoformat(lead["created_time"].replace("Z", "+00:00"))
        if captured.tzinfo is None:
            raise MetaError("Meta lead timestamp lacks a timezone.")
        consent = None
        if config.get("consent_field") in fields and form_id == config.get("consent_form_id"):
            value = fields[config["consent_field"]]
            affirmative = value.strip().lower() in {v.strip().lower() for v in config["consent_true_values"]}
            question = next((q for q in form.get("questions", []) if q.get("key") == config["consent_field"]), {})
            actual_text = question.get("label") or config["consent_text"]
            text_changed = actual_text.strip() != config["consent_text"].strip()
            consent = {"channel": config["consent_channel"], "status": "opted_in" if affirmative else "unknown",
                       "captured_at": captured.isoformat(), "checkbox_text": actual_text,
                       "checkbox_field": config["consent_field"], "checkbox_value": value,
                       "text_changed": text_changed,
                       "text_origin": "graph_form_question" if question.get("label") else "configured_exact_form"}
            if text_changed:
                consent["status"] = "unknown"
        attrs = {"meta_lead_id": str(lead_id), "meta_form_id": form_id, "meta_form_name": form["name"],
                 "capture_source": "meta_lead_ads", "source_name": row["source_name"],
                 "meta_consent": consent, "captured_at": captured.isoformat()}
        rid = contact(db, source_id, "meta-lead:" + str(lead_id), values, attrs)
        db.execute(text("""INSERT INTO meta_leads(source_id,lead_id,form_id,source_record_id,status)
          VALUES(:s,:l,:f,:r,:status) ON CONFLICT(source_id,lead_id) DO UPDATE SET
          form_id=EXCLUDED.form_id,source_record_id=EXCLUDED.source_record_id,
          status=EXCLUDED.status,error=NULL,updated_at=now()"""),
          {"s": source_id, "l": lead_id, "f": form_id, "r": rid,
           "status": "completed" if rid else "suppressed"})
        db.commit()


def backfill(source_id, job_id=None):
    with Session(engine) as db:
        row, credentials = connection(db, source_id)
        config = row["config"]
        since = int((datetime.now(timezone.utc) - timedelta(days=config["backfill_days"])).timestamp())
        db.commit()
        with Meta(config, credentials) as client:
            for form in client.pages(f'/{config["page_id"]}/leadgen_forms', {"fields": "id,name", "limit": 100}):
                for lead in client.pages(f'/{form["id"]}/leads', {
                    "fields": "id", "limit": 100, "filtering": json.dumps([
                        {"field": "time_created", "operator": "GREATER_THAN", "value": since}])}):
                    queue_lead(db, source_id, str(lead["id"]), str(form["id"]))
                    db.commit()
                    if job_id:
                        from app.jobs.queue import heartbeat
                        heartbeat(db, job_id, {"phase": "backfill", "form_id": str(form["id"])})
                        db.commit()


def queue_lead(db, source_id, lead_id, form_id=None):
    if not str(lead_id).isdigit() or len(str(lead_id)) > 40 or (
            form_id is not None and (not str(form_id).isdigit() or len(str(form_id)) > 40)):
        raise MetaError("Invalid Meta lead/form ID.")
    status = db.scalar(text("""INSERT INTO meta_leads(source_id,lead_id,form_id)
      VALUES(:s,:l,:f) ON CONFLICT(source_id,lead_id) DO UPDATE SET updated_at=now()
      RETURNING status"""), {"s": source_id, "l": lead_id, "f": form_id})
    if status not in {"completed", "suppressed"}:
        return enqueue(db, "meta.lead", {"source_id": source_id, "lead_id": lead_id},
                       dedupe_key=f"meta-lead:{source_id}:{lead_id}")


def finish_consents(db, source_id):
    # Bound work to new/replayed captures. Do not rescan years of completed
    # leads and attempt every historical event again for each arriving lead.
    db.execute(text("DROP TABLE IF EXISTS pg_temp.meta_pending_work"))
    db.execute(text("""CREATE TEMP TABLE meta_pending_work ON COMMIT DROP AS
      SELECT s.id FROM meta_pending_contacts p JOIN source_records s ON s.id=p.source_record_id
      WHERE s.source_id=:source AND s.profile_id IS NOT NULL"""), {"source": source_id})
    db.execute(text("""INSERT INTO events(source_id,profile_id,type,name,occurred_at,properties,message_id)
      SELECT s.source_id,s.profile_id,'track',CASE s.attributes->>'capture_source'
        WHEN 'meta_lead_ads' THEN 'Meta Lead' WHEN 'facebook_login' THEN 'Facebook Login'
        ELSE 'Messenger Contact' END,
        coalesce((s.attributes->>'captured_at')::timestamptz,s.created_at),
        jsonb_strip_nulls(jsonb_build_object('source_record_id',s.id,
          'form_id',s.attributes->>'meta_form_id','form_name',s.attributes->>'meta_form_name',
          'source_name',s.attributes->>'source_name')),
        'meta-capture:'||s.id::text
      FROM source_records s JOIN meta_pending_work w ON w.id=s.id JOIN active_profiles p ON p.id=s.profile_id
      WHERE s.source_id=:source AND s.attributes->>'capture_source'
        IN ('meta_lead_ads','facebook_login','meta_messenger')
      ON CONFLICT(source_id,message_id,occurred_at) DO NOTHING"""), {"source": source_id})
    # Use the same database precedence function as imports/manual changes.
    db.execute(text("""INSERT INTO consents(profile_id,channel,status,source_id,captured_at,evidence)
      SELECT DISTINCT ON (s.profile_id,s.attributes->'meta_consent'->>'channel')
        s.profile_id,s.attributes->'meta_consent'->>'channel',
        s.attributes->'meta_consent'->>'status',s.source_id,
        (s.attributes->'meta_consent'->>'captured_at')::timestamptz,
        (s.attributes->'meta_consent') || jsonb_build_object('source','meta_lead_ads',
          'source_record_id',s.id,'form_id',s.attributes->>'meta_form_id',
          'form_name',s.attributes->>'meta_form_name')
      FROM source_records s JOIN meta_pending_work w ON w.id=s.id
      WHERE s.source_id=:source AND s.profile_id IS NOT NULL
        AND s.attributes->'meta_consent'->>'channel' IS NOT NULL
      ORDER BY s.profile_id,s.attributes->'meta_consent'->>'channel',
        CASE WHEN s.attributes->'meta_consent'->>'status'='unknown' THEN 1 ELSE 0 END,
        (s.attributes->'meta_consent'->>'captured_at')::timestamptz DESC
      ON CONFLICT(profile_id,channel) DO UPDATE SET status=EXCLUDED.status,
        source_id=EXCLUDED.source_id,captured_at=EXCLUDED.captured_at,evidence=EXCLUDED.evidence
      WHERE consent_candidate_wins(consents.status,consents.captured_at,consents.source_id,
        EXCLUDED.status,EXCLUDED.captured_at,EXCLUDED.source_id)
    """), {"source": source_id})
    db.execute(text("""DELETE FROM meta_pending_contacts p USING meta_pending_work w
      WHERE p.source_record_id=w.id"""))
