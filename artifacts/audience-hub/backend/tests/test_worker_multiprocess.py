"""Worker supervision and leader election tests do not use a database."""
import importlib.util
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace

from app import worker
from app.jobs.scheduler import SchedulerLeader


def recovery_probe():
    path = Path(__file__).resolve().parents[2] / "scripts" / "manual_worker_recovery.py"
    spec = importlib.util.spec_from_file_location("manual_worker_recovery", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeConnection:
    def __init__(self, state):
        self.state = state
        self.locked = False
        self.closed = False
        self.invalidated = False

    def execution_options(self, **kwargs):
        assert kwargs == {"isolation_level": "AUTOCOMMIT"}
        return self

    def execute(self, statement, params=None):
        query = str(statement)
        if "pg_try_advisory_lock" in query:
            assert params["key"]
            acquired = self.state["owner"] is None
            if acquired:
                self.state["owner"] = self
                self.locked = True
            return SimpleNamespace(scalar_one=lambda: acquired)
        if "pg_advisory_unlock" in query:
            assert self.locked
            self.locked = False
            self.state["owner"] = None
        else:
            assert query == "SELECT 1"
        return SimpleNamespace()

    def close(self):
        self.closed = True
        assert not self.locked, "Never return a locked connection to the pool"

    def invalidate(self):
        self.invalidated = True


def test_only_one_scheduler_leader_and_failover():
    state = {"owner": None, "connections": []}

    class Engine:
        def connect(self):
            connection = FakeConnection(state)
            state["connections"].append(connection)
            return connection

    first, second = SchedulerLeader(), SchedulerLeader()
    engine = Engine()
    assert first.ensure(engine)
    assert first.ensure(engine)  # health check must not acquire the same lock again
    assert not second.ensure(engine)
    assert state["connections"][1].closed
    first.close()
    assert second.ensure(engine)
    second.close()
    assert state["owner"] is None


def test_maintenance_only_schedules_for_leader(monkeypatch):
    calls = []

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def commit(self):
            calls.append("commit")

    monkeypatch.setattr(worker, "Session", lambda engine: Db())
    monkeypatch.setattr(worker.queue, "requeue_stale",
                        lambda db, stale_after: calls.append(stale_after.total_seconds()))
    monkeypatch.setattr(worker.scheduler, "tick", lambda db: calls.append("tick"))
    worker.maintenance(SimpleNamespace(ensure=lambda engine: False), 15)
    assert calls == [15, "commit"]
    worker.maintenance(SimpleNamespace(ensure=lambda engine: True), 15)
    assert calls == [15, "commit", 15, "commit", "tick", "commit"]


def test_child_finishes_job_before_claiming_next(monkeypatch):
    events = []
    handlers = {}

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def commit(self):
            events.append("commit")

    def claim(db):
        job_id = len([event for event in events if event[0] == "claim"]) + 1
        events.append(("claim", job_id))
        return SimpleNamespace(id=job_id, type="noop", payload={})

    def run_job(job_id, job_type, payload):
        events.append(("run", job_id))
        if job_id == 2:
            handlers[worker.signal.SIGTERM](None, None)

    monkeypatch.setattr(worker, "Session", lambda engine: Db())
    monkeypatch.setattr(worker.queue, "claim", claim)
    monkeypatch.setattr(worker, "run_job", run_job)
    monkeypatch.setattr(worker.signal, "signal", lambda signum, callback: handlers.__setitem__(signum, callback))
    monkeypatch.setattr(worker.engine, "dispose", lambda: None)
    monkeypatch.setattr(worker, "configure_logging", lambda level: None)
    worker.child_main()
    assert events == [
        ("claim", 1), "commit", ("run", 1),
        ("claim", 2), "commit", ("run", 2),
    ]


def test_dead_child_is_reaped_and_replaced_without_exceeding_concurrency():
    class Process:
        def __init__(self, target):
            assert target is worker.child_main
            self.alive = False
            self.joined = False
            self.pid = 123
            self.exitcode = 1

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self):
            self.joined = True

    context = SimpleNamespace(Process=Process)
    children = []
    worker.replenish_children(children, 2, context)
    assert len(children) == 2
    dead = children[0]
    dead.alive = False
    worker.replenish_children(children, 2, context)
    assert dead.joined and dead not in children
    assert len(children) == 2 and all(child.is_alive() for child in children)


def test_graceful_shutdown_releases_leader_first_and_does_not_requeue(monkeypatch):
    events = []
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100)
    monkeypatch.setattr(worker.queue, "requeue_stale",
                        lambda *args, **kwargs: events.append("UNSAFE immediate requeue"))
    leader = SimpleNamespace(close=lambda: events.append("unlock"))

    class Child:
        pid = 1

        def __init__(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            events.append("term")

        def join(self, timeout):
            events.append(("join", timeout))
            self.alive = False  # handler completes and records success

        def kill(self):
            events.append("kill")

    worker.stop_children([Child()], leader, 25)
    assert events == ["unlock", "term", ("join", 25)]


def test_shutdown_escalates_at_global_deadline_without_requeue(monkeypatch):
    clock = iter([0, 4, 10, 10, 10, 11])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    events = []
    monkeypatch.setattr(worker.queue, "requeue_stale",
                        lambda *args, **kwargs: events.append("UNSAFE immediate requeue"))

    class Child:
        def __init__(self, pid):
            self.pid = pid
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            events.append(("term", self.pid))

        def join(self, timeout):
            events.append(("join", self.pid, timeout))
            if not self.alive:
                return
            # A hung handler ignores the child's graceful SIGTERM signal.

        def kill(self):
            events.append(("kill", self.pid))
            self.alive = False

    worker.stop_children([Child(1), Child(2)],
                         SimpleNamespace(close=lambda: events.append("unlock")), 10)
    assert events == [
        "unlock", ("term", 1), ("term", 2),
        ("join", 1, 6), ("join", 2, 0),
        ("kill", 1), ("kill", 2),
        ("join", 1, 5), ("join", 2, 4),
    ]


def test_recovery_probe_crashes_only_its_own_spawn_tree(monkeypatch):
    probe = recovery_probe()
    launches = []
    monkeypatch.setattr(probe.subprocess, "Popen",
                        lambda command, **kwargs: launches.append((command, kwargs)) or "owned")
    assert probe.worker_process({"WORKER_CONCURRENCY": "1"}) == "owned"
    assert launches[0][1]["start_new_session"] is True

    signals = []
    monkeypatch.setattr(probe.os, "killpg",
                        lambda pid, signum: signals.append((pid, signum)))

    class OwnedProcess:
        pid = 12345

        def wait(self, timeout):
            assert timeout == 10

    probe.stop_owned_worker(OwnedProcess(), crash=True)
    assert signals == [(12345, signal.SIGKILL)]


def test_recovery_probe_graceful_cleanup_escalates_owned_group(monkeypatch):
    probe = recovery_probe()
    signals = []
    monkeypatch.setattr(probe.os, "killpg",
                        lambda pid, signum: signals.append((pid, signum)))

    class HungOwnedProcess:
        pid = 23456
        attempts = 0

        def wait(self, timeout):
            self.attempts += 1
            if self.attempts == 1:
                raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout)

    process = HungOwnedProcess()
    probe.stop_owned_worker(process)
    assert process.attempts == 2
    assert signals == [(23456, signal.SIGTERM), (23456, signal.SIGKILL)]