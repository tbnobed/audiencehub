"""Real PostgreSQL recovery checks, exclusively in benchmark --test-suite."""
import csv
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine
from app.imports.service import run_import


@pytest.mark.skipif(
    os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
    reason="Requires benchmark's disposable PostgreSQL cluster",
)
@pytest.mark.parametrize("failure", ["after_commit", "during_next_batch"])
def test_batch_checkpoint_retry_preserves_diagnostics_and_bounce_precedence(tmp_path, monkeypatch, failure):
    from app.imports import service

    path = tmp_path / "checkpoint.csv"
    headers = ["external_id", "email", "amount", "gift_date", "hard_bounce"]
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for index in range(10_003):
            writer.writerow([
                f"gift-{index}", "donor@valid-example.org",
                "invalid" if index in (8, 10_001) else "12.50",
                "12/25/2024" if index in (7, 10_002) else "2024-12-25",
                "true" if index == 10_002 else "",
            ])
    with Session(engine) as db:
        source = db.scalar(text("""
            INSERT INTO sources (key,name,kind,record_types,priority,is_active)
            VALUES (:key,'Checkpoint test','csv',ARRAY['gift'],10,true) RETURNING id
        """), {"key": "checkpoint_" + uuid4().hex})
        import_id = db.scalar(text("""
            INSERT INTO imports (source_id,filename,file_path,record_type,mapping,rows_total)
            VALUES (:source,'checkpoint.csv',:path,'gift',CAST(:mapping AS jsonb),10003)
            RETURNING id
        """), {"source": source, "path": str(path), "mapping": json.dumps({
            "columns": {h: ("attributes.hard_bounce" if h == "hard_bounce" else h)
                        for h in headers},
        })})
        # Preview counters must not be mistaken for committed import counters.
        db.execute(text(
            "UPDATE imports SET rows_ok=4999, rows_rejected=1, warning_count=1 "
            "WHERE id=:id"), {"id": import_id})
        db.commit()

    def fault(update):
        if update["phase"] == "writing" and update["done"] == 10_000:
            # A separate physical connection still cannot own this import,
            # even though the first batch's transaction has committed.
            with engine.connect() as connection:
                acquired = connection.scalar(text(
                    "SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                    {"key": f"audience-hub:import:{import_id}"})
                assert acquired is False
            if failure == "after_commit":
                raise RuntimeError("fault after durable first batch")

    original_gifts = service._insert_gifts
    gift_batches = []
    def write_gifts(*args):
        original_gifts(*args)
        gift_batches.append(1)
        if failure == "during_next_batch" and len(gift_batches) == 2:
            raise RuntimeError("fault after durable first batch")
    monkeypatch.setattr(service, "_insert_gifts", write_gifts)

    with pytest.raises(RuntimeError, match="durable first batch"):
        run_import(import_id, fault)
    monkeypatch.setattr(service, "_insert_gifts", original_gifts)
    with Session(engine) as db:
        before = dict(db.execute(text("SELECT * FROM imports WHERE id=:id"),
                                 {"id": import_id}).mappings().one())
        assert before["last_committed_record_number"] == 10_001
        assert before["rows_ok"] == 9_999
        assert before["rows_rejected"] == 1
        assert before["warning_count"] == 1
        assert before["status"] == "failed"
        assert db.scalar(text("SELECT count(*) FROM gifts WHERE source_id=:id"),
                         {"id": source}) == 9_999
        assert db.scalar(text("""
            SELECT attributes->'_import_consent'->>'status' FROM source_records
            WHERE source_id=:id AND external_id='gift-0'
        """), {"id": source}) == "opted_out"
        assert db.scalar(text("SELECT to_regclass(:stage)"),
                         {"stage": f"ah_import_rows_{import_id}"}) is None

    calls = []
    original = service.map_and_validate_row
    def validate(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "map_and_validate_row", validate)
    result = run_import(import_id)
    assert len(calls) == 3  # committed records aren't normalized/validated again
    assert result["rows_total"] == 10_003
    assert result["rows_ok"] == 10_001
    assert result["rows_rejected"] == 2
    assert result["warning_count"] == 2
    assert run_import(import_id) == result  # completed replay is a no-op
    with Session(engine) as db:
        after = db.execute(text("SELECT * FROM imports WHERE id=:id"),
                           {"id": import_id}).mappings().one()
        assert after["last_committed_record_number"] == 10_004
        assert after["status"] == "completed"
        for table in ("source_records", "gifts"):
            assert db.scalar(text(f"SELECT count(*) FROM {table} WHERE source_id=:id"),
                             {"id": source}) == 10_001
        with open(after["error_file_path"], newline="") as handle:
            diagnostics = list(csv.DictReader(handle))
        assert len([d for d in diagnostics if d["severity"] == "warning"]) == 2
        assert {int(d["row"]) for d in diagnostics} == {9, 10, 10_003, 10_004}


def test_upserts_order_by_actual_conflict_keys():
    from app.imports.staging import copy_upsert
    from test_imports import _ExecuteRecorder
    db = _ExecuteRecorder()
    copy_upsert(db, """
        INSERT INTO enrichment_values (profile_id, source_id, attribute_key)
        SELECT :profile_id, :source_id, :key FROM {stage} WHERE true
        ON CONFLICT (profile_id, source_id, attribute_key) DO NOTHING
    """, [{"profile_id": 2, "source_id": 4, "key": "z"}],
        "profile_id bigint, source_id bigint, key text")
    assert 'ORDER BY staged."profile_id", staged."source_id", staged."key"' in db.statement


@pytest.mark.parametrize("operation", ["mapping", "validation"])
def test_partial_import_rejects_mapping_and_preview_counter_changes(monkeypatch, operation):
    from fastapi import HTTPException
    from app.imports import api
    row = {"status": "failed", "last_committed_record_number": 10_001}
    monkeypatch.setattr(api, "_get_import", lambda *_: row)
    with pytest.raises(HTTPException) as exc:
        if operation == "mapping":
            api.set_mapping(9, api.MappingBody(columns={"email": "email"}), user=None, db=None)
        else:
            api.validate_import(9, user=None, db=None)
    assert exc.value.status_code == 409
    assert "committed batches" in exc.value.detail


@pytest.mark.parametrize("import_status", ["failed", "running"])
def test_failed_import_resume_preserves_cursor_counters_and_progress(monkeypatch, import_status):
    from types import SimpleNamespace
    from app.imports import api
    row = {"status": import_status, "job_status": "failed", "last_committed_record_number": 10_001,
           "rows_total": 25_000, "rows_ok": 9_990, "rows_rejected": 10,
           "warning_count": 7, "mapping": {"columns": {"email": "email"}}}
    job = SimpleNamespace(id=50, progress={})
    statements = []
    class Db:
        def execute(self, sql, params):
            statements.append(str(sql))
        def commit(self):
            pass
    monkeypatch.setattr(api, "_get_import", lambda *_: row)
    monkeypatch.setattr(api, "enqueue", lambda *args, **kwargs: job)
    assert api.start_import(9, user=None, db=Db())["status"] == "running"
    assert job.progress == {"done": 10_000, "total": 25_000, "message": "Preparing resume…"}
    assert statements == ["UPDATE imports SET status='running' WHERE id=:id"]
    serialized = api._json_import(row)
    assert serialized["last_committed_record_number"] == 10_001
    assert serialized["rows_ok"] == 9_990
    assert serialized["warning_count"] == 7