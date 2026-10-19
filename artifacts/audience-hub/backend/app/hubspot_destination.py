"""Consent-filtered static-list reconciliation using native CRM batch APIs."""
import json
from urllib.parse import quote

from sqlalchemy import select, text

from app.activations import security, service
from app.hubspot_client import HubSpot
from app.hubspot_source import assert_portal
from app.models import Activation, Segment
from app.source_connectors import SourceError


def enqueue_destination(db, destination, user_id=None, dedupe_key=None):
    if destination.type != "hubspot" or not destination.is_active:
        raise ValueError("Choose an active HubSpot destination.")
    if not db.get(Segment, destination.config["segment_id"]):
        raise ValueError("Destination segment does not exist.")
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
               {"key": f"hubspot-destination:{destination.id}"})
    activation = db.scalar(select(Activation).where(
        Activation.destination_id == destination.id, Activation.name == "Managed HubSpot sync").order_by(Activation.id).limit(1))
    if activation is None:
        activation = Activation(name="Managed HubSpot sync", destination_id=destination.id,
                                segment_id=destination.config["segment_id"], required_consent="email",
                                field_mapping=service.preset("hubspot"), created_by=user_id)
        db.add(activation)
    activation.segment_id = destination.config["segment_id"]
    activation.required_consent = "email"
    activation.is_active = True
    db.flush()
    run, job = service.enqueue_run(db, activation.id, user_id, dedupe_key=dedupe_key)
    return {"run_id": run.id, "job_id": job.id, "status": run.status}


def ensure_property(client):
    path = "/crm/v3/properties/contacts/kinship_master_id"
    prop = client.request("GET", path, allow_missing=True)
    if prop is None:
        try:
            prop = client.request("POST", "/crm/v3/properties/contacts", body={
                "name": "kinship_master_id", "label": "Kinship Master ID", "type": "string",
                "fieldType": "text", "groupName": "contactinformation"})
        except SourceError:
            prop = client.request("GET", path, allow_missing=True)
            if prop is None:
                raise
    if prop.get("type") != "string" or prop.get("modificationMetadata", {}).get("readOnlyValue"):
        raise SourceError("Existing kinship_master_id must be a writable string property.")


def ensure_list(client, config):
    if config.get("list_id"):
        data = client.request("GET", f"/crm/v3/lists/{config['list_id']}")
    else:
        data = client.request("GET", "/crm/v3/lists/object-type-id/0-1/name/" + quote(config["list_name"], safe=""),
                              allow_missing=True)
        if data is None:
            data = client.request("POST", "/crm/v3/lists", body={
                "name": config["list_name"], "objectTypeId": "0-1", "processingType": "MANUAL"})
    result = data.get("list", data)
    if result.get("processingType") != "MANUAL" or result.get("objectTypeId") != "0-1":
        raise SourceError("HubSpot destination must be a dedicated MANUAL contact list, not an active/dynamic list.")
    list_id = str(result["listId"])
    if result.get("name") != config["list_name"]:
        client.request("PUT", f"/crm/v3/lists/{list_id}/update-list-name", params={"listName": config["list_name"]})
    return list_id


def sync_master_ids(client, expected):
    """Native-ID-keyed, bounded batch; never write an already-correct value."""
    contacts = client.request("POST", "/crm/v3/objects/contacts/batch/read", body={
        "properties": ["kinship_master_id", "email", "hs_email_optout"],
        "inputs": [{"id": identifier} for identifier in expected]}).get("results", [])
    if {str(c["id"]) for c in contacts} != set(expected):
        raise SourceError("HubSpot batch omitted contacts; list membership was not advanced.")
    updates = [{"id": str(c["id"]), "properties": {"kinship_master_id": expected[str(c["id"])]["master_id"]}}
               for c in contacts if c["properties"].get("kinship_master_id") != expected[str(c["id"])]["master_id"]]
    if updates:
        response = client.request("POST", "/crm/v3/objects/contacts/batch/update", body={"inputs": updates})
        if {str(c["id"]) for c in response.get("results", [])} != {item["id"] for item in updates}:
            raise SourceError("HubSpot did not confirm every Master ID update.")
    return contacts, len(updates), len(contacts)-len(updates)


