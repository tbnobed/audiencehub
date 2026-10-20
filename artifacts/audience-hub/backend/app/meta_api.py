"""Local-admin Meta setup plus signed callbacks; no hosted integration dependency."""
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.activations import security
from app.auth.deps import require_role
from app.db import session_scope
from app.jobs.queue import enqueue
from app.meta_capture import DEFAULT_CONFIG, config_valid, connection, contact, queue_lead
from app.meta_client import Meta, MetaError
from app.models import AuditLog, Source, User

router = APIRouter(prefix="/api/meta", tags=["meta"])
public_router = APIRouter(prefix="/v1/meta", tags=["meta-capture"])
admin = Depends(require_role("admin"))
analyst = Depends(require_role("analyst"))


class ConnectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict
    credentials: dict | None = Field(default=None, repr=False)


def source(db, source_id):
    item = db.get(Source, source_id)
    if item is None or item.kind != "meta_lead_ads":
        raise HTTPException(404, "Meta Lead Ads source not found")
    return item


def audit(db, user, action, source_id):
    db.add(AuditLog(user_id=user.id, actor_type="user", action=action, entity_type="source",
                    entity_id=str(source_id), details={}))


@router.get("/sources/{source_id}/connection")
def get_connection(source_id: int, db: Session = Depends(session_scope), user: User = admin):
    source(db, source_id)
    row = db.execute(text("SELECT config FROM meta_connections WHERE source_id=:id"), {"id": source_id}).mappings().first()
    return {"configured": bool(row), "has_credentials": bool(row),
            "config": row["config"] if row else DEFAULT_CONFIG,
            "webhook_path": f"/v1/meta/webhook/{source_id}"}


@router.put("/sources/{source_id}/connection")
def save_connection(source_id: int, body: ConnectionWrite, db: Session = Depends(session_scope), user: User = admin):
    source(db, source_id)
    try:
        config = config_valid(body.config)
        old = db.execute(text("SELECT * FROM meta_connections WHERE source_id=:id FOR UPDATE"),
                         {"id": source_id}).mappings().first()
        if old and old["connected_at"] and any(old["config"][k] != config[k] for k in ("page_id", "app_id")):
            raise MetaError("Create a new source to connect a different Page or Facebook app.")
        if old and old["config"].get("ad_account_id") != config["ad_account_id"] and db.scalar(text(
                "SELECT EXISTS(SELECT 1 FROM meta_audiences WHERE source_id=:id)"), {"id": source_id}):
            raise MetaError("Create a new source for a different ad account once an audience is linked.")
        credentials = security.decrypt(old["credentials"]) if old else {}
        supplied = body.credentials or {}
        if set(supplied) - {"page_access_token", "app_secret", "verify_token", "ads_access_token"}:
            raise MetaError("Unsupported Meta credential.")
        for key, value in supplied.items():
            if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(c) < 32 for c in value):
                raise MetaError("Invalid Meta credential.")
            credentials[key] = value
        if any(not credentials.get(k) for k in ("page_access_token", "app_secret", "verify_token")):
            raise MetaError("A Page token, app secret and webhook verification token are required.")
        if len(credentials["verify_token"]) < 24:
            raise MetaError("Use a random webhook verification token of at least 24 characters.")
        db.execute(text("""INSERT INTO meta_connections(source_id,config,credentials)
          VALUES(:id,CAST(:c AS jsonb),:s) ON CONFLICT(source_id) DO UPDATE SET
          config=EXCLUDED.config,credentials=EXCLUDED.credentials,updated_at=now()"""),
          {"id": source_id, "c": json.dumps(config), "s": security.encrypt(credentials)})
        replay_keys = ("field_mapping", "consent_form_id", "consent_field", "consent_text",
                       "consent_channel", "consent_true_values")
        if old and any(old["config"].get(k) != config[k] for k in replay_keys):
            db.execute(text("""UPDATE meta_leads SET status='queued',error=NULL
              WHERE source_id=:id AND status='completed' AND updated_at>=now()-interval '90 days'"""),
              {"id": source_id})
        audit(db, user, "meta.connection.save", source_id)
        db.commit()
        return get_connection(source_id, db, user)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.post("/sources/{source_id}/connect")
