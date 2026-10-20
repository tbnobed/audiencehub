"""Only Kinship-created audiences; ad-set results are not person-level facts."""
import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.activations import security
from app.db import engine
from app.jobs.queue import enqueue
from app.meta_capture import connection
from app.meta_client import Meta, MetaError
from app.meta_privacy import safe_rule, safe_mapping
from app.models import Destination, Segment


def create_audience(db, segment_id, source_id, name, user_id):
    segment = db.get(Segment, segment_id)
    if not segment:
        raise MetaError("Audience not found.")
    safe_rule(db, segment.definition)
    row, credentials = connection(db, source_id)
    account = row["config"].get("ad_account_id", "").removeprefix("act_")
    if not account:
        raise MetaError("Configure a Meta ad account and advertising token first.")
    # Serialize remote creation against other creates for the same local pair.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
               {"key": f"meta-audience:{source_id}:{segment_id}"})
    existing = db.execute(text("""SELECT meta_audience_id,destination_id FROM meta_audiences
      WHERE source_id=:s AND segment_id=:g ORDER BY id LIMIT 1"""),
      {"s": source_id, "g": segment_id}).mappings().first()
    if existing:
        return dict(existing)
    with Meta(row["config"], credentials, ads=True) as client:
        remote = client.request("POST", f"/act_{account}/customaudiences", body={
            "name": name, "subtype": "CUSTOM", "customer_file_source": "USER_PROVIDED_ONLY",
            "description": f"Kinship segment {segment_id}; giving/signup/consent-safe data only."})
    audience_id = str(remote.get("id", ""))
    if not audience_id.isdigit():
        raise MetaError("Meta did not return a custom audience ID.")
    destination = Destination(name=name, type="meta_custom_audience",
        config={"meta_source_id": source_id, "meta_audience_id": audience_id,
                "delimiter": ",", "include_pii": True}, is_active=True,
        secret_encrypted=security.encrypt({}))
    db.add(destination)
    db.flush()
    db.execute(text("""INSERT INTO meta_audiences(segment_id,source_id,meta_audience_id,name,destination_id)
      VALUES(:g,:s,:a,:n,:d)"""),
      {"g": segment_id, "s": source_id, "a": audience_id, "n": name, "d": destination.id})
    return {"meta_audience_id": audience_id, "destination_id": destination.id}


def assert_owned(db, destination_id, segment_id, config):
    if not db.scalar(text("""SELECT EXISTS(SELECT 1 FROM meta_audiences
      WHERE destination_id=:d AND segment_id=:g AND source_id=:s AND meta_audience_id=:a)"""),
      {"d": destination_id, "g": segment_id, "s": config["meta_source_id"], "a": config["meta_audience_id"]}):
        raise MetaError("Connected Meta destination does not belong to this Kinship audience.")


def send(db, run, snapshot):
    config = snapshot["config"]
    assert_owned(db, snapshot["destination_id"], snapshot["segment_id"], config)
    safe_mapping(snapshot["mapping"])
    safe_rule(db, db.get(Segment, snapshot["segment_id"]).definition)
    row, credentials = connection(db, config["meta_source_id"])
    # The shared export pipeline has already applied consent, suppression,
    # required identifiers and optional one-per-household filtering.
    # USER_DATA_REPLACE session is a full replacement, not add-only growth.
    schema = ["EMAIL", "PHONE", "FN", "LN", "ZIP", "CT", "ST", "COUNTRY", "EXTERN_ID"]
    with Meta(row["config"], credentials, ads=True) as client, open(run.file_path, newline="") as stream:
        reader = csv.reader(stream, delimiter=config.get("delimiter", ","))
        headers = next(reader, [])
        if headers != ["email", "phone", "fn", "ln", "zip", "ct", "st", "country", "external_id"]:
            raise MetaError("Meta export schema did not match the approved identity-only contract.")
        batch, sequence = [], 0
        def upload(rows, last):
            nonlocal sequence
            sequence += 1
            receipt = client.request("POST", f'/{config["meta_audience_id"]}/usersreplace', body={
                "session": {"session_id": run.id, "batch_seq": sequence, "last_batch_flag": last},
                "payload": {"schema": schema, "data": rows}})
            if int(receipt.get("num_invalid_entries", 0)) > 0:
                raise MetaError("Meta rejected records in the replacement; review the audience receipt before retrying.")
            if receipt.get("num_received") is not None and int(receipt["num_received"]) != len(rows):
                raise MetaError("Meta did not acknowledge the complete replacement batch.")
        # Keep one batch buffered so last_batch_flag is correct, including an
        # empty full replacement when all previous members lost eligibility.
        for record in reader:
            if len(batch) == 1000:
                upload(batch, False)
                batch = []
            if len(record) != 9 or any(v and (len(v) != 64 or any(c not in "0123456789abcdef" for c in v)) for v in record):
                raise MetaError("Meta exports must contain normalized SHA-256 identifiers only.")
            batch.append(record)
        upload(batch, True)


