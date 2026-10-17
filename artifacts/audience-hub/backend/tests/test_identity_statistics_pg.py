"""Controlled fresh-statistics regression, exclusively in disposable schemas."""
import json
import os
import time
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_consent_ingestion_pg import db

pytestmark = pytest.mark.skipif(
    os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
    reason="requires disposable benchmark PostgreSQL",
)


def test_fresh_five_file_identity_statistics_and_full_traits(db, monkeypatch, tmp_path):
    import app.db
    import app.importer
    import app.load_status
    from app.seed import generate_seed
    from app.imports.service import run_import
    from app.identity.bulk import resolve_bulk
    from app.identity.attachments import attach_transactions
    from app.identity.statistics import refresh_identity_statistics
    from app.jobs.handlers import run
    from app.jobs.queue import enqueue, succeed
    from app.traits.engine import _RECOMPUTE_SQL

    local = db.get_bind()
    monkeypatch.setattr(app.db, "engine", local)
    monkeypatch.setattr(app.importer, "engine", local)
    files = generate_seed(profiles=500, scale="medium", output_dir=tmp_path / "seed")["files"]
    evidence = {}
    started = time.perf_counter()

    def plan(session):
        return session.execute(text("EXPLAIN (FORMAT JSON) " + str(_RECOMPUTE_SQL)),
                               {"profile_ids": None, "as_of": date.today()}).scalar_one()[0]

    def drain(bind, **kwargs):
        with Session(local) as session:
            jobs = session.execute(text("""
                SELECT id,(payload->>'import_id')::bigint FROM jobs WHERE type='import.run' ORDER BY id
            """)).all()
            for job_id, imported in jobs:
                session.execute(text("UPDATE jobs SET status='running' WHERE id=:id"), {"id": job_id})
                session.commit()
                run_import(imported, db=session)
                succeed(session, job_id)
                session.commit()
            before = time.perf_counter()
            evidence["identity"] = resolve_bulk(session, mode="full")
            attach_transactions(session)
            session.commit()
            evidence["identity_seconds"] = time.perf_counter() - before
            evidence["counts"] = dict(session.execute(text("""
                SELECT (SELECT count(*) FROM profiles) profiles,
                       (SELECT count(*) FROM gifts) gifts,
                       (SELECT count(*) FROM events) events
            """)).one()._mapping)
            evidence["before_plan"] = plan(session)
            evidence["before_null_fractions"] = [dict(row._mapping) for row in session.execute(text("""
                SELECT tablename,attname,null_frac,n_distinct FROM pg_stats
                WHERE schemaname=current_schema() AND tablename IN ('gifts','source_records','events')
                  AND attname='profile_id'
            """))]
            nested = session.begin_nested()
            before = time.perf_counter()
            try:
                session.execute(text("SET LOCAL statement_timeout='3s'"))
                session.execute(_RECOMPUTE_SQL, {"profile_ids": None, "as_of": date.today()})
                evidence["baseline"] = "completed"
            except DBAPIError as exc:
                assert getattr(exc.orig, "sqlstate", None) == "57014"
                evidence["baseline"] = "statement_timeout"
            finally:
                evidence["baseline_seconds"] = time.perf_counter() - before
                nested.rollback()
            before = time.perf_counter()
            refresh_identity_statistics(session)
            evidence["statistics_seconds"] = time.perf_counter() - before
            evidence["after_plan"] = plan(session)
            evidence["after_null_fractions"] = [dict(row._mapping) for row in session.execute(text("""
                SELECT tablename,attname,null_frac,n_distinct FROM pg_stats
                WHERE schemaname=current_schema() AND tablename IN ('gifts','source_records','events')
                  AND attname='profile_id'
            """))]
            job = enqueue(session, "traits.recompute", {"mode": "full"}, dedupe_key="statistics-regression")
            job_id = job.id
            session.commit()
        before = time.perf_counter()
        run("traits.recompute", {"mode": "full"}, job_id=job_id)
        evidence["full_traits_rollups_seconds"] = time.perf_counter() - before
        with Session(local) as session:
            assert session.scalar(text("SELECT count(*) FROM profile_traits")) == evidence["counts"]["profiles"]
            assert session.scalar(text("SELECT count(*) FROM trait_snapshots")) > 0
            assert session.scalar(text("SELECT count(*) FROM dashboard_giving_daily")) > 0
        return 0

    monkeypatch.setattr(app.load_status, "run_load_status", drain)
    app.importer.import_seed_files(files)
    evidence["pipeline_seconds"] = time.perf_counter() - started
    path = tmp_path / "fresh-statistics-evidence.json"
    path.write_text(json.dumps(evidence, indent=2, default=str))
    print("STATISTICS_EVIDENCE", path, json.dumps({k: v for k, v in evidence.items() if "plan" not in k}))
    assert evidence["full_traits_rollups_seconds"] < 10
