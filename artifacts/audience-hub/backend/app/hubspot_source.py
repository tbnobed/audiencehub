"""HubSpot pages are immutable COPY manifests; only a complete run moves its cursor."""
import json
import time
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.activations import security
from app.db import engine
from app.hubspot_client import HubSpot, DEFAULT_PROPERTIES
from app.identity.locking import IdentityResolutionDeferred
from app.identity.normalize import normalize_email
from app.imports.mapping import suggest_mapping
from app.jobs.queue import enqueue, heartbeat
from app.source_connectors import SourceError
from app.source_pulls import connection_row, run_source, source_lock, spool
from app.source_rows import mapped_row


def assert_portal(db, hub_id):
    if db.scalar(text("SELECT EXISTS(SELECT 1 FROM hubspot_pulls WHERE hub_id<>:hub)"), {"hub": hub_id}):
        raise SourceError("HubSpot account differs from the account bound to Kinship identity keys.")


def mapped_contact(contact, mapping):
    incoming = dict(contact.get("properties", {}))
    incoming["hs_object_id"] = str(contact["id"])
    columns = {"firstname": "first_name", "lastname": "last_name", "address": "address1",
               "state": "region", "zip": "postal_code", **mapping.get("columns", {})}
    # Neither editable mapping nor a vendor property can replace the real key.
    columns["hs_object_id"] = "hubspot_record_id"
    result = mapped_row(incoming, {"feed": "people", "columns": columns})
    result["hubspot_record_id"] = str(contact["id"])
    result["external_id"] = str(contact["id"])
    # Subscription identity is the provider's native email, independently of
    # an administrator mapping another property to the canonical email field.
    result["hubspot_native_email"] = normalize_email(incoming.get("email")) or ""
    result["hubspot_native_email_present"] = True
    return result


def preview(row):
    mapping = row["config"]["mappings"][0]
    with HubSpot(security.decrypt(row["credentials"])) as client:
        contacts = client.contacts(0, int(time.time() * 1000), "0", mapping.get("properties", DEFAULT_PROPERTIES))
    rows = [mapped_contact(contact, mapping) for contact in contacts[:100]]
    fields = sorted({key for item in rows for key in item})
    return {"limit_per_mapping": 100, "items": [{"mapping_id": "contacts", "rows_checked": len(rows),
        "rows_rejected": 0, "rows": rows, "fields": fields, "identity_keys": ["hubspot_record_id"],
        "suggested_columns": {**suggest_mapping(fields, "contact"), "hs_object_id": "hubspot_record_id"}}]}


def pulse(db, job_id, pull, count, phase="collecting"):
    if job_id is not None and not heartbeat(db, job_id, {"phase": phase, "rows_read": count,
                                                       "hubspot_pull_id": pull["id"]}):
        from app.jobs.lifecycle import OwnershipLost
        raise OwnershipLost("HubSpot pull no longer owns its work.")


def capture_suppressions(db, contacts):
    from app.privacy.service import digest
    from app.identity.normalize import normalize_email
    for contact in contacts:
        email = normalize_email(contact.get("email"))
        statuses = contact.get("_hubspot_preferences", [])
        if email and any(item.get("status") == "UNSUBSCRIBED" for item in statuses):
            db.execute(text("""INSERT INTO suppressions(type,value_hash,reason)
                VALUES ('email',:hash,'hubspot_unsubscribe') ON CONFLICT(type,value_hash) DO NOTHING"""),
                       {"hash": digest(email)})


