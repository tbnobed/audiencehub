"""Bounded 500/5000-person attachment plans on fresh private five-file loads."""
import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_consent_ingestion_pg import db

pytestmark = pytest.mark.skipif(
    os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
    reason="requires disposable benchmark PostgreSQL",
)


@pytest.mark.parametrize("people", [500, 5000])
def test_attachment_prerequisite_statistics(db, monkeypatch, tmp_path, people):
    import app.importer
    import app.load_status
    from app.seed import generate_seed
    from app.imports.service import run_import
    from app.identity.bulk import resolve_bulk
    from app.identity.attachments import attach_transactions
    from app.jobs.queue import succeed

    class Recorder:
        sql = ""

        def scalar(self, *args):
            return True

        def execute(self, statement, params=None):
            if str(statement).startswith(("DROP TABLE", "CREATE TEMP TABLE", "CREATE UNIQUE INDEX",
                                          "ANALYZE", "UPDATE ", "INSERT ")):
                self.sql += str(statement) + ";\n"

    recorder = Recorder()
    attach_transactions(recorder)
    statements = [part.strip() for part in recorder.sql.split(";") if part.strip()]
    local = db.get_bind()
    monkeypatch.setattr(app.importer, "engine", local)
    files = generate_seed(profiles=people, scale="medium", output_dir=tmp_path / "seed")["files"]
    evidence = {"people": people, "variants": {}}

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
            started = time.perf_counter()
            evidence["identity"] = resolve_bulk(session, mode="full")
            evidence["identity_seconds"] = time.perf_counter() - started
            # Deliberately do NOT commit identity: this is the real handler's
            # transaction boundary, so autovacuum cannot rescue new-owner stats.
            for label, refresh in (("import_statistics_only", False), ("fresh_owner_statistics", True)):
                variant = {"steps": []}
                evidence["variants"][label] = variant
                savepoint = session.begin_nested()
                started = time.perf_counter()
                try:
                    session.execute(text("SET LOCAL statement_timeout='10s'"))
                    if refresh:
                        for table in ("profiles", "identifiers", "source_records", "identifier_blocklist", "imports", "sources"):
                            session.execute(text(f"ANALYZE {table}"))
                    for sql in statements:
                        step = {"statement": sql.splitlines()[0]}
                        variant["steps"].append(step)
                        before = time.perf_counter()
                        if sql.startswith(("CREATE TEMP TABLE", "UPDATE ", "INSERT ")):
                            step["estimated_plan"] = session.execute(
                                text("EXPLAIN (FORMAT JSON) " + sql)).scalar_one()[0]
                            step["executed_plan"] = session.execute(
                                text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)).scalar_one()[0]
                        else:
                            session.execute(text(sql))
                        step["seconds"] = time.perf_counter() - before
                    variant["status"] = "completed"
                    variant["unresolved_transactions"] = session.scalar(text("""
                        SELECT count(*) FROM source_records sr JOIN imports i ON i.id=sr.last_import_id
                        WHERE i.record_type IN ('gift','event') AND sr.resolved_at IS NULL
                    """))
                except DBAPIError as exc:
                    assert getattr(exc.orig, "sqlstate", None) == "57014"
                    variant["status"] = "statement_timeout"
                finally:
                    variant["seconds"] = time.perf_counter() - started
                    savepoint.rollback()
            session.rollback()
        return 0

    monkeypatch.setattr(app.load_status, "run_load_status", drain)
    app.importer.import_seed_files(files)
    target = Path(os.environ.get("ATTACHMENT_EVIDENCE_DIR", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"attachment-{people}.json"
    path.write_text(json.dumps(evidence, indent=2, default=str))
    print("ATTACHMENT_EVIDENCE", path, json.dumps({
        "people": people, "identity_seconds": evidence["identity_seconds"],
        "variants": {k: {key: value for key, value in v.items() if key != "steps"}
                     for k, v in evidence["variants"].items()},
    }))
    assert evidence["variants"]["fresh_owner_statistics"]["status"] == "completed"
    assert evidence["variants"]["fresh_owner_statistics"]["unresolved_transactions"] == 0
