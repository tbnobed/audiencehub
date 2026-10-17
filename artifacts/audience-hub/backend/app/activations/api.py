"""Admin destinations, analyst activation management and audited exports."""
import os
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.auth.deps import require_role
from app.db import session_scope
from app.models import Activation, ActivationRun, Destination, Segment, User
from app.activations import fields, schedule, security, service

router = APIRouter(prefix="/api", tags=["activations"])
TYPES = {"csv", "google_customer_match", "meta_custom_audience", "webhook"}


class DestinationWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    type: str
    config: dict = Field(default_factory=dict)
    is_active: bool = True
    signing_secret: str | None = Field(default=None, max_length=4096)
    headers: dict[str, str] | None = None


class DestinationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: str | None = None
    config: dict | None = None
    is_active: bool | None = None
    signing_secret: str | None = Field(default=None, max_length=4096)
    headers: dict[str, str] | None = None


class ActivationWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    segment_id: int = Field(gt=0)
    destination_id: int = Field(gt=0)
    field_mapping: dict = Field(default_factory=dict)
    required_consent: str | None = None
    schedule: str | None = Field(default=None, max_length=100)
    timezone: str = Field(default="America/Chicago", max_length=100)
    is_active: bool = True
    consent_override: bool = False
    override_note: str | None = Field(default=None, max_length=500)


class ActivationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    segment_id: int | None = Field(default=None, gt=0)
    destination_id: int | None = Field(default=None, gt=0)
    field_mapping: dict | None = None
    required_consent: str | None = None
    schedule: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None
    consent_override: bool = False
    override_note: str | None = Field(default=None, max_length=500)


def require(db, model, identifier):
    row = db.get(model, identifier)
    if row is None:
        raise HTTPException(404, "Requested item was not found.")
    return row


def destination_json(row):
    secrets = security.decrypt(row.secret_encrypted)
    secret = secrets.get("signing_secret", "")
    return {"id": row.id, "name": row.name, "type": row.type, "config": row.config,
            "is_active": row.is_active, "has_secret": bool(secret),
            "secret_last4": secret[-4:] if secret else None,
            "header_names": sorted(secrets.get("headers", {})), "created_at": row.created_at}


def run_json(row):
    result = {name: getattr(row, name) for name in (
        "id", "activation_id", "status", "started_at", "finished_at", "created_at",
        "profiles_selected", "profiles_excluded_consent", "profiles_excluded_suppressed",
        "profiles_excluded_missing_fields", "profiles_sent", "error", "triggered_by")}
    try:
        service.safe_file(row)
        downloadable = True
    except ValueError:
        downloadable = False
    result.update(downloadable=downloadable, revoked=row.revoked_at is not None)
    # The public run contract has four states. A cancelled delivery is a failed
    # run with its explicit cancellation/revocation reason, never a success.
    if result["status"] == "cancelled":
        result["status"] = "failed"
    return result


def activation_json(db, row):
    segment, destination = db.get(Segment, row.segment_id), db.get(Destination, row.destination_id)
    last = db.scalar(select(ActivationRun).where(ActivationRun.activation_id == row.id).order_by(ActivationRun.id.desc()).limit(1))
    result = {name: getattr(row, name) for name in (
        "id", "name", "segment_id", "destination_id", "field_mapping", "required_consent",
        "schedule", "timezone", "is_active", "created_by")}
    result.update(segment_name=segment.name if segment else "Unavailable audience",
                  destination_name=destination.name if destination else "Unavailable destination",
                  destination_type=destination.type if destination else None,
                  last_run=run_json(last) if last else None,
                  next_run_at=schedule.next_run(row.schedule, row.timezone) if row.is_active else None)
    result["field_mapping"] = service.ordered_mapping(row)
    return result


def validate_destination(kind, config, secrets):
    if kind not in TYPES:
        raise ValueError("Unsupported destination type.")
    if set(config) - {"delimiter", "include_pii", "url", "batch_size", "mode", "allow_internal_network"}:
        raise ValueError("Unsupported destination configuration.")
    for key in ("include_pii", "allow_internal_network"):
        if key in config and not isinstance(config[key], bool):
            raise ValueError("Destination flags must be boolean.")
    if config.get("delimiter", ",") not in (",", ";", "\t", "|"):
        raise ValueError("Choose comma, semicolon, tab or pipe as the delimiter.")
    if kind in service.ADS and config.get("include_pii") is False:
        raise ValueError("Ad audience files require consented partner identifiers.")
    if kind == "webhook":
        size = config.get("batch_size", 500)
        if type(size) is not int or not 1 <= size <= 1000:
            raise ValueError("Webhook batches must contain between one and 1,000 partners.")
        if config.get("mode", "full") not in ("full", "delta"):
            raise ValueError("Choose full or delta delivery.")
        security.target(config.get("url", ""), config.get("allow_internal_network", False))
        if not isinstance(secrets.get("signing_secret"), str) or len(secrets["signing_secret"]) < 16:
            raise ValueError("Webhook signing secrets must have at least 16 characters.")
        security.validate_headers(secrets.get("headers", {}))
    elif secrets:
        raise ValueError("File destinations do not accept webhook credentials.")


