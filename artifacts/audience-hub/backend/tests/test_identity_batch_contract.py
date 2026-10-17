"""No-connection orchestration regressions; SQL execution is acceptance-tested separately."""
import inspect
from unittest.mock import Mock

import pytest

from app.identity.locking import IdentityResolutionDeferred
from app.jobs.handlers import run


class FakeSession:
    def __init__(self, *, active=False, phase="importing", large=False):
        self.active, self.phase, self.large = active, phase, large
        self.statements = []
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def scalar(self, statement, params=None):
        sql = str(statement)
        if "type='import.run'" in sql:
            return self.active
        if "SELECT phase FROM identity_batches" in sql:
            return self.phase
        if "WITH sizes" in sql:
            return self.large
        if "INSERT INTO identity_batches" in sql:
            return 44
        return False

    def execute(self, statement, params=None):
        self.statements.append(str(statement))

    def commit(self):
        self.commits += 1


@pytest.fixture
def harness(monkeypatch):
    import sqlalchemy.orm
    import app.identity.bulk
    import app.identity.attachments
    import app.identity.statistics
    import app.identity.locking
    import app.jobs.phase_progress
    import app.jobs.queue

    calls = []
    current = {}
    monkeypatch.setattr(sqlalchemy.orm, "Session", lambda engine: current["db"])
    monkeypatch.setattr(app.identity.locking, "acquire_resolver_lock", lambda db: calls.append("lock"))
    monkeypatch.setattr(app.identity.bulk, "resolve_bulk",
                        lambda db, **kw: calls.append(("resolve", kw["mode"])))
    monkeypatch.setattr(app.identity.attachments, "attach_transactions", lambda db, **kwargs: calls.append("attach"))
    monkeypatch.setattr(app.identity.statistics, "refresh_identity_statistics", lambda db: calls.append("analyze"))
    monkeypatch.setattr(app.jobs.phase_progress, "begin_phase",
                        lambda engine, job_id, phase: calls.append(("phase", phase)))
    monkeypatch.setattr(app.jobs.phase_progress, "finish_phase",
                        lambda engine, job_id: calls.append("finished"))
    enqueue = Mock()
    monkeypatch.setattr(app.jobs.queue, "enqueue", enqueue)
    return current, calls, enqueue


@pytest.mark.parametrize("payload", [{}, {"batch_id": 4}, {"import_id": 9}])
def test_concurrent_queued_imports_defer_without_any_graph_work(harness, payload):
    current, calls, enqueue = harness
    current["db"] = FakeSession(active=True)
    with pytest.raises(IdentityResolutionDeferred):
        run("identity.resolve_batch", payload, 10)
    assert calls == ["lock"]
    enqueue.assert_not_called()


@pytest.mark.parametrize("payload,large,mode", [
    ({"batch_id": 4}, False, "full"),
    ({"import_id": 9}, True, "full"),
    ({"import_id": 9}, False, "incremental"),
])
def test_one_pass_then_attachments_then_scoped_traits(harness, payload, large, mode):
    current, calls, enqueue = harness
    current["db"] = FakeSession(large=large)
    run("identity.resolve_batch", payload, 10)
    assert calls == ["lock", ("phase", "resolving"), ("resolve", mode),
                     ("phase", "attaching"), "attach", "analyze", "finished"]
    assert current["db"].commits == 1
    expected = {"mode": "full", **payload}
    if "import_id" in payload:
        expected["batch_id"] = 44
    assert enqueue.call_args.args[1:3] == ("traits.recompute", expected)


def test_registering_batch_cannot_resolve(harness):
    current, calls, enqueue = harness
    current["db"] = FakeSession(phase="registering")
    with pytest.raises(IdentityResolutionDeferred):
        run("identity.resolve_batch", {"batch_id": 4}, 10)
    assert calls == ["lock"]


def test_completed_batch_retry_does_not_repeat_full_pass(harness):
    current, calls, enqueue = harness
    current["db"] = FakeSession(phase="completed")
    run("identity.resolve_batch", {"batch_id": 4}, 10)
    assert calls == ["lock"]
    enqueue.assert_not_called()


def test_attachment_sql_terminal_no_match_ambiguity_and_privacy(monkeypatch):
    from app.identity import attachments
    db = FakeSession()
    monkeypatch.setattr(attachments, "acquire_resolver_lock",
                        lambda session: session.statements.append("privacy_then_identity_lock"))
    attachments.attach_transactions(db)
    assert db.statements[0] == "privacy_then_identity_lock"
    sql = ";\n".join(db.statements[1:])
    assert sql.count("UPDATE gifts ") == sql.count("UPDATE events ") == 1
    assert "count(DISTINCT profile_id)=1" in sql
    assert "NOT owner.is_deleted AND owner.merged_into_id IS NULL" in sql
    assert "identifier_blocklist" in sql
    assert "LEFT JOIN winners" in sql
    assert "SET profile_id=a.profile_id,resolved_at=now()" in sql
    assert "(0, 'external'" in sql and "(1, 'email'" in sql and "(1, 'phone'" in sql


def test_attachment_reports_each_statement_without_changing_sql_or_committing(monkeypatch):
    from app.identity import attachments
    db = FakeSession()
    monkeypatch.setattr(attachments, "acquire_resolver_lock", lambda session: None)
    events = []
    attachments.attach_transactions(db, lambda *event: events.append(event))
    steps = ["drop_assignment", "assignment", "index_assignment", "analyze_assignment",
             "gifts", "events", "source_records", "dirty_profiles"]
    assert [(step, status) for step, status, elapsed in events] == [
        (step, status) for step in steps for status in ("running", "completed")
    ]
    assert all(elapsed >= 0 for step, status, elapsed in events)
    assert len(db.statements) == 8 and db.commits == 0


def test_attachment_reports_failed_statement_and_leaves_rollback_to_caller(monkeypatch):
    from app.identity import attachments
    db = FakeSession()
    execute = db.execute

    def fail_gifts(statement, params=None):
        if str(statement).startswith("UPDATE gifts"):
            raise ValueError("controlled failure")
        execute(statement, params)

    db.execute = fail_gifts
    monkeypatch.setattr(attachments, "acquire_resolver_lock", lambda session: None)
    events = []
    with pytest.raises(ValueError, match="controlled failure"):
        attachments.attach_transactions(db, lambda *event: events.append(event))
    assert events[-1][:2] == ("gifts", "failed")
    assert not any(str(sql).startswith("UPDATE events") for sql in db.statements)
    assert db.commits == 0


def test_seed_publishes_jobs_only_after_registration_loop():
    import ast
    from app.importer import import_seed_files
    tree = ast.parse(inspect.getsource(import_seed_files))
    outer_loop = next(n for n in tree.body[0].body if isinstance(n, ast.For))
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "enqueue" for n in ast.walk(outer_loop))
    assert "set(files) != set(SEED_SOURCES)" in inspect.getsource(import_seed_files)


def test_both_graph_entry_points_exclude_transactional_records():
    from app.identity.bulk import resolve_bulk
    from app.identity.resolver import resolve_batch
    for entry_point in (resolve_bulk, resolve_batch):
        assert "COALESCE(i.record_type,'contact') NOT IN ('gift','event')" in inspect.getsource(entry_point)
