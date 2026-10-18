"""Admin-only source configuration; credentials are write-only encrypted blobs."""
import json
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.activations import security
from app.auth.deps import require_role
from app.db import session_scope
from app.jobs.queue import enqueue
from app.models import AuditLog, Source, User
from app.source_connectors import DEFAULT_SCHEDULES, FEEDS, SourceError, validate_config, validate_credentials
from app.source_pulls import collect, connection_row

router = APIRouter()


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict
    credentials: dict | None = Field(default=None, repr=False)


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feed: str | None = None
    mapping_id: str | None = None


class ConnectionTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credentials: dict | None = Field(default=None, repr=False)


def credentials_for(db, source_id, supplied=None):
    if supplied is not None:
        validate_credentials("snowflake", supplied)
        return supplied
    encrypted = db.scalar(text("SELECT credentials FROM source_connections WHERE source_id=:id"), {"id": source_id})
    if encrypted is None:
        raise SourceError("Save the source credentials first.")
    return security.decrypt(encrypted)


@router.post("/{source_id}/test")
def test_connection(source_id: int, data: ConnectionTest = ConnectionTest(),
                    user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    item = source(db, source_id)
    if item.kind != "snowflake":
        raise HTTPException(422, "Connection testing here requires a Snowflake source")
    from app.source_connectors import snowflake_connection, identifier
    try:
        credentials = credentials_for(db, source_id, data.credentials)
        with snowflake_connection(credentials) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT CURRENT_VERSION()")
            version = str(cursor.fetchone()[0])
            cursor.execute("SELECT TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME FROM IDENTIFIER(%s) "
                           "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME",
                           (identifier(credentials["database"] + ".INFORMATION_SCHEMA.TABLES"),
                            credentials["schema"].upper()))
            tables = []
            while rows := cursor.fetchmany(1000):
                tables.extend(".".join(map(str, row)) for row in rows)
        return {"version": version, "tables": tables}
    except SourceError as error:
        raise HTTPException(422, str(error)) from None
    except Exception:
        raise HTTPException(422, "Connection test failed; check saved credentials and read-only grants") from None


@router.get("/{source_id}/columns")
def table_columns(source_id: int, table: str = Query(max_length=300),
                  user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    item = source(db, source_id)
    if item.kind != "snowflake":
        raise HTTPException(422, "Only Snowflake sources support table introspection")
    from app.source_connectors import snowflake_connection, identifier
    from app.source_rows import matched_columns
    try:
        with snowflake_connection(credentials_for(db, source_id)) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM IDENTIFIER(%s) LIMIT 0", (identifier(table),))
            columns = [field[0].lower() for field in cursor.description]
        return {"columns": columns, "suggested_columns": matched_columns(columns)}
    except SourceError as error:
        raise HTTPException(422, str(error)) from None


def source(db, source_id):
    item = db.get(Source, source_id)
    if item is None:
        raise HTTPException(404, "Source not found")
    if item.kind not in {"sftp_jsonl", "snowflake", "zeta_exit"}:
        raise HTTPException(422, "Source is not a pull connection")
    return item


@router.get("/{source_id}/connection")
def get_connection(source_id: int, user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    source(db, source_id)
    row = db.execute(text("SELECT config,state FROM source_connections WHERE source_id=:id"),
                     {"id": source_id}).mappings().first()
    connection_fields, credentials = {}, {}
    if row:
        credentials = credentials_for(db, source_id)
        connection_fields = {key: credentials[key] for key in
                             ("account", "user", "role", "warehouse", "database", "schema") if key in credentials}
    return {"configured": bool(row), "has_credentials": bool(row and credentials), "connection_fields": connection_fields,
            "config": row["config"] if row else {"mappings": [], "schedules": {}},
            "default_schedules": DEFAULT_SCHEDULES,
            "recovery_pending": bool(row and row["state"].get("pending"))}


@router.put("/{source_id}/connection")
def put_connection(source_id: int, data: ConnectionUpdate,
                   user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    item = source(db, source_id)
    # Cross-process lock shared with pulls; config cannot race a running pull.
    from app.source_pulls import source_lock
    from app.identity.locking import IdentityResolutionDeferred
    try:
        with source_lock(source_id):
            old = db.execute(text("SELECT credentials,state,config FROM source_connections WHERE source_id=:id"),
                             {"id": source_id}).mappings().first()
            if old and old["state"].get("pending"):
                raise HTTPException(409, "Recover the pending pull before changing its configuration")
            validate_config(item.kind, data.config)
            if data.credentials is None:
                if not old:
                    raise SourceError("Credentials are required for a new connection.")
                encrypted = old["credentials"]
            else:
                validate_credentials(item.kind, data.credentials)
                encrypted = security.encrypt(data.credentials)
            state = dict(old["state"]) if old else {}
            previous = {m["id"]: {k: v for k, v in m.items() if k != "schedule"}
                        for m in old["config"]["mappings"]} if old else {}
            current = {m["id"]: {k: v for k, v in m.items() if k != "schedule"}
                       for m in data.config["mappings"]}
            state["cursors"] = {key: value for key, value in state.get("cursors", {}).items()
                                if previous.get(key) == current.get(key) and key in current}
            db.execute(text("""INSERT INTO source_connections(source_id,config,credentials)
                VALUES (:id,CAST(:config AS jsonb),:credentials)
                ON CONFLICT(source_id) DO UPDATE SET config=EXCLUDED.config,credentials=EXCLUDED.credentials,
                state=CAST(:state AS jsonb),updated_at=now()"""),
                       {"id": source_id, "config": json.dumps(data.config), "credentials": encrypted,
                        "state": json.dumps(state)})
            db.add(AuditLog(user_id=user.id, actor_type="user", action="source.configure",
                            entity_type="source", entity_id=str(source_id), details={}))
            db.commit()
    except IdentityResolutionDeferred:
        raise HTTPException(409, "Source is currently running") from None
    except ValueError:
        raise HTTPException(422, "Invalid source configuration or credentials") from None
    return get_connection(source_id, user, db)


@router.post("/{source_id}/dry-run")
def dry_run(source_id: int, request: PullRequest = PullRequest(),
            user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    source(db, source_id)
    if request.feed is not None and request.feed not in FEEDS:
        raise HTTPException(422, "Unsupported feed")
    try:
        return {"limit_per_mapping": 100, "items": collect(connection_row(db, source_id), request.feed,
                                                          dry_run=True, mapping_id=request.mapping_id)}
    except SourceError as error:
        raise HTTPException(422, str(error)) from None
    except Exception:
        # Connector exception messages can include usernames, key fragments or
        # result data. Never expose them in HTTP responses.
        raise HTTPException(422, "Source dry-run failed; verify connectivity, configuration and canonical fields") from None


@router.post("/{source_id}/pull", status_code=202)
def pull(source_id: int, request: PullRequest = PullRequest(),
         user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    source(db, source_id)
    if request.feed is not None and request.feed not in FEEDS:
        raise HTTPException(422, "Unsupported feed")
    try:
        connection_row(db, source_id)
    except SourceError:
        raise HTTPException(422, "Configure and activate this source before pulling") from None
    row = connection_row(db, source_id)
    if not row["config"]["mappings"] and not row["state"].get("pending"):
        raise HTTPException(422, "Configure at least one mapping before running this source")
    if request.mapping_id and request.mapping_id not in {m["id"] for m in row["config"]["mappings"]}:
        raise HTTPException(422, "Mapping not found")
    job = enqueue(db, "source.pull", {"source_id": source_id, "feed": request.feed, "mapping_id": request.mapping_id},
                  dedupe_key=f"source:{source_id}:{request.mapping_id or request.feed or 'all'}")
    db.add(AuditLog(user_id=user.id, actor_type="user", action="source.pull",
                   entity_type="source", entity_id=str(source_id), details={"job_id": job.id}))
    db.commit()
    return {"job_id": job.id, "status": job.status}


@router.get("/{source_id}/pulls")
def pull_history(source_id: int, limit: int = Query(50, ge=1, le=50),
                 user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    source(db, source_id)
    rows = db.execute(text("""WITH recent AS (
        SELECT id,status,attempts,max_attempts,created_at,run_after,started_at,finished_at,heartbeat_at,
               payload->>'feed' AS feed,progress->>'batch_id' AS batch_id,error IS NOT NULL AS has_error
        FROM jobs WHERE type='source.pull' AND payload->>'source_id'=:source_id
        ORDER BY id DESC LIMIT :limit
      )
      SELECT j.*,counts.imports_total,counts.imports_completed,counts.rows_total,counts.rows_ok,counts.rows_rejected
      FROM recent j LEFT JOIN LATERAL (
        SELECT count(*) AS imports_total,
               count(*) FILTER (WHERE i.status='completed') AS imports_completed,
               COALESCE(sum(i.rows_total),0) AS rows_total,
               COALESCE(sum(i.rows_ok),0) AS rows_ok,
               COALESCE(sum(i.rows_rejected),0) AS rows_rejected
        FROM imports i WHERE i.identity_batch_id::text=j.batch_id AND i.source_id=:id
      ) counts ON true ORDER BY j.id DESC"""),
                      {"source_id": str(source_id), "id": source_id, "limit": limit}).mappings()
    return {"items": [pull_history_item(row) for row in rows]}


def pull_history_item(row):
    """Allowlist only operational fields. Never return job error/payload/progress."""
    item = {"job_id": row["id"], "status": row["status"],
            "feed": row["feed"] if row["feed"] in FEEDS else None,
            "attempts": row["attempts"], "max_attempts": row["max_attempts"],
            "counts": {key: int(row[key]) for key in
                       ("imports_total", "imports_completed", "rows_total", "rows_ok", "rows_rejected")},
            "error": "Source pull failed. Check configuration and retry to resume committed checkpoints."
                     if row["has_error"] else None}
    for key in ("created_at", "run_after", "started_at", "finished_at", "heartbeat_at"):
        item[key] = row[key].isoformat() if row[key] else None
    return item
