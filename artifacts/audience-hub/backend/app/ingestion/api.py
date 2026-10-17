import base64
import hashlib
import json
import threading
import time
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import get_settings
from app.db import session_scope
from app.models import Source
from app.ingestion.service import ingest
from app.ingestion.validation import BATCH_LIMIT, MESSAGE_LIMIT, validate

router = APIRouter(prefix="/v1", tags=["ingestion"])
_buckets = {}
_lock = threading.Lock()


def allow(key, now=None):
    now = time.monotonic() if now is None else now
    with _lock:
        tokens, previous = _buckets.get(key, (200.0, now))
        tokens = min(200.0, tokens + max(0, now - previous) * 50)
        permitted = tokens >= 1
        _buckets[key] = (tokens - 1 if permitted else tokens, now)
        if len(_buckets) > 4096:
            for old in [k for k, (_, at) in _buckets.items() if now - at > 60]:
                del _buckets[old]
        return permitted


def cors(request):
    origin = request.headers.get("origin")
    allowed = {s.strip() for s in get_settings().ingest_cors_origins.split(",") if s.strip() and s.strip() != "*"}
    if origin and origin not in allowed:
        raise HTTPException(403, "Origin is not allowed")
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if origin else {}


def authenticate(request, db):
    key = request.headers.get("x-write-key")
    authorization = request.headers.get("authorization")
    if authorization:
        try:
            scheme, encoded = authorization.split(" ", 1)
            decoded = base64.b64decode(encoded, validate=True).decode()
            if scheme.lower() != "basic" or not decoded.endswith(":"):
                raise ValueError()
            basic = decoded[:-1]
            if key and key != basic:
                raise ValueError()
            key = basic
        except (ValueError, UnicodeError):
            raise HTTPException(401, "Invalid write key") from None
    # Beacon query credentials are deliberately unsupported: proxy/access logs
    # commonly retain query strings. SDK uses a write_key field in batch bodies.
    if not key:
        raise HTTPException(401, "Write key required")
    if len(key) > 256:
        raise HTTPException(401, "Invalid write key")
    hashed = hashlib.sha256(key.encode()).hexdigest()
    source = db.scalar(select(Source).where(Source.write_key_hash == hashed,
                         Source.kind == "event_api", Source.is_active.is_(True)).with_for_update(read=True))
    if source is None or "event" not in source.record_types:
        raise HTTPException(401, "Invalid write key")
    if not allow(hashed):
        raise HTTPException(429, "Write key rate limit exceeded")
    return source


@router.options("/{kind}")
def preflight(kind: str, request: Request):
    headers = cors(request)
    headers.update({"Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, X-Write-Key, Content-Type",
                    "Access-Control-Max-Age": "600"})
    return Response(status_code=204, headers=headers)


@router.post("/{kind}", status_code=202)
async def receive(kind: str, request: Request, db: Session = Depends(session_scope)):
    headers = cors(request)
    try:
        if kind not in {"track", "identify", "page", "batch"}:
            raise HTTPException(404, "Unknown ingestion endpoint")
        limit = BATCH_LIMIT if kind == "batch" else MESSAGE_LIMIT
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise HTTPException(413, "Request body exceeds size limit")
            body.extend(chunk)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid JSON body") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "Request body must be an object")
        # A body key supports sendBeacon without placing credentials in URLs.
        if kind == "batch" and "write_key" in data and not request.headers.get("x-write-key") and not request.headers.get("authorization"):
            from starlette.datastructures import Headers
            request._headers = Headers({**dict(request.headers), "x-write-key": str(data.pop("write_key"))})
        source = authenticate(request, db)
        items = data.get("batch") if kind == "batch" else [data]
        if not isinstance(items, list) or not items or len(items) > 500:
            raise HTTPException(400, "batch must contain between 1 and 500 messages")
        valid, rejected = [], []
        for index, item in enumerate(items):
            try:
                valid.append((index, validate(item, None if kind == "batch" else kind,
                    ip=request.client.host if request.client else None)))
            except (ValueError, RecursionError) as exc:
                # Validator errors contain only static rules, never input.
                reason = "Message is too deeply nested" if isinstance(exc, RecursionError) else str(exc)
                rejected.append({"index": index, "reason": reason})
        result = ingest(db, source, valid)
        result["rejected"] = sorted(result["rejected"] + rejected, key=lambda r: r["index"])
        db.commit()
        return JSONResponse(result, status_code=202, headers=headers)
    except HTTPException as exc:
        db.rollback()
        return JSONResponse({"error": {"code": f"http_{exc.status_code}", "message": exc.detail}},
                            status_code=exc.status_code, headers=headers)