def run_hubspot(source_id, job_id=None):
    with source_lock(-1_000_000-source_id), Session(engine) as db:
        if job_id is not None:
            linked = db.scalar(text("SELECT progress->>'hubspot_pull_id' FROM jobs WHERE id=:id"), {"id": job_id})
            if linked:
                previous = db.execute(text("SELECT * FROM hubspot_pulls WHERE id=:id AND source_id=:source"),
                                      {"id": int(linked), "source": source_id}).mappings().first()
                if previous and previous["phase"] == "revoked":
                    raise SourceError("HubSpot pull was revoked for privacy; start a new run.")
                if previous and previous["phase"] == "completed":
                    heartbeat(db, job_id, {"phase": "completed", "batch_id": previous["batch_id"]})
                    db.commit()
                    return {"batch_id": previous["batch_id"]}
                if previous and previous["phase"] in {"copied", "consents"}:
                    failed = db.scalar(text("""SELECT status FROM jobs WHERE type='hubspot.finish'
                        AND payload->>'pull_id'=:pull ORDER BY id DESC LIMIT 1"""), {"pull": linked})
                    if failed in {"failed", "cancelled"}:
                        raise SourceError("HubSpot consent refresh failed. Review credentials/scopes and run the source again to resume.")
                    raise IdentityResolutionDeferred("HubSpot pull waits for subscription refresh.")
        row = connection_row(db, source_id)
        mapping = row["config"]["mappings"][0]
        with HubSpot(security.decrypt(row["credentials"])) as client:
            info = client.info()
            required = ["contacts", "subscriptions"] + (["engagement"] if row["config"].get("engagement") else [])
            if any(info["missing_scopes"][feature] for feature in required):
                raise SourceError("HubSpot is missing required source scopes. Use Test connection to review them.")
            assert_portal(db, info["account"]["hub_id"])
            pull = db.execute(text("""SELECT * FROM hubspot_pulls WHERE source_id=:source
                AND phase NOT IN ('completed','revoked') ORDER BY id DESC LIMIT 1"""),
                              {"source": source_id}).mappings().first()
            if pull is None:
                # Re-read a five-minute overlap for search-index delay/ties.
                since = max(0, int(row["state"].get("hubspot_cursor", 0)) - 300_000)
                until = max(1, int(time.time() * 1000) - 120_000)
                key = db.scalar(text("""INSERT INTO hubspot_pulls(source_id,hub_id,since_ms,until_ms)
                    VALUES (:source,:hub,:since,:until) RETURNING id"""),
                                {"source": source_id, "hub": info["account"]["hub_id"], "since": since, "until": until})
                db.commit()
                pull = db.execute(text("SELECT * FROM hubspot_pulls WHERE id=:id"), {"id": key}).mappings().one()
            pull = dict(pull)
            if pull["phase"] in {"copied", "consents"}:
                enqueue(db, "hubspot.finish", {"pull_id": pull["id"]}, dedupe_key=f"hubspot:finish:{pull['id']}")
                if job_id is not None:
                    heartbeat(db, job_id, {"phase": "consents", "batch_id": pull["batch_id"], "hubspot_pull_id": pull["id"]})
                db.commit()
                raise IdentityResolutionDeferred("HubSpot pull waits for subscription refresh.")
            manifests = list(pull["manifests"])
            while not pull["contacts_done"]:
                contacts = client.contacts(pull["since_ms"], pull["until_ms"], pull["after_id"],
                                           mapping.get("properties", DEFAULT_PROPERTIES))
                if not contacts:
                    pull["contacts_done"] = True
                else:
                    emails = list(dict.fromkeys(c["properties"].get("email") for c in contacts if c["properties"].get("email")))
                    # Preferences are also refreshed for UNCHANGED contacts in
                    # finish_hubspot: a subscription change need not change CRM
                    # lastmodifieddate. Do not infer it from the CRM cursor.
                    preferences = {}
                    mapped = []
                    for contact in contacts:
                        item = mapped_contact(contact, mapping)
                        statuses = list(preferences.get((contact["properties"].get("email") or "").lower(), []))
                        if str(contact["properties"].get("hs_email_optout", "")).lower() == "true":
                            statuses.append({"subscriptionId": "ALL", "status": "UNSUBSCRIBED",
                                             "timestamp": datetime.now(timezone.utc).isoformat(),
                                             "timestamp_kind": "observed"})
                        item["_hubspot_preferences"] = statuses
                        mapped.append(item)
                    manifest = spool(iter(mapped), "people", row["key"])
                    manifests.extend(manifest.pop("children"))
                    manifests.append(manifest)
                    capture_suppressions(db, mapped)
                    pull["after_id"] = str(contacts[-1]["id"])
                pulse(db, job_id, pull, sum(m["count"] for m in manifests))
                db.execute(text("""UPDATE hubspot_pulls SET after_id=:after,contacts_done=:done,
                    manifests=CAST(:files AS jsonb),updated_at=now() WHERE id=:id"""),
                           {"after": pull["after_id"], "done": pull["contacts_done"],
                            "files": json.dumps(manifests), "id": pull["id"]})
                db.commit()
            if row["config"].get("engagement"):
                while not pull["events_done"]:
                    params = {"startTimestamp": pull["since_ms"], "endTimestamp": pull["until_ms"], "limit": 1000}
                    if pull["event_cursor"]:
                        params["offset"] = pull["event_cursor"]
                    page = client.request("GET", "/email/public/v1/events", params=params)
                    names = {"SENT": "email_sent", "OPEN": "email_opened", "CLICK": "email_clicked", "BOUNCE": "email_bounced"}
                    events = [{"external_id": f"hubspot-email:{event['id']}", "name": names[event["type"]],
                               "occurred_at": datetime.fromtimestamp(int(event["created"])/1000, timezone.utc).isoformat(),
                               "email": event["recipient"], "properties": {
                                   "hubspot_email_event_id": event["id"], "email_campaign_id": event.get("emailCampaignId")}}
                              for event in page.get("events", []) if event.get("type") in names]
                    if events:
                        manifest = spool(iter(events), "events", row["key"])
                        manifests.extend(manifest.pop("children"))
                        manifests.append(manifest)
                    pull["events_done"] = not page.get("hasMore", False)
                    next_cursor = page.get("offset")
                    if not pull["events_done"] and (not next_cursor or next_cursor == pull["event_cursor"]):
                        raise SourceError("HubSpot engagement paging did not advance; resume after upstream recovery.")
                    pull["event_cursor"] = next_cursor
                    pulse(db, job_id, pull, sum(m["count"] for m in manifests))
                    db.execute(text("""UPDATE hubspot_pulls SET event_cursor=:cursor,events_done=:done,
                        manifests=CAST(:files AS jsonb),updated_at=now() WHERE id=:id"""),
                               {"cursor": next_cursor, "done": pull["events_done"], "files": json.dumps(manifests), "id": pull["id"]})
                    db.commit()
            db.execute(text("UPDATE hubspot_pulls SET phase='prepared' WHERE id=:id"), {"id": pull["id"]})
            db.commit()
        if manifests:
            result = run_source(source_id, mapping_id=f"migration_hubspot_{pull['id']}", manifests=manifests, job_id=job_id)
            batch_id = result["batch_id"]
        else:
            batch_id = None
        db.execute(text("UPDATE hubspot_pulls SET phase='copied',batch_id=:batch WHERE id=:id"),
                   {"id": pull["id"], "batch": batch_id})
        enqueue(db, "hubspot.finish", {"pull_id": pull["id"]}, dedupe_key=f"hubspot:finish:{pull['id']}")
        if job_id is not None:
            heartbeat(db, job_id, {"phase": "consents", "batch_id": batch_id, "hubspot_pull_id": pull["id"]})
        db.commit()
        if job_id is not None:
            raise IdentityResolutionDeferred("HubSpot pull waits for subscription refresh.")
        return {"batch_id": batch_id, "files": len(manifests)}


