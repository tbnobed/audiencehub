from contextlib import asynccontextmanager
from pathlib import Path
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from app.auth import dev, oidc
from app.auth.deps import check_csrf, current_user, require_role
from app.config import get_settings
from app.db import engine, session_scope
from app.jobs import queue
from app.logging_config import configure_logging
from app.models import AuditLog, Job, ScheduledRun, User
from app.sources import router as sources_router
from app.imports.api import router as imports_router
from app.profiles.api import router as profiles_router
from app.dashboards.api import router as dashboards_router
from app.settings.api import router as settings_router
from app.client_errors import router as client_errors_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()  # Fail closed before accepting requests.
    configure_logging(settings.log_level)
    oidc.configure_oidc()
    queue.startup_selfcheck(engine)
    yield


settings = get_settings()
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(dev.router)
app.include_router(oidc.router)
app.include_router(sources_router)
app.include_router(imports_router)
app.include_router(profiles_router)
app.include_router(dashboards_router)
app.include_router(settings_router)
app.include_router(client_errors_router)


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException):
    return JSONResponse({"error": {"code": f"http_{error.status_code}", "message": str(error.detail)}},
                        status_code=error.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    return JSONResponse({"error": {"code": "validation_error", "message": "Invalid request",
                                   "details": {"fields": [list(item["loc"]) for item in error.errors()]}}},
                        status_code=422)


@app.middleware("http")
async def secure_headers(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        try:
            check_csrf(request)
        except HTTPException:
            return JSONResponse({"error": {"code": "csrf_failed", "message": "Invalid CSRF token"}},
                                status_code=403, headers={"X-Content-Type-Options": "nosniff"})
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if settings.app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Starlette applies middleware in reverse registration order. Session must wrap CSRF.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                   session_cookie="ah_session", same_site="lax",
                   https_only=settings.app_env == "production", max_age=8 * 3600)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def migration_script() -> ScriptDirectory:
    # Source checkouts and containers running from the backend directory both
    # work; installed packages can use the backend working directory as well.
    candidates = (Path(__file__).resolve().parent.parent / "alembic", Path.cwd() / "alembic")
    for directory in candidates:
        if (directory / "env.py").is_file() and (directory / "versions").is_dir():
            return ScriptDirectory(str(directory))
    raise RuntimeError("Alembic migration scripts are unavailable")


def database_heads(connection) -> tuple[str, ...]:
    return MigrationContext.configure(connection).get_current_heads()


@app.get("/readyz")
def readyz():
    try:
        expected = set(migration_script().get_heads())
        if not expected:
            raise RuntimeError("Alembic migration scripts have no heads")
        with engine.connect() as db:
            actual = set(database_heads(db))
    except Exception:
        # Readiness must fail closed for missing scripts, missing version table,
        # and unreachable databases, without leaking connection details.
        raise HTTPException(503, detail="Database migration status is unavailable") from None
    if actual != expected:
        raise HTTPException(503, detail="Database migration is not current")
    return {"status": "ready"}


@app.get("/api/me")
def me(request: Request, user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role,
            "csrf_token": request.session["csrf"], "auth_mode": settings.auth_mode,
            "app_env": settings.app_env}


@app.get("/api/auth-mode")
def auth_mode():
    return {"mode": settings.auth_mode}


@app.post("/auth/logout")
def logout(request: Request, user: User = Depends(current_user)):
    check_csrf(request)
    request.session.clear()
    return {"ok": True}


@app.get("/api/admin/jobs")
def list_jobs(status: str | None = None, user: User = Depends(require_role("admin")),
              db: Session = Depends(session_scope)):
    stmt = select(Job).order_by(Job.id.desc()).limit(100)
    if status:
        if status not in ("queued", "running", "succeeded", "failed", "cancelled"):
            raise HTTPException(422, detail="Invalid status")
        stmt = stmt.where(Job.status == status)
    return {"items": [job_json(job) for job in db.scalars(stmt)]}


def job_json(job: Job):
    return {"id": job.id, "type": job.type, "status": job.status,
            "attempts": job.attempts, "max_attempts": job.max_attempts,
            "progress": job.progress, "error": job.error,
            "created_at": job.created_at.isoformat(), "run_after": job.run_after.isoformat()}


@app.get("/api/admin/system")
def system(user: User = Depends(require_role("admin")), db: Session = Depends(session_scope)):
    counts = {status: count for status, count in db.execute(
        select(Job.status, func.count(Job.id)).group_by(Job.status))}
    scheduled = db.scalars(select(ScheduledRun).order_by(ScheduledRun.created_at.desc()).limit(20)).all()
    size = db.scalar(text("SELECT pg_size_pretty(pg_database_size(current_database()))"))
    return {"counts": counts, "database_size": size,
            "migration": ", ".join(sorted(database_heads(db.connection()))),
            "scheduled_runs": [{"task": run.task, "window_key": run.window_key, "job_id": run.job_id}
                               for run in scheduled]}


@app.post("/api/admin/jobs/noop")
def create_noop(request: Request, user: User = Depends(require_role("admin")),
                db: Session = Depends(session_scope)):
    job = queue.enqueue(db, "noop")
    db.add(AuditLog(user_id=user.id, actor_type="user", action="job.enqueue",
                    entity_type="job", entity_id=str(job.id), details={"type": "noop"}))
    db.commit()
    db.refresh(job)
    return job_json(job)


@app.post("/api/admin/jobs/{job_id}/retry")
def retry_job(job_id: int, user: User = Depends(require_role("admin")),
              db: Session = Depends(session_scope)):
    job = queue.retry(db, job_id)
    if not job:
        raise HTTPException(409, detail="Only failed jobs can be retried")
    db.add(AuditLog(user_id=user.id, actor_type="user", action="job.retry",
                    entity_type="job", entity_id=str(job.id), details={}))
    db.commit()
    return job_json(job)


static = Path("/app/static")
if static.exists():
    app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith(("api/", "v1/", "auth/", "sdk/")):
            raise HTTPException(404)
        file = (static / path).resolve()
        if file.is_file() and file.is_relative_to(static.resolve()):
            return FileResponse(file)
        return FileResponse(static / "index.html")