def save_destination(db, row, body, user):
    values = body.model_dump(exclude_unset=True)
    secrets = security.decrypt(row.secret_encrypted) if row else {}
    kind = values.get("type", row.type if row else None)
    config = values.get("config", row.config if row else {})
    if row is not None and kind != row.type:
        # A CSV→ad switch must not silently inherit an activation's absent
        # consent requirement. Select a new destination through the wizard,
        # where ad defaults and explicit overrides are validated and audited.
        raise HTTPException(422, "Destination type cannot change; create a new destination and update the activation.")
    if not isinstance(kind, str) or not isinstance(config, dict):
        raise HTTPException(422, "Destination type and configuration cannot be null.")
    if "signing_secret" in values:
        if values["signing_secret"] is None:
            secrets.pop("signing_secret", None)
        else:
            secrets["signing_secret"] = values["signing_secret"]
    if "headers" in values:
        secrets["headers"] = values["headers"] or {}
    try:
        validate_destination(kind, config, secrets)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if row is None:
        row = Destination(name=body.name, type=kind, config=config)
        db.add(row)
    for key in ("name", "is_active"):
        if key in values:
            if values[key] is None:
                raise HTTPException(422, "Destination fields cannot be null.")
            setattr(row, key, values[key])
    row.type, row.config = kind, config
    row.secret_encrypted = security.encrypt(secrets) if secrets else None
    db.flush()
    service.audit(db, "destination.save", "destination", row.id, user.id,
                  allow_internal_network=config.get("allow_internal_network", False),
                  destination_type=kind)
    db.commit()
    return destination_json(row)