def conversion_metric(insight):
    # Pick one reported business action, never add overlapping roll-up aliases.
    actions = {v["action_type"]: Decimal(str(v.get("value", 0))) for v in insight.get("actions", [])}
    for action in ("offsite_conversion.fb_pixel_purchase", "offsite_conversion.fb_pixel_complete_registration",
                   "onsite_conversion.lead_grouped", "lead"):
        if action in actions:
            return actions[action], action
    return Decimal(0), "No giving/signup/lead conversion action reported"


def refresh_results(segment_id, job_id=None):
    with Session(engine) as db:
        from app.privacy.locking import privacy_shared
        privacy_shared(db)
        audiences = db.execute(text("SELECT * FROM meta_audiences WHERE segment_id=:id"), {"id": segment_id}).mappings().all()
        if not audiences:
            return
        for audience in audiences:
            safe_rule(db, db.get(Segment, segment_id).definition)
            row, credentials = connection(db, audience["source_id"])
            account = row["config"]["ad_account_id"].removeprefix("act_")
            today = datetime.now(timezone.utc).date()
            with Meta(row["config"], credentials, ads=True) as client:
                account_data = client.request("GET", f"/act_{account}", {"fields": "currency"})
                for adset in client.pages(f"/act_{account}/adsets", {"fields": "id,name,targeting", "limit": 100}):
                    included = {str(a["id"]) for a in adset.get("targeting", {}).get("custom_audiences", [])}
                    if audience["meta_audience_id"] not in included:
                        continue
                    # Meta cannot attribute an ad-set's spend/reach to one
                    # audience when multiple/expanded targeting is used.
                    # Store honestly labelled ad-set-level observations.
                    params = {"fields": "adset_id,adset_name,spend,reach,actions,date_start,date_stop",
                              "level": "adset", "time_increment": 1,
                              "time_range": json.dumps({"since": (today-timedelta(days=30)).isoformat(),
                                                        "until": (today-timedelta(days=1)).isoformat()})}
                    for insight in client.pages(f'/{adset["id"]}/insights', params):
                        count, action = conversion_metric(insight)
                        db.execute(text("""INSERT INTO meta_ad_results(audience_id,adset_id,date_start,date_stop,
                          adset_name,spend,reach,conversions,currency,conversion_action)
                          VALUES(:a,:id,:start,:stop,:name,:spend,:reach,:conversions,:currency,:action)
                          ON CONFLICT(audience_id,adset_id,date_start,date_stop) DO UPDATE SET
                          adset_name=EXCLUDED.adset_name,spend=EXCLUDED.spend,reach=EXCLUDED.reach,
                          conversions=EXCLUDED.conversions,currency=EXCLUDED.currency,
                          conversion_action=EXCLUDED.conversion_action"""),
                          {"a": audience["id"], "id": str(adset["id"]), "start": insight["date_start"],
                           "stop": insight["date_stop"], "name": insight.get("adset_name", adset["name"]),
                           "spend": insight.get("spend", 0), "reach": insight.get("reach", 0),
                           "conversions": count, "currency": account_data.get("currency"), "action": action})
                db.execute(text("UPDATE meta_audiences SET last_refreshed_at=now() WHERE id=:id"),
                           {"id": audience["id"]})
                if job_id:
                    from app.jobs.queue import heartbeat
                    heartbeat(db, job_id, {"phase": "results", "audience_id": audience["id"]})
                db.commit()
