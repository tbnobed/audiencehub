"""Privacy-safe, bounded render diagnostics. No raw browser text is persisted."""
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.auth.deps import current_user, require_role
from app.db import session_scope
from app.models import ClientError, User

router = APIRouter()
COMPONENTS = frozenset(("App", "ErrorBoundary", "ProfileDetail", "ProfileSectionBoundary",
                       "Overview", "System", "Card", "CardContent", "Table", "TableBody",
                       "TableRow", "TableCell"))
ROUTES = frozenset(("/", "/profiles", "/profiles/:id", "/system", "/sources",
                    "/imports", "/settings", "/data-health", "/unknown"))
CATEGORIES = frozenset(("TypeError", "RangeError", "ReferenceError", "SyntaxError", "RenderError"))
MAX_BODY = 4096
MAX_ROWS = 1000
RETENTION_DAYS = 7


def sanitize(body):
    if not isinstance(body, dict) or set(body) - {"category", "message", "component_stack", "route", "profile_id"}:
        raise HTTPException(422, "Invalid client error report")
    category = body.get("category", "RenderError")
    route = body.get("route", "/unknown")
    stack = body.get("component_stack", [])
    profile = body.get("profile_id")
    if (not isinstance(category, str) or not isinstance(route, str)
            or not isinstance(stack, list) or len(stack) > 16
            or any(not isinstance(item, str) or len(item) > 128 for item in stack)
            or (profile is not None and (type(profile) is not int or not 0 < profile <= 999999999999999))):
        raise HTTPException(422, "Invalid client error report")
    # Discard supplied message entirely, including apparently innocuous names.
    route = re.split(r"[?#]", route, maxsplit=1)[0]
    return {
        "category": category if category in CATEGORIES else "RenderError",
        "message": "A component failed to render.",
        "route": route if route in ROUTES or re.fullmatch(r"/profiles/[0-9]{1,15}", route) else "/unknown",
        "component_stack": [item for item in stack if item in COMPONENTS],
        "profile_id": profile,
    }


def prune(db):
    db.execute(delete(ClientError).where(
        ClientError.created_at < datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)))


@router.post("/api/client-errors", status_code=202)
async def report(request: Request, user: User = Depends(current_user),
                 db: Session = Depends(session_scope)):
    # Stream cap also covers absent/forged Content-Length. Never echo input.
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "Client error report too large")
        raw.extend(chunk)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "Invalid client error report") from None
    values = sanitize(body)
    # Cross-worker rate limits; bounded storage, no IP/session/cookie retained.
    db.execute(text("SELECT pg_advisory_xact_lock(22822001)"))
    prune(db)
    recent = ClientError.created_at >= datetime.now(timezone.utc) - timedelta(minutes=1)
    total = db.scalar(select(func.count()).select_from(ClientError).where(recent))
    own = db.scalar(select(func.count()).select_from(ClientError).where(recent, ClientError.user_id == user.id))
    if total >= 100 or own >= 10:
        db.commit()
        raise HTTPException(429, "Client error reporting limit reached")
    db.add(ClientError(user_id=user.id, **values))
    db.flush()
    keep = select(ClientError.id).order_by(ClientError.id.desc()).limit(MAX_ROWS)
    db.execute(delete(ClientError).where(ClientError.id.not_in(keep)))
    db.commit()
    return {"accepted": True}


@router.get("/api/admin/client-errors")
def recent_errors(user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    prune(db)
    items = db.scalars(select(ClientError).order_by(ClientError.id.desc()).limit(100)).all()
    result = [{"id": row.id, "created_at": row.created_at.isoformat(),
               "category": row.category, "message": row.message, "route": row.route,
               "component_stack": row.component_stack, "profile_id": row.profile_id} for row in items]
    db.commit()
    return {"items": result, "retention_days": RETENTION_DAYS, "max_rows": MAX_ROWS}