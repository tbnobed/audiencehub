"""Bounded, cross-process dashboard cache; no database DDL or worker hooks.

PostgreSQL's MVCC snapshot is a conservative durable data version: any new
transaction or completion of an in-flight writer invalidates the entry. Files are
shared by API processes on the same host; no correctness depends on that sharing.
"""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

DEADLINE_SECONDS = 25
TTL_SECONDS = 60
MAX_ENTRIES = 32
MAX_BYTES = 4 * 1024 * 1024
_deadline = ContextVar("dashboard_deadline", default=None)


def check_deadline(db=None):
    deadline = _deadline.get()
    if deadline is None:
        return
    remaining = int((deadline-time.monotonic())*1000)
    if remaining <= 0:
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
        check_deadline(db)
        yield
        check_deadline()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in ("57014", "55P03"):
            raise HTTPException(504, detail="Dashboard database deadline exceeded. Please retry.") from exc
        raise
    finally:
        _deadline.reset(token)


def cached(db, key, build):
    check_deadline(db)
    # Never publish uncommitted session-local writes as globally reusable facts.
    if db.new or db.dirty or db.deleted or db.execute(
        text("SELECT pg_current_xact_id_if_assigned() IS NOT NULL")
    ).scalar_one():
        return build()
    version = db.execute(text("SELECT pg_current_snapshot()::text")).scalar_one()
    from app.config import get_settings
    settings = get_settings()
    namespace = hashlib.sha256(
        (settings.database_url + settings.pii_hash_pepper).encode()).hexdigest()[:24]
    directory = Path(tempfile.gettempdir()) / f"kinship-dashboard-{os.getuid()}-{namespace}"
    directory.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.sha256(json.dumps(["v2", version, *key], default=str).encode()).hexdigest()
    path = directory / (digest+".json")

    def read():
        try:
            if time.time()-path.stat().st_mtime >= TTL_SECONDS or path.stat().st_size > MAX_BYTES:
                return None
            return json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    hit = read()
    if hit is not None:
        return copy.deepcopy(hit)
    # One bounded lock for the cache prevents a cold-page thundering herd, even
    # when several requests observed slightly different MVCC snapshots.
    with (directory / "compute.lock").open("a") as lock:
        wait_until = time.monotonic()+2
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                check_deadline()
                hit = read()
                if hit is not None:
                    return hit
                if time.monotonic() >= wait_until:
                    raise HTTPException(503, detail="Dashboard refresh is in progress. Please retry.",
                                        headers={"Retry-After": "3"})
                time.sleep(.05)
        hit = read()
        if hit is not None:
            return hit
        # A killed writer can leave its atomic-write staging file behind.
        # Holding the sole writer lock makes these safe to remove.
        for abandoned in directory.glob("*.tmp"):
            abandoned.unlink(missing_ok=True)
        result = build()  # exceptions are never cached
        check_deadline()
        encoded = json.dumps(result)
        if len(encoded.encode()) <= MAX_BYTES:
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            try:
                temporary.write_text(encoded)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for i, old in enumerate(files):
            if i >= MAX_ENTRIES or time.time()-old.stat().st_mtime >= TTL_SECONDS:
                old.unlink(missing_ok=True)
        return result