def connect(source_id: int, db: Session = Depends(session_scope), user: User = admin):
    try:
        row, credentials = connection(db, source_id)
        config = row["config"]
        with Meta(config, credentials) as client:
            info = client.request("GET", "/debug_token", {"input_token": credentials["page_access_token"]},
                                  token=config["app_id"] + "|" + credentials["app_secret"])["data"]
            if not info.get("is_valid") or str(info.get("app_id")) != config["app_id"]:
                raise MetaError("The Page token was not issued for the configured Facebook app.")
            missing = {"leads_retrieval", "pages_manage_metadata"} - set(info.get("scopes", []))
            if missing:
                raise MetaError("Page token lacks required scopes: " + ", ".join(sorted(missing)))
            page = client.request("GET", "/me", {"fields": "id,name"})
            if str(page["id"]) != config["page_id"]:
                raise MetaError("The Page token does not belong to the configured Page.")
            subscribed = client.request("POST", f'/{config["page_id"]}/subscribed_apps',
                                        body={"subscribed_fields": ["leadgen"]})
            if subscribed.get("success") is not True:
                raise MetaError("Meta did not confirm the Page leadgen subscription.")
        db.execute(text("UPDATE meta_connections SET connected_at=now() WHERE source_id=:id"), {"id": source_id})
        job = enqueue(db, "meta.backfill", {"source_id": source_id}, dedupe_key=f"meta-backfill:{source_id}")
        audit(db, user, "meta.connection.connect", source_id)
        db.commit()
        return {"page": page, "job_id": job.id, "subscribed": True}
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.post("/sources/{source_id}/run", status_code=202)
def run_source(source_id: int, db: Session = Depends(session_scope), user: User = admin):
    try:
        connection(db, source_id)
        job = enqueue(db, "meta.backfill", {"source_id": source_id}, dedupe_key=f"meta-backfill:{source_id}")
        audit(db, user, "meta.source.run", source_id)
        db.commit()
        return {"job_id": job.id}
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get("/sources/{source_id}/forms")
def forms(source_id: int, db: Session = Depends(session_scope), user: User = admin):
    try:
        row, credentials = connection(db, source_id)
        with Meta(row["config"], credentials) as client:
            return {"items": list(client.pages(f'/{row["config"]["page_id"]}/leadgen_forms',
                                               {"fields": "id,name,status", "limit": 100}))}
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get("/sources/{source_id}/runs")
def runs(source_id: int, db: Session = Depends(session_scope), user: User = admin):
    source(db, source_id)
    items = [dict(r) for r in db.execute(text("""SELECT id,status,error,created_at FROM jobs
      WHERE type IN ('meta.backfill','meta.lead','ingestion.resolve') AND payload->>'source_id'=:s
      ORDER BY id DESC LIMIT 100"""), {"s": str(source_id)}).mappings()]
    counts = {r["status"]: r["n"] for r in db.execute(text("""SELECT status,count(*) AS n
      FROM meta_leads WHERE source_id=:s GROUP BY status"""), {"s": source_id}).mappings()}
    return {"items": items, "leads": {key: counts.get(key, 0) for key in ("queued", "completed", "suppressed")}}


class AudienceWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=200)


@router.post("/segments/{segment_id}/audiences", status_code=201)
def create_audience(segment_id: int, body: AudienceWrite, db: Session = Depends(session_scope), user: User = admin):
    from app.meta_ads import create_audience as create
    try:
        result = create(db, segment_id, body.source_id, body.name, user.id)
        db.add(AuditLog(user_id=user.id, actor_type="user", action="meta.audience.create", entity_type="segment",
                        entity_id=str(segment_id), details=result))
        db.commit()
        return result
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get("/segments/{segment_id}/results")
def results(segment_id: int, db: Session = Depends(session_scope), user: User = analyst):
    audiences = [dict(r) for r in db.execute(text("SELECT * FROM meta_audiences WHERE segment_id=:g ORDER BY id"),
                                            {"g": segment_id}).mappings()]
    rows = [dict(r) for r in db.execute(text("""SELECT r.* FROM meta_ad_results r
      JOIN meta_audiences a ON a.id=r.audience_id WHERE a.segment_id=:g
        AND r.date_start>=CURRENT_DATE-30 AND r.date_stop<CURRENT_DATE
      ORDER BY r.date_start DESC,r.audience_id,r.adset_id"""), {"g": segment_id}).mappings()]
    for item in rows:
        item["attribution_note"] = "Ad-set results; audiences may overlap or expand. Conversion: " + (item["conversion_action"] or "none")
    jobs = [dict(r) for r in db.execute(text("""SELECT id,status,error,created_at FROM jobs
      WHERE type='meta.results' AND payload->>'segment_id'=:g ORDER BY id DESC LIMIT 10"""),
      {"g": str(segment_id)}).mappings()]
    return {"audiences": audiences, "items": rows, "jobs": jobs}


@router.post("/segments/{segment_id}/results/refresh", status_code=202)
def refresh_results(segment_id: int, db: Session = Depends(session_scope), user: User = admin):
    if not db.scalar(text("SELECT EXISTS(SELECT 1 FROM meta_audiences WHERE segment_id=:g)"), {"g": segment_id}):
        raise HTTPException(422, "Create a Kinship-owned Meta audience first.")
    job = enqueue(db, "meta.results", {"segment_id": segment_id}, dedupe_key=f"meta-results:{segment_id}")
    db.add(AuditLog(user_id=user.id, actor_type="user", action="meta.results.refresh", entity_type="segment",
                    entity_id=str(segment_id), details={}))
    db.commit()
    return {"job_id": job.id}


async def limited_body(request, limit):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "Meta request exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