@router.get("/destinations")
def destinations(db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    return {"items": [destination_json(row) for row in db.scalars(select(Destination).order_by(Destination.id))]}


@router.post("/destinations")
def create_destination(body: DestinationWrite, db: Session = Depends(session_scope), user: User = Depends(require_role("admin"))):
    return save_destination(db, None, body, user)


@router.get("/destinations/{identifier}")
def get_destination(identifier: int, db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    return destination_json(require(db, Destination, identifier))


@router.patch("/destinations/{identifier}")
def update_destination(identifier: int, body: DestinationPatch, db: Session = Depends(session_scope), user: User = Depends(require_role("admin"))):
    return save_destination(db, require(db, Destination, identifier), body, user)


@router.delete("/destinations/{identifier}")
def archive_destination(identifier: int, db: Session = Depends(session_scope), user: User = Depends(require_role("admin"))):
    row = require(db, Destination, identifier)
    row.is_active = False
    service.audit(db, "destination.archive", "destination", identifier, user.id)
    db.commit()
    return {"archived": True}


@router.post("/destinations/{identifier}/test")
def test_destination(identifier: int, db: Session = Depends(session_scope), user: User = Depends(require_role("admin"))):
    row = require(db, Destination, identifier)
    if row.type != "webhook" or not row.is_active:
        raise HTTPException(422, "Only active webhook destinations can receive test pings.")
    secrets = security.decrypt(row.secret_encrypted)
    try:
        security.deliver(row.config["url"], {"type": "kinship.destination.test", "destination_id": row.id},
                         secrets["signing_secret"], secrets.get("headers", {}),
                         f"test-{row.id}-{datetime.now(timezone.utc).timestamp()}", 1,
                         row.config.get("allow_internal_network", False))
    except security.DeliveryError as error:
        raise HTTPException(422, str(error)) from None
    service.audit(db, "destination.test", "destination", row.id, user.id)
    db.commit()
    return {"delivered": True}


@router.get("/activations/fields")
def export_fields(db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    return {"fields": fields.registry(db)}


@router.get("/activations")
def activations(db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    return {"items": [activation_json(db, row) for row in db.scalars(select(Activation).order_by(Activation.id.desc()))]}


def save_activation(db, row, body, user):
    values = body.model_dump(exclude_unset=True)
    creating = row is None
    prior_consent = row.required_consent if row is not None else None
    prior_destination = row.destination_id if row is not None else None
    if creating:
        row = Activation(name=body.name, segment_id=body.segment_id, destination_id=body.destination_id,
                         timezone="America/Chicago", is_active=True, field_mapping={}, created_by=user.id)
    for key in ("name", "segment_id", "destination_id", "timezone", "is_active", "field_mapping"):
        if key in values:
            if values[key] is None:
                raise HTTPException(422, "Activation fields cannot be null.")
            setattr(row, key, values[key])
    segment, destination = require(db, Segment, row.segment_id), require(db, Destination, row.destination_id)
    if segment.status != "active" or not destination.is_active:
        raise HTTPException(422, "Choose an active audience and destination.")
    if "required_consent" in values:
        row.required_consent = values["required_consent"]
    elif creating or (row.required_consent is None and "destination_id" in values):
        row.required_consent = "ads_personalization" if destination.type in service.ADS else None
    if row.required_consent is not None and row.required_consent not in service.CHANNELS:
        raise HTTPException(422, "Unknown consent channel.")
    override = destination.type in service.ADS and row.required_consent != "ads_personalization"
    unchanged_override = override and not creating and prior_consent == row.required_consent and prior_destination == row.destination_id
    preserved_override = unchanged_override and bool(db.scalar(text("""SELECT EXISTS(
      SELECT 1 FROM audit_log WHERE entity_type='activation' AND entity_id=:id
      AND action='activation.consent_override'
      AND details->>'required_consent' IS NOT DISTINCT FROM CAST(:consent AS text))"""),
      {"id": str(row.id), "consent": row.required_consent}))
    if override and not preserved_override and (user.role != "admin" or not values.get("consent_override") or not (values.get("override_note") or "").strip()):
        raise HTTPException(403, "Ad audiences require ads personalization consent. An admin must explicitly override it with a note.")
    if "schedule" in values:
        row.schedule = values["schedule"] or None
    if "field_mapping" in values:
        try:
            service.validate_provider_mapping(destination.type, row.field_mapping)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    if not row.field_mapping:
        row.field_mapping = service.preset(destination.type)
    if creating or "field_mapping" in values:
        row.column_order = list(row.field_mapping)
    try:
        schedule.validate(row.schedule, row.timezone)
        fields.validate_mapping(db, row.field_mapping, destination.config.get("include_pii", True))
        service.validate_provider_mapping(destination.type, row.field_mapping)
        if destination.type in service.ADS:
            allowed = set(service.GOOGLE_MAP if destination.type == "google_customer_match" else service.META_MAP)
            if set(row.field_mapping) - allowed:
                raise ValueError("Ad audience columns must use the provider template headers.")
            if any(value["kind"] == "constant" for value in row.field_mapping.values()):
                raise ValueError("Ad audience identity fields cannot be constants.")
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if creating:
        db.add(row)
    db.flush()
    if override and not preserved_override:
        # Do not put free-form notes (which can contain PII) in the audit log.
        service.audit(db, "activation.consent_override", "activation", row.id, user.id,
                      required_consent=row.required_consent, note_provided=True)
    service.audit(db, "activation.save", "activation", row.id, user.id)
    db.commit()
    return activation_json(db, row)


@router.post("/activations")
def create_activation(body: ActivationWrite, db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    return save_activation(db, None, body, user)


@router.get("/activations/{identifier}")
def get_activation(identifier: int, db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    return activation_json(db, require(db, Activation, identifier))


@router.patch("/activations/{identifier}")
def update_activation(identifier: int, body: ActivationPatch, db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    return save_activation(db, require(db, Activation, identifier), body, user)


@router.delete("/activations/{identifier}")
def archive_activation(identifier: int, db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    row = require(db, Activation, identifier)
    row.is_active = False
    service.audit(db, "activation.archive", "activation", row.id, user.id)
    db.commit()
    return {"archived": True}


@router.post("/activations/{identifier}/run")
def start_run(identifier: int, db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    try:
        run, job = service.enqueue_run(db, identifier, user.id)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    service.audit(db, "activation.queued", "activation", identifier, user.id, run_id=run.id)
    db.commit()
    return {"run_id": run.id, "job_id": job.id}


@router.get("/activations/{identifier}/runs")
def runs(identifier: int, db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    require(db, Activation, identifier)
    return {"items": [run_json(row) for row in db.scalars(select(ActivationRun).where(
        ActivationRun.activation_id == identifier).order_by(ActivationRun.id.desc()).limit(100))]}


@router.get("/activation-runs/{run_id}/download")
def download(run_id: int, db: Session = Depends(session_scope), user: User = Depends(require_role("analyst"))):
    from app.privacy.locking import privacy_shared
    # Dedicated session is held through stream completion, independently of the
    # FastAPI dependency teardown timing. Deletion cannot remove an open export.
    download_db = Session(db.get_bind())
    file = None
    try:
        privacy_shared(download_db)
        row = require(download_db, ActivationRun, run_id)
        path = service.safe_file(row)
        file = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb")
        service.audit(db, "export.download", "activation_run", run_id, user.id)
        db.commit()
    except (ValueError, OSError):
        if file:
            file.close()
        download_db.close()
        raise HTTPException(410, "Export is unavailable, expired or revoked.") from None
    except BaseException:
        if file:
            file.close()
        download_db.close()
        raise
    def close():
        file.close()
        download_db.close()
    def stream():
        try:
            for block in iter(lambda: file.read(65536), b""):
                yield block
        finally:
            close()
    return StreamingResponse(stream(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{path.name}"', "Cache-Control": "no-store"},
        background=BackgroundTask(close))
