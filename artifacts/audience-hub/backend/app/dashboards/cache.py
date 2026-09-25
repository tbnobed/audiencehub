"""Bounded, cross-process dashboard cache; no database DDL or worker hooks.

PostgreSQL's MVCC snapshot is a conservative durable data version: any new
transaction or completion of an in-flight writer invalidates the entry. Files are
shared by API processes on the same host; no correctness depends on that sharing.

Coalesced callers may adopt a generation published after their call started,
even if writes continue during computation. This is an as-of-computation result,
not a promise of a transactionally consistent multi-query snapshot. Preexisting
generations always require an exact MVCC match; TTL alone never grants freshness.
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
# Monotonic timestamps are comparable across processes, but not across boots.
# This cache already requires a local Unix host (flock); deployed hosts are Linux.
_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()


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
    # Direct callers get the same bounded budget as HTTP endpoint callers.
    if _deadline.get() is None:
        with deadline(db):
            return cached(db, key, build)
    started = time.monotonic_ns()
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
    digest = hashlib.sha256(json.dumps(["v3", _boot_id, *key], default=str).encode()).hexdigest()
    path = directory / (digest+".json")

    def read():
        try:
            stat = path.stat()
            if time.time()-stat.st_mtime >= TTL_SECONDS or stat.st_size > MAX_BYTES:
                return None
            entry = json.loads(path.read_text())
            if "result" not in entry:
                return None
            if entry["version"] == version or entry["published"] >= started:
                return entry
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return None

    hit = read()
    if hit is not None:
        return copy.deepcopy(hit["result"])
    # Persistent per-logical-key inodes: never unlink lock files (even an
    # apparently idle lock can have a waiter holding its inode). Distinct dates
    # and dashboards never hold each other's expensive-computation lock.
    with (directory / (digest+".lock")).open("a") as lock:
        while True:
            check_deadline()
            hit = read()
            if hit is not None:
                return hit["result"]
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                check_deadline()
                time.sleep(min(.05, max(0, _deadline.get()-time.monotonic())))
        hit = read()
        if hit is not None:
            return hit["result"]
        # A killed writer can leave its atomic-write staging file behind.
        # Only this key's staging files are safe to remove.
        for abandoned in directory.glob(digest+".*.tmp"):
            abandoned.unlink(missing_ok=True)
        check_deadline(db)
        result = build()  # exceptions are never cached
        check_deadline()
        encoded = json.dumps({"version": version, "published": time.monotonic_ns(),
                              "result": result})
        if len(encoded.encode()) <= MAX_BYTES:
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            try:
                temporary.write_text(encoded)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        # Other keys can publish/prune concurrently. Missing files are normal.
        files = []
        for candidate in directory.glob("*.json"):
            try:
                files.append((candidate.stat().st_mtime, candidate))
            except FileNotFoundError:
                pass
        for i, (modified, old) in enumerate(sorted(files, reverse=True)):
            if i >= MAX_ENTRIES or time.time()-modified >= TTL_SECONDS:
                old.unlink(missing_ok=True)
        return result