def deliver(db, run, snapshot):
    from app.identity.normalize import normalize_email
    from app.privacy.service import digest
    result = {"list_id": None, "added": 0, "removed": 0,
              "master_ids_updated": 0, "master_ids_unchanged": 0}
    run.hubspot_result = result
    try:
        with HubSpot(snapshot["secrets"]["credentials"]) as client:
            info = client.info()
            if any(info["missing_scopes"][feature] for feature in ("master_id", "lists")):
                raise SourceError("HubSpot destination is missing required scopes; use Test connection.")
            assert_portal(db, info["account"]["hub_id"])
            ensure_property(client)
            list_id = ensure_list(client, snapshot["config"])
            result["list_id"] = list_id
            if not snapshot["config"].get("list_id"):
                # Persist remote identity independently of delivery. Future
                # name edits rename this list rather than orphaning it. The
                # compare-and-set never overwrites a concurrent admin edit.
                from sqlalchemy.orm import Session
                with Session(db.get_bind()) as control:
                    control.execute(text("""UPDATE destinations SET config=jsonb_set(config,'{list_id}',
                        to_jsonb(CAST(:list_id AS text))) WHERE id=:id AND config=CAST(:config AS jsonb)"""),
                                    {"id": snapshot["destination_id"], "list_id": list_id,
                                     "config": json.dumps(snapshot["config"])})
                    control.commit()
            # One destination owns one list; concurrent activations may not
            # reconcile the same remote list against different snapshots.
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                       {"key": f"hubspot-list:{info['account']['hub_id']}:{list_id}"})
            ambiguous = db.scalar(text("""SELECT EXISTS(
                SELECT 1 FROM segment_membership sm JOIN active_profiles p ON p.id=sm.profile_id
                JOIN identity_xref x ON x.master_id=p.master_id AND x.key_type='hubspot_record_id'
                JOIN identity_xref other ON other.key_type=x.key_type AND other.key_value=x.key_value
                JOIN active_profiles p2 ON p2.master_id=other.master_id
                WHERE sm.segment_id=:segment AND p.master_id<>p2.master_id)"""), {"segment": snapshot["segment_id"]})
            if ambiguous:
                raise SourceError("Ambiguous HubSpot identity ownership must be reviewed before sync.")
            db.execute(text("""DROP TABLE IF EXISTS pg_temp.hs_ids;
                DROP TABLE IF EXISTS pg_temp.hs_desired; DROP TABLE IF EXISTS pg_temp.hs_current;
                CREATE TEMP TABLE hs_ids(contact_id text PRIMARY KEY,master_id text,profile_id bigint) ON COMMIT DROP;
                CREATE TEMP TABLE hs_desired(contact_id text PRIMARY KEY,email_hashes text[]) ON COMMIT DROP;
                CREATE TEMP TABLE hs_current(contact_id text PRIMARY KEY) ON COMMIT DROP"""))
            db.execute(text("""INSERT INTO hs_ids SELECT x.key_value,min(p.master_id),min(p.id)
                FROM segment_membership sm JOIN active_profiles p ON p.id=sm.profile_id
                JOIN identity_xref x ON x.master_id=p.master_id AND x.key_type='hubspot_record_id'
                WHERE sm.segment_id=:segment
                GROUP BY x.key_value"""), {"segment": snapshot["segment_id"]})
            db.execute(text("""INSERT INTO hs_desired(contact_id) SELECT h.contact_id FROM hs_ids h
                JOIN activation_work w ON w.profile_id=h.profile_id"""))
            missing = db.execute(text("""DELETE FROM activation_work w WHERE NOT EXISTS(
                SELECT 1 FROM hs_ids h WHERE h.profile_id=w.profile_id)""")).rowcount
            run.profiles_excluded_missing_fields += missing
            after = ""
            while True:
                batch = db.execute(text("SELECT * FROM hs_ids WHERE contact_id>:after ORDER BY contact_id LIMIT 100"),
                                   {"after": after}).mappings().all()
                if not batch:
                    break
                expected = {row["contact_id"]: row for row in batch}
                contacts, updated, unchanged = sync_master_ids(client, expected)
                result["master_ids_updated"] += updated
                result["master_ids_unchanged"] += unchanged
                from app.keyring import pii_hashes
                hashes = {str(c["id"]): pii_hashes(normalize_email(c["properties"].get("email")) or "")
                          for c in contacts}
                suppressed = set(db.scalars(text("SELECT value_hash FROM suppressions WHERE type='email' AND value_hash=ANY(:hashes)"),
                                           {"hashes": [hashed for candidates in hashes.values() for hashed in candidates]}))
                db.execute(text("""UPDATE hs_desired d SET email_hashes=ARRAY(SELECT jsonb_array_elements_text(h.value))
                    FROM jsonb_each(CAST(:hashes AS jsonb)) h WHERE d.contact_id=h.key"""),
                           {"hashes": json.dumps(hashes)})
                blocked = []
                for contact in contacts:
                    identifier, props = str(contact["id"]), contact["properties"]
                    if (not normalize_email(props.get("email")) or suppressed.intersection(hashes[identifier])
                            or str(props.get("hs_email_optout", "")).lower() == "true"):
                        blocked.append(identifier)
                if blocked:
                    db.execute(text("DELETE FROM hs_desired WHERE contact_id=ANY(:ids)"), {"ids": blocked})
                after = batch[-1]["contact_id"]
            cursor = None
            while True:
                page = client.request("GET", f"/crm/v3/lists/{list_id}/memberships",
                                      params={"limit": 1000, **({"after": cursor} if cursor else {})})
                members = [{"id": str(item["recordId"])} for item in page.get("results", [])]
                if members:
                    db.execute(text("INSERT INTO hs_current VALUES (:id) ON CONFLICT DO NOTHING"), members)
                next_cursor = page.get("paging", {}).get("next", {}).get("after")
                if next_cursor is None:
                    break
                if next_cursor == cursor:
                    raise SourceError("HubSpot list pagination did not advance.")
                cursor = next_cursor
            # Remove before adding. Even an interrupted run cannot retain a
            # newly excluded contact merely because the add phase failed.
            prune = """DELETE FROM hs_desired d USING hs_ids h WHERE h.contact_id=d.contact_id
                AND (NOT EXISTS(SELECT 1 FROM consents c WHERE c.profile_id=h.profile_id
                      AND c.channel='email' AND c.status='opted_in')
                  OR EXISTS(SELECT 1 FROM suppressions s WHERE s.type='email' AND s.value_hash=ANY(d.email_hashes)))"""
            db.execute(text(prune))
            for operation, left, right in (("remove", "hs_current", "hs_desired"),
                                           ("add", "hs_desired", "hs_current"),
                                           ("remove", "hs_current", "hs_desired")):
                db.execute(text(prune))
                after = ""
                while True:
                    ids = list(db.scalars(text(f"""SELECT a.contact_id FROM {left} a
                        WHERE a.contact_id>:after AND NOT EXISTS(SELECT 1 FROM {right} b WHERE b.contact_id=a.contact_id)
                        ORDER BY a.contact_id LIMIT 1000"""), {"after": after}))
                    if not ids:
                        break
                    if operation == "add":
                        # Recheck newly arrived HubSpot suppressions immediately
                        # before each batch; a long property sync must not freeze
                        # its marketing eligibility at the start of the run.
                        db.execute(text(prune + " AND d.contact_id=ANY(:ids)"), {"ids": ids})
                        ids = list(db.scalars(text("SELECT contact_id FROM hs_desired WHERE contact_id=ANY(:ids) ORDER BY contact_id"),
                                              {"ids": ids}))
                        if not ids:
                            continue
                    client.request("PUT", f"/crm/v3/lists/{list_id}/memberships/{operation}", body=ids)
                    result["removed" if operation == "remove" else "added"] += len(ids)
                    if operation == "remove":
                        db.execute(text("DELETE FROM hs_current WHERE contact_id=ANY(:ids)"), {"ids": ids})
                    else:
                        db.execute(text("INSERT INTO hs_current VALUES (:id) ON CONFLICT DO NOTHING"),
                                   [{"id": identifier} for identifier in ids])
                    after = ids[-1]
            blocked_profiles = db.execute(text("""DELETE FROM activation_work w WHERE NOT EXISTS(
                SELECT 1 FROM hs_desired d JOIN hs_ids h USING(contact_id) WHERE h.profile_id=w.profile_id)""")).rowcount
            run.profiles_excluded_suppressed += blocked_profiles
            run.profiles_sent = db.scalar(text("""SELECT count(DISTINCT h.profile_id)
                FROM hs_desired d JOIN hs_ids h USING(contact_id)"""))
            result["contacts_synced"] = db.scalar(text("SELECT count(*) FROM hs_desired"))
    except SourceError as error:
        raise security.DeliveryError(str(error)) from None
    finally:
        run.hubspot_result = dict(result)


def tick_destinations(db, now):
    from app.models import Destination
    from app.source_connectors import window
    from app.jobs.scheduler import schedule_once
    for destination in db.scalars(select(Destination).where(Destination.type == "hubspot", Destination.is_active)):
        token = window(destination.config.get("schedule", {"frequency": "manual"}), now)
        if token:
            schedule_once(db, f"hubspot-destination:{destination.id}", token, "hubspot.destination",
                          {"destination_id": destination.id})
