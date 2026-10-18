"""Small real-PG proof for the isolated audit harness's worker-equivalent budget."""
import json
import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.seed_audit_verification import PhaseTimings, bulk_phase_budget


def test_phase_timings_retains_completed_and_failed_elapsed(tmp_path, monkeypatch):
    import app.seed_audit_verification as audit
    ticks = iter([10.0, 12.0, 20.0, 25.0])
    monkeypatch.setattr(audit.time, "perf_counter", lambda: next(ticks))
    progress = PhaseTimings(tmp_path)
    assert progress.run("real_imports", lambda: 42) == 42
    after_import = json.loads((tmp_path / "phase-timings.json").read_text())
    assert after_import["completed_timings"] == {"real_imports": 2.0}
    def fail():
        raise RuntimeError("deliberate identity failure")
    with pytest.raises(RuntimeError, match="deliberate"):
        progress.run("identity", fail)
    failed = json.loads((tmp_path / "phase-timings.json").read_text())
    assert failed["status"] == "failed"
    assert failed["completed_timings"] == {"real_imports": 2.0}
    assert failed["failing_phase"] == "identity"
    assert failed["failing_phase_elapsed_seconds"] == 5.0
    assert failed["error_type"] == "RuntimeError"


@pytest.mark.skipif(os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1",
                    reason="requires the private PostgreSQL benchmark runner")
def test_audit_phase_bulk_budget_survives_commit_and_restores_on_success_failure():
    from app.db import engine
    from app.jobs import lifecycle
    from app.segments.api import timed
    from app.seed_audit_verification import assert_isolated
    assert_isolated(engine)
    seconds = text("SELECT extract(epoch FROM current_setting('statement_timeout')::interval)")
    assert lifecycle.current() is None
    assert lifecycle.timeout_ms("identity.resolve_batch") == 7_200_000
    with Session(engine) as db:
        # Direct handlers do not install worker ownership: actual PG therefore
        # uses API 60 seconds despite identity's configured worker 7200 seconds.
        assert db.scalar(seconds) == 60
    initial_listeners = tuple(engine.dispatch.begin)
    with bulk_phase_budget(engine):
        with Session(engine) as db:
            assert db.scalar(seconds) == 7200
            db.commit()
            assert db.scalar(seconds) == 7200
            db.rollback()
            assert db.scalar(seconds) == 7200
            # Even inside the bulk scope, API preview's own guard remains 10s.
            assert timed(db, lambda: db.scalar(seconds)) == 10
            assert db.scalar(seconds) == 7200
    assert tuple(engine.dispatch.begin) == initial_listeners
    with Session(engine) as db:
        assert db.scalar(seconds) == 60
        assert timed(db, lambda: db.scalar(seconds)) == 10
        assert db.scalar(seconds) == 60
    with pytest.raises(RuntimeError, match="phase failure"):
        with bulk_phase_budget(engine):
            with Session(engine) as db:
                assert db.scalar(seconds) == 7200
                db.commit()
                assert db.scalar(seconds) == 7200
                raise RuntimeError("phase failure")
    assert tuple(engine.dispatch.begin) == initial_listeners
    with Session(engine) as db:
        assert db.scalar(seconds) == 60


def test_audit_bulk_budget_refuses_unisolated_engine(monkeypatch):
    monkeypatch.delenv("KINSHIP_BENCHMARK_ISOLATED_TESTS", raising=False)
    with pytest.raises(RuntimeError, match="isolated benchmark"):
        with bulk_phase_budget(None):
            pytest.fail("Must reject before registering any listener")