@public_router.get("/webhook/{source_id}", response_class=PlainTextResponse)
def challenge(source_id: int, request: Request, db: Session = Depends(session_scope)):
    try:
        _, credentials = connection(db, source_id)
    except ValueError:
        raise HTTPException(404, "Webhook not configured") from None
    if (request.query_params.get("hub.mode") != "subscribe" or not hmac.compare_digest(
            request.query_params.get("hub.verify_token", ""), credentials["verify_token"])):
        raise HTTPException(403, "Invalid webhook verification")
    challenge = request.query_params.get("hub.challenge", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", challenge):
        raise HTTPException(422, "Invalid webhook challenge")
    return challenge


@public_router.post("/webhook/{source_id}")
async def webhook(source_id: int, request: Request, db: Session = Depends(session_scope)):
    try:
        row, credentials = connection(db, source_id)
    except ValueError:
        raise HTTPException(404, "Webhook not configured") from None
    raw = await limited_body(request, 2 * 1024 * 1024)
    expected = "sha256=" + hmac.new(credentials["app_secret"].encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("x-hub-signature-256", ""), expected):
        raise HTTPException(401, "Invalid Meta signature")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("object") != "page":
            raise ValueError()
        for entry in data.get("entry", []):
            if str(entry.get("id")) != row["config"]["page_id"]:
                continue
            for change in entry.get("changes", []):
                if change.get("field") == "leadgen":
                    value = change["value"]
                    if str(value.get("page_id", entry["id"])) != row["config"]["page_id"]:
                        continue
                    queue_lead(db, source_id, str(value["leadgen_id"]), str(value["form_id"]))
            # Optional Messenger user_email/user_phone quick replies. Nothing
            # links on a PSID alone or on arbitrary message text.
            for message in entry.get("messaging", []):
                if str(message.get("recipient", {}).get("id")) != row["config"]["page_id"]:
                    continue
                content = message.get("message", {})
                if not content.get("quick_reply") or not content.get("mid"):
                    continue
                from app.identity.normalize import normalize_email, normalize_phone
                provided = str(content.get("text", ""))
                email, phone = normalize_email(provided), normalize_phone(provided)
                psid = str(message.get("sender", {}).get("id", ""))
                if not (email or phone) or not psid.isdigit():
                    continue
                contact(db, source_id, "messenger:" + row["config"]["page_id"] + ":" + psid,
                        {"email": email, "phone": phone},
                        {"capture_source": "meta_messenger", "meta_form_name": "Messenger contact quick reply",
                         "source_name": row["source_name"]},
                        psid=row["config"]["page_id"] + ":" + psid)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(422, "Malformed Meta callback") from None
    db.commit()
    return {"received": True}


def digest_message(value):
    from app.privacy.service import digest
    return digest(str(value))


def verified_facebook_profile(config, credentials, token):
    with Meta(config, credentials) as client:
        info = client.request("GET", "/debug_token", {"input_token": token},
                              token=config["app_id"] + "|" + credentials["app_secret"])["data"]
        if (not info.get("is_valid") or str(info.get("app_id")) != config["app_id"]
                or info.get("type") != "USER" or not str(info.get("user_id", "")).isdigit()
                or any(info.get(k, 0) and info[k] <= time.time()
                       for k in ("expires_at", "data_access_expires_at"))):
            raise ValueError("Invalid Facebook Login token")
        user = client.request("GET", "/me", {"fields": "id,email,first_name,last_name"}, token=token)
        if str(user.get("id")) != str(info["user_id"]):
            raise ValueError("Facebook Login user mismatch")
        return user


@public_router.options("/login/{source_id}")
def login_preflight(source_id: int, request: Request):
    from app.ingestion.api import cors
    return Response(status_code=204, headers={**cors(request),
        "Access-Control-Allow-Methods": "POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"})


@public_router.post("/login/{source_id}")
async def facebook_login(source_id: int, request: Request, db: Session = Depends(session_scope)):
    """A token is verified with Facebook; arbitrary app IDs in traits are ignored."""
    raw = await limited_body(request, 16384)
    from app.ingestion.api import cors, allow
    headers = cors(request)
    if not allow(f"meta-login:{source_id}:{request.client.host if request.client else 'unknown'}"):
        raise HTTPException(429, "Facebook Login capture rate limit exceeded", headers=headers)
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) - {"access_token"}:
            raise ValueError()
        token = body["access_token"]
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ValueError()
        row, credentials = connection(db, source_id)
        config = row["config"]
        user = await run_in_threadpool(verified_facebook_profile, config, credentials, token)
        app_user_id = config["app_id"] + ":" + str(user["id"])
        rid = contact(db, source_id, "facebook-login:" + app_user_id,
                      {k: user.get(k) for k in ("email", "first_name", "last_name")},
                      {"capture_source": "facebook_login", "source_name": row["source_name"]},
                      app_user_id=app_user_id)
        db.commit()
        return JSONResponse({"accepted": rid is not None}, headers=headers)
    except (ValueError, KeyError, TypeError):
        # Do not echo an access token, Graph response, or validation body.
        raise HTTPException(401, "Facebook Login could not be verified", headers=headers) from None
