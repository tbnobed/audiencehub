"""Real 50k regression, opt-in and ONLY in benchmark's disposable database."""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine
from app.identity.locking import IdentityResolutionDeferred
from app.identity.resolver import resolve_batch
from app.imports.service import run_import
from app.models import Import, Source


pytestmark = pytest.mark.skipif(
    os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1"
    or os.environ.get("KINSHIP_IDENTITY_SCALE_TEST") != "1",
    reason="requires opt-in KINSHIP_IDENTITY_SCALE_TEST=1 and benchmark --test-suite",
)


def test_identity_scale_50000_default_lock_budget_concurrent_contact_import(tmp_path):
    prefix = uuid.uuid4().hex
    requested = 50_000
    imported_count = 10_000
    with Session(engine) as db:
        assert int(db.scalar(text("SHOW max_locks_per_transaction"))) == 64
        # Check the disposable fixture's connection too; never use the app DB.
        assert db.scalar(text("SELECT current_database()")) == "benchmark"
        # Earlier committed tests may leave pending rows in this isolated suite.
        while True:
            previous = resolve_batch(db, limit=requested)
            db.commit()
            if previous["records"] == 0:
                break
        seeded = Source(key=f"scale_{prefix}", name="Scale regression", kind="csv",
                        record_types=["contact"])
        importing = Source(key=f"scale_import_{prefix}", name="Concurrent scale import",
                           kind="csv", record_types=["contact"])
        db.add_all([seeded, importing])
        db.flush()
        seeded_id, importing_id = seeded.id, importing.id
        # 200k distinct match keys: the old per-identifier advisory-lock loop
        # exhausts default shared memory even on its former 10k request cap.
        db.execute(text("""
            INSERT INTO source_records
                (source_id, external_id, email_norm, attributes, raw_hash)
            SELECT :source, n::text, :prefix || '-' || n || '@example.org',
                   jsonb_build_object('user_id', :prefix || '-user-' || n,
                                      'anonymous_id', :prefix || '-anon-' || n,
                                      'identify', true),
                   md5(:prefix || n)
            FROM generate_series(1, :count) AS n
        """), {"source": seeded_id, "prefix": prefix, "count": requested})
        path = tmp_path / "scale_contacts.csv"
        # Shared email links the imported rows to the seeded population.
        path.write_text("external_id,email,first_name\n" + "".join(
            f"{n},{prefix}-{n}@example.org,Concurrent\n"
            for n in range(1, imported_count + 1)
        ))
        imported = Import(
            source_id=importing_id, filename=path.name, file_path=str(path),
            record_type="contact", rows_total=imported_count,
            mapping={"columns": {key: key for key in
                                 ("external_id", "email", "first_name")}, "options": {}},
        )
        db.add(imported)
        db.flush()
        import_id = imported.id
        db.commit()

    ready = threading.Event()
    allow_write = threading.Event()
    import_committed = threading.Event()
    finish_import = threading.Event()
    overlap = []

    def progress(value):
        if value["phase"] != "writing":
            return
        if value["done"] == 0:
            ready.set()
            assert allow_write.wait(180), "Resolver did not release import"
        elif not import_committed.is_set():
            import_committed.set()
            assert finish_import.wait(240), "Resolver did not finish request"

    def after_resolver_commit():
        if overlap:
            return
        # The production handler owns this loop and commits each bounded group.
        # Force actual import writes before the 50k request's next group.
        allow_write.set()
        assert import_committed.wait(180), "No contact batch committed during resolution"
        with Session(engine) as observer:
            written = observer.scalar(text(
                "SELECT count(*) FROM source_records WHERE source_id=:source"
            ), {"source": importing_id})
            resolved = observer.scalar(text(
                "SELECT count(*) FROM source_records "
                "WHERE source_id=:source AND resolved_at IS NOT NULL"
            ), {"source": seeded_id})
        assert written > 0
        assert 0 < resolved < requested, "Import must commit inside, not after, the request"
        overlap.append((written, resolved))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_import, import_id, progress_callback=progress)
        try:
            assert ready.wait(180), "Contact import did not reach writing phase"
            total_resolved = 0
            max_advisory_locks = 0
            groups = 0
            deadline = time.monotonic() + 240
            with Session(engine) as db:
                while total_resolved < requested:
                    assert time.monotonic() < deadline, "50k request exceeded deadline"
                    try:
                        result = resolve_batch(db, limit=requested - total_resolved)
                    except IdentityResolutionDeferred:
                        db.rollback()
                        time.sleep(0.05)
                        continue
                    assert 0 < result["records"] <= 500
                    advisory_locks = db.scalar(text("""
                        SELECT count(*) FROM pg_locks
                        WHERE pid=pg_backend_pid() AND locktype='advisory' AND granted
                    """))
                    # At most 1024 identifier buckets plus coordination lock.
                    assert 1 <= advisory_locks <= 1025
                    max_advisory_locks = max(max_advisory_locks, advisory_locks)
                    total_resolved += result["records"]
                    groups += 1
                    db.commit()
                    if not overlap:
                        after_resolver_commit()
            assert total_resolved == requested
            assert groups >= 100
            assert max_advisory_locks > 500, "Exercise a diverse, nontrivial bucket set"
            assert overlap and not future.done()
            with Session(engine) as db:
                counts = db.execute(text("""
                    SELECT count(*), count(*) FILTER (
                        WHERE resolved_at IS NULL OR profile_id IS NULL)
                    FROM source_records WHERE source_id=:source
                """), {"source": seeded_id}).one()
                assert tuple(counts) == (requested, 0)
        finally:
            allow_write.set()
            finish_import.set()
        future.result(timeout=180)

    # Resolve the imported records too. Only expected coordination deferrals
    # may retry; SQL errors (including out-of-shared-memory) fail immediately.
    deadline = time.monotonic() + 180
    with Session(engine) as db:
        while True:
            assert time.monotonic() < deadline, "Imported records failed to drain"
            try:
                result = resolve_batch(db, limit=requested)
                db.commit()
            except IdentityResolutionDeferred:
                db.rollback()
                time.sleep(0.05)
                continue
            if result["records"] == 0:
                break
        imported = db.get(Import, import_id)
        assert imported.rows_ok == imported_count
        assert imported.rows_rejected == 0
        counts = db.execute(text("""
            SELECT count(*), count(DISTINCT profile_id),
                   count(*) FILTER (WHERE resolved_at IS NULL OR profile_id IS NULL)
            FROM source_records WHERE source_id=ANY(:sources)
        """), {"sources": [seeded_id, importing_id]}).one()
        assert tuple(counts) == (requested + imported_count, requested, 0)
        assert int(db.scalar(text("SHOW max_locks_per_transaction"))) == 64
        print(f"identity_scale: max_locks_per_transaction=64; "
              f"requested={requested}; imported={imported_count}; groups={groups}; "
              f"max_advisory_locks={max_advisory_locks}; overlap={overlap}; "
              f"final_records={counts[0]}; profiles={counts[1]}; unresolved={counts[2]}")