def finish_hubspot(pull_id, job_id=None):
    from app.privacy.locking import privacy_shared
    from app.identity.locking import acquire_resolver_lock
    from app.imports.staging import copy_upsert
    from app.privacy.consent import consent_wins
    with source_lock(-2_000_000-pull_id), Session(engine) as db:
        pull = db.execute(text("SELECT * FROM hubspot_pulls WHERE id=:id FOR UPDATE"), {"id": pull_id}).mappings().one()
        if pull["phase"] in {"completed", "revoked"}:
            return
        pulse(db, job_id, pull, 0, "consents")
        if pull["batch_id"]:
            if db.scalar(text("SELECT phase FROM identity_batches WHERE id=:id"), {"id": pull["batch_id"]}) != "completed":
                raise IdentityResolutionDeferred("HubSpot consent waits for contact identity resolution.")
        pull = dict(pull)
        encrypted = db.scalar(text("SELECT credentials FROM source_connections WHERE source_id=:id"), {"id": pull["source_id"]})
        db.execute(text("UPDATE hubspot_pulls SET phase='consents' WHERE id=:id"), {"id": pull_id})
        db.commit()
        with HubSpot(security.decrypt(encrypted)) as client:
            info = client.info()
            if info["account"]["hub_id"] != pull["hub_id"]:
                raise SourceError("HubSpot account changed during subscription refresh.")
            while True:
                records = db.execute(text("""SELECT sr.id,sr.profile_id,
                    CASE WHEN sr.attributes ? 'hubspot_native_email_present'
                      THEN NULLIF(sr.attributes->>'hubspot_native_email','')
                      ELSE sr.email_norm::text END AS email,sr.attributes
                    FROM source_records sr JOIN imports i ON i.id=sr.last_import_id
                    WHERE sr.source_id=:source AND i.record_type='contact' AND sr.id>:after
                    ORDER BY sr.id LIMIT 100"""), {"source": pull["source_id"], "after": pull["consent_after_id"]}).mappings().all()
                db.commit()
                if not records:
                    break
                emails = list(dict.fromkeys(r["email"] for r in records if r["email"]))
                preferences = client.preferences(emails, info["scopes"]) if emails else {}
                mapped, candidates = [], {}
                for record in records:
                    statuses = list(preferences.get((record["email"] or "").lower(), []))
                    if str(record["attributes"].get("hs_email_optout", "")).lower() == "true":
                        statuses.append({"subscriptionId": "ALL", "status": "UNSUBSCRIBED",
                                         "timestamp": datetime.now(timezone.utc).isoformat(), "timestamp_kind": "observed"})
                    mapped.append({"email": record["email"], "_hubspot_preferences": statuses})
                privacy_shared(db)
                pulse(db, job_id, pull, pull["consent_after_id"], "consents")
                capture_suppressions(db, mapped)
                db.commit()  # Restriction is visible before any wait for the resolver.
                privacy_shared(db)
                pulse(db, job_id, pull, pull["consent_after_id"], "consents")
                acquire_resolver_lock(db)
                for record, incoming in zip(records, mapped):
                    current_profile = db.scalar(text("SELECT profile_id FROM source_records WHERE id=:id"), {"id": record["id"]})
                    statuses = incoming["_hubspot_preferences"]
                    db.execute(text("""UPDATE source_records SET attributes=jsonb_set(attributes,
                        '{_hubspot_preferences}',CAST(:statuses AS jsonb)) WHERE id=:id"""),
                               {"statuses": json.dumps(statuses), "id": record["id"]})
                    known = [item for item in statuses if item.get("status") in {"SUBSCRIBED", "UNSUBSCRIBED"}]
                    if not current_profile or not known:
                        continue
                    decision = [item for item in known if item["status"] == "UNSUBSCRIBED"] or known
                    status = "opted_out" if decision[0]["status"] == "UNSUBSCRIBED" else "opted_in"
                    captured = max(datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")) for item in decision)
                    candidate = {"profile_id": current_profile, "channel": "email", "status": status, "source_id": pull["source_id"],
                                 "captured_at": captured, "evidence": json.dumps({"platform": "hubspot", "subscription_types": statuses})}
                    if consent_wins(candidates.get(current_profile), candidate):
                        candidates[current_profile] = candidate
                if candidates:
                    changed = copy_upsert(db, """INSERT INTO consents(profile_id,channel,status,source_id,captured_at,evidence)
                        SELECT :profile_id,:channel,:status,:source_id,:captured_at,:evidence FROM {stage} WHERE true
                        ON CONFLICT(profile_id,channel) DO UPDATE SET status=EXCLUDED.status,source_id=EXCLUDED.source_id,
                          captured_at=EXCLUDED.captured_at,evidence=EXCLUDED.evidence
                        WHERE consent_candidate_wins(consents.status,consents.captured_at,consents.source_id,
                            EXCLUDED.status,EXCLUDED.captured_at,EXCLUDED.source_id) RETURNING profile_id""",
                                list(candidates.values()), "profile_id bigint,channel text,status text,source_id bigint,captured_at timestamptz,evidence jsonb",
                                returning=True)
                    if changed:
                        db.execute(text("""INSERT INTO trait_dirty_profiles(profile_id,dirtied_at)
                            SELECT unnest(CAST(:ids AS bigint[])),clock_timestamp()
                            ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at"""),
                                   {"ids": [row[0] for row in changed]})
                pull["consent_after_id"] = records[-1]["id"]
                db.execute(text("UPDATE hubspot_pulls SET consent_after_id=:after,updated_at=now() WHERE id=:id"),
                           {"after": pull["consent_after_id"], "id": pull_id})
                db.commit()
        if pull["batch_id"]:
            enqueue(db, "traits.recompute", {"mode": "full", "batch_id": pull["batch_id"]},
                    dedupe_key=f"hubspot:traits:{pull_id}")
        else:
            enqueue(db, "traits.recompute", {"mode": "dirty"}, dedupe_key=f"hubspot:traits:{pull_id}")
        pulse(db, job_id, pull, pull["consent_after_id"], "consents")
        db.execute(text("""UPDATE source_connections SET state=jsonb_set(state,'{hubspot_cursor}',CAST(:cursor AS jsonb)),
            updated_at=now() WHERE source_id=:source"""), {"cursor": str(pull["until_ms"]), "source": pull["source_id"]})
        db.execute(text("UPDATE hubspot_pulls SET phase='completed',updated_at=now() WHERE id=:id"), {"id": pull_id})
        db.commit()


def expire_checkpoints(db, days):
    """Abandon stale inactive checkpoints, never their committed import data."""
    if days <= 0:
        return 0
    from app.privacy.service import owned_file, remove_owned_file
    from app.config import get_settings
    count = 0
    for pull in db.execute(text("""SELECT p.* FROM hubspot_pulls p
        WHERE p.manifests<>'[]'::jsonb AND p.updated_at<now()-(:days * interval '1 day')
        AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.status IN ('queued','running')
          AND ((j.type='source.pull' AND j.payload->>'source_id'=p.source_id::text)
            OR (j.type='hubspot.finish' AND j.payload->>'pull_id'=p.id::text)))
        FOR UPDATE"""), {"days": days}).mappings():
        for manifest in pull["manifests"]:
            remove_owned_file(owned_file(manifest["path"], get_settings().upload_dir), get_settings().upload_dir)
        db.execute(text("""UPDATE hubspot_pulls SET manifests='[]'::jsonb,after_id='0',event_cursor=NULL,
            consent_after_id=0,phase=CASE WHEN phase='completed' THEN phase ELSE 'revoked' END
            WHERE id=:id"""), {"id": pull["id"]})
        if pull["phase"] != "completed":
            db.execute(text("UPDATE source_connections SET state=state-'pending' WHERE source_id=:source"),
                       {"source": pull["source_id"]})
        count += 1
    return count
