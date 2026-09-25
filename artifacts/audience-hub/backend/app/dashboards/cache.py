"""PostgreSQL-shared stale-while-revalidate cache, with nonblocking refresh locks."""
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError

logger = logging.getLogger(__name__)
TTL_SECONDS = 60
DEADLINE_SECONDS = 15
MIN_STATEMENT_BUDGET_MS = 100
_deadline = ContextVar("dashboard_deadline", default=None)
# The executor holds work, not data. Payloads and coordination live in PostgreSQL.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dashboard-refresh")


def check_deadline(db=None):
    end = _deadline.get()
    if end is None:
        return
    remaining = int((end-time.monotonic())*1000)
    if remaining <= 0 or (db is not None and remaining < MIN_STATEMENT_BUDGET_MS):
        raise HTTPException(504, detail="Dashboard computation exceeded its deadline. Please retry.")
    if db is not None:
        db.execute(text("SELECT set_config('statement_timeout', :ms, true), "
                        "set_config('lock_timeout', '2000', true)"), {"ms": str(remaining)})


@contextmanager
def deadline(db):
    if _deadline.get() is not None:
        yield
        return
    token = _deadline.set(time.monotonic()+DEADLINE_SECONDS)
    try:
        check_deadline()
        previous = db.execute(text(
            "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')"
        )).one()
        check_deadline(db)
        yield
        check_deadline()
        db.execute(text("SELECT set_config('statement_timeout', :statement, true), "
                        "set_config('lock_timeout', :lock, true)"),
                   {"statement": previous[0], "lock": previous[1]})
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in ("57014", "55P03"):
            db.invalidate()
            raise HTTPException(504, detail="Dashboard database deadline exceeded. Please retry.") from exc
        raise
    except HTTPException as exc:
        if exc.status_code == 504:
            db.invalidate()
        raise
    finally:
        _deadline.reset(token)


def generation(db):
    value = db.execute(text(
        "SELECT value FROM dashboard_kpis WHERE key='generation' ORDER BY as_of DESC LIMIT 1"
    )).scalar_one_or_none()
    if value is None:
        raise HTTPException(503, detail=(
            "Dashboard rollups have not been initialized. Run traits.recompute "
            "(enqueue the traits.recompute job and run python -m app.jobs.worker) after upgrading."))
    return value


def _publish(db, key, result, version):
    # Never let a slow old-generation builder overwrite a newly published
    # generation. Share the refresh lock only for this tiny publication and
    # never wait behind a running offline refresh on the request path.
    if not db.execute(text(
        "SELECT pg_try_advisory_xact_lock_shared(hashtext('dashboard-rollups'))"
    )).scalar_one():
        return False
    if generation(db) != version:
        return False
    db.execute(text("""
      INSERT INTO dashboard_cache(key,payload,computed_at,generation)
      VALUES(:key,CAST(:payload AS jsonb),clock_timestamp(),:version)
      ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,
        computed_at=excluded.computed_at,generation=excluded.generation
      WHERE excluded.generation=(SELECT value #>> '{}' FROM dashboard_kpis WHERE key='generation')
    """), {"key": key, "payload": json.dumps(result, default=str), "version": version})
    return True


def _refresh(key, logical_key, version, engine):
    try:
        with Session(engine.execution_options(isolation_level="REPEATABLE READ")) as db:
            db.execute(text("SET LOCAL statement_timeout='15s'"))
            locked = db.execute(text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"
            ), {"key": key}).scalar_one()
            if not locked:
                return
            existing = db.execute(text("""
              SELECT generation=:version AND computed_at>now()-interval '60 seconds'
              FROM dashboard_cache WHERE key=:key
            """), {"key": key, "version": version}).scalar_one_or_none()
            if existing:
                return
            # One consistent generation across every query in this background build.
            from app.dashboards.api import build_cache_key
            result = build_cache_key(db, logical_key)
            with Session(engine) as writer:
                _publish(writer, key, result, version)
                writer.commit()
    except Exception:
        logger.exception("Dashboard background refresh failed; stale payload retained")


def cached(db, key, build):
    version = generation(db)
    digest = hashlib.sha256(json.dumps(key, default=str).encode()).hexdigest()
    row = db.execute(text("""
      SELECT payload,generation,computed_at>now()-interval '60 seconds' AS fresh
      FROM dashboard_cache WHERE key=:key
    """), {"key": digest}).mappings().one_or_none()
    if row:
        if row["generation"] != version or not row["fresh"]:
            _executor.submit(_refresh, digest, key, version, db.get_bind())
        return row["payload"]
    # Build and publish from one repeatable-read snapshot, independent of the
    # auth/request session. A concurrent traits commit cannot mix generations.
    with Session(db.get_bind().execution_options(isolation_level="REPEATABLE READ")) as writer:
        writer.execute(text("SET LOCAL statement_timeout='15s'"))
        version = generation(writer)
        from app.dashboards.api import build_cache_key
        result = build_cache_key(writer, key)
    with Session(db.get_bind()) as writer:
        _publish(writer, digest, result, version)
        writer.commit()
    return result