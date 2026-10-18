"""Existing-container benchmark boundaries, exercised only in disposable PG."""
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import stat
import re
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.schema_benchmark import assert_private, isolated_schema, normalized_url, child_environment, CHILD_BOOTSTRAP


def disposable_test_url(value):
    url = normalized_url(value)
    socket = str(url.query.get("host", ""))
    if (url.drivername != "postgresql+psycopg" or url.host or url.port
            or url.database != "benchmark" or url.username != "benchmark"
            or set(url.query) != {"host"}
            or not re.fullmatch(r"/tmp/kinship-bench-[A-Za-z0-9_-]+", socket)):
        raise ValueError("Private benchmark socket and database identity required")
    directory = Path(socket)
    info = directory.stat()
    if directory.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Private benchmark socket ownership required")
    if not stat.S_ISSOCK((directory / ".s.PGSQL.5432").stat().st_mode):
        raise ValueError("Private PostgreSQL socket required")
    return url


@pytest.fixture
def admin():
    url = os.environ.get("CONSENT_TEST_DATABASE_URL")
    if os.environ.get("KINSHIP_BENCHMARK_ISOLATED_TESTS") != "1" or not url:
        pytest.skip("Disposable PostgreSQL harness required")
    if any(key.startswith("PG") for key in os.environ):
        pytest.skip("Disposable harness must clear libpq environment overrides")
    private_url = disposable_test_url(url)
    engine = create_engine(private_url, hide_parameters=True)
    with engine.connect() as db:
        assert tuple(db.execute(text("SELECT current_database(),current_user,inet_server_addr()")).one()) == ("benchmark", "benchmark", None)
    yield engine, url
    engine.dispose()


@pytest.mark.parametrize("url", [
    "postgresql://benchmark@localhost/benchmark",
    "postgresql://benchmark@/production?host=/tmp/kinship-bench-fixture",
    "postgresql://admin@/benchmark?host=/tmp/kinship-bench-fixture",
    "postgresql://benchmark@/benchmark?host=/tmp/kinship-bench-fixture&service=live",
    "postgresql://benchmark@/benchmark?host=/tmp/kinship-bench-fixture/../live",
])
def test_guard_rejects_nonprivate_test_url(url):
    with pytest.raises(ValueError, match="Private benchmark"):
        disposable_test_url(url)


def test_child_environment_excludes_unrelated_secrets(monkeypatch, tmp_path):
    for key in ("OIDC_CLIENT_SECRET", "AWS_SECRET_ACCESS_KEY", "CONSENT_TEST_DATABASE_URL",
                "AH_WORKER_RECOVERY_TEST_DATABASE_URL", "PGPASSWORD", "CUSTOM_CLOUD_TOKEN"):
        monkeypatch.setenv(key, "production-secret")
    env = child_environment("private", "restricted-url", tmp_path)
    assert "production-secret" not in env.values()
    assert env["DATABASE_URL"] == "restricted-url"
    assert env["APP_ENV"] == "test"


def test_child_bootstrap_ignores_dotenv(tmp_path):
    (tmp_path / ".env").write_text("OIDC_CLIENT_SECRET=dotenv-production-secret\n")
    env = child_environment("private", "postgresql://restricted@localhost/private", tmp_path)
    backend = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([backend, env.get("PYTHONPATH", "")])
    result = subprocess.run([sys.executable, "-c", CHILD_BOOTSTRAP +
                             "assert Settings().oidc_client_secret == ''"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_interrupt_at_commit_still_cleans_exact_objects(monkeypatch):
    statements = []
    connections = []

    class Connection:
        def scalar(self, query, params=None):
            return "rolsuper" in str(query)

        def execute(self, query, params=None):
            if "pg_terminate_backend" in str(query):
                # Repeated termination must not interrupt cleanup.
                os.kill(os.getpid(), signal.SIGTERM)
            return SimpleNamespace(scalars=lambda: ["citext", "pg_trgm"])

        def exec_driver_sql(self, query):
            statements.append(query)

    class Engine:
        @contextmanager
        def begin(self):
            connections.append(1)
            yield Connection()
            if len(connections) == 1:
                raise KeyboardInterrupt("interrupted at commit")

        def dispose(self):
            pass

    def engine_factory(url, **kwargs):
        assert kwargs["connect_args"]["connect_timeout"] == 10
        assert "statement_timeout=30000" in kwargs["connect_args"]["options"]
        return Engine()

    monkeypatch.setattr("app.schema_benchmark.create_engine", engine_factory)
    receipt = {}
    with pytest.raises(KeyboardInterrupt):
        with isolated_schema("postgresql://fixture@localhost/fixture", receipt):
            pytest.fail("Provisioning interrupted")
    assert receipt["cleanup"] == "complete"
    assert statements[-2:] == [
        f'DROP SCHEMA IF EXISTS "{receipt["schema"]}" CASCADE',
        f'DROP ROLE IF EXISTS "{receipt["schema"]}"',
    ]


@pytest.mark.parametrize("interrupt", [False, True])
def test_restricted_role_sentinel_and_cleanup(admin, interrupt):
    engine, url = admin
    with engine.begin() as db:
        db.execute(text("CREATE TABLE public.benchmark_live_sentinel(value text)"))
        db.execute(text("INSERT INTO public.benchmark_live_sentinel VALUES ('live')"))
    role = None
    try:
        try:
            with isolated_schema(url) as (role, private_url):
                private = create_engine(private_url)
                with private.begin() as db:
                    assert_private(db, role)
                    # There is deliberately no private table: public fallback
                    # must fail, even if fully qualified by future code.
                    for query in (
                        "SELECT * FROM benchmark_live_sentinel",
                        "UPDATE public.benchmark_live_sentinel SET value='bad'",
                        "DROP TABLE public.benchmark_live_sentinel",
                        "SET ROLE benchmark",
                    ):
                        with pytest.raises(Exception):
                            with db.begin_nested():
                                db.execute(text(query))
                    db.execute(text("CREATE TABLE private_sentinel(value text)"))
                    db.execute(text("INSERT INTO private_sentinel VALUES ('private')"))
                    # Session and transaction locks get a private namespace.
                    db.execute(text("SELECT pg_advisory_xact_lock(123::bigint)"))
                    db.execute(text("SELECT pg_advisory_xact_lock(5,6)"))
                    ns = db.scalar(text("SELECT oid FROM pg_namespace WHERE nspname=:n"), {"n": role})
                    assert db.scalar(text("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory' AND classid=:n"), {"n": ns}) == 2
                private.dispose()
                if interrupt:
                    raise KeyboardInterrupt()
        except KeyboardInterrupt:
            assert interrupt
        with engine.connect() as db:
            assert db.scalar(text("SELECT value FROM public.benchmark_live_sentinel")) == "live"
            assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:n)"), {"n": role})
            assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:n)"), {"n": role})
    finally:
        with engine.begin() as db:
            db.execute(text("DROP TABLE public.benchmark_live_sentinel"))


def test_public_grants_fail_closed(admin):
    engine, url = admin
    with engine.begin() as db:
        db.execute(text("CREATE TABLE public.benchmark_unsafe(value text)"))
        db.execute(text("GRANT SELECT ON public.benchmark_unsafe TO PUBLIC"))
    try:
        with pytest.raises(RuntimeError, match="Unsafe PUBLIC privileges"):
            with isolated_schema(url):
                pytest.fail("Must refuse access to live tables")
    finally:
        with engine.begin() as db:
            db.execute(text("DROP TABLE public.benchmark_unsafe"))


@pytest.mark.parametrize("terminate", [False, True])
@pytest.mark.parametrize("mode", ["traits", "segments"])
def test_child_failure_or_sigterm_cleans_owned_schema(admin, tmp_path, monkeypatch, terminate, mode):
    from app.schema_benchmark import run_schema_benchmark
    engine, url = admin
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(database_url=url))
    monkeypatch.setenv("KINSHIP_BENCHMARK_OUTPUT", str(tmp_path))
    child_state = {"terminated": False}

    class Child:
        def wait(self, timeout=None):
            if terminate and not child_state["terminated"]:
                os.kill(os.getpid(), signal.SIGTERM)
            return 1

        def poll(self):
            return None if terminate else 1

        def terminate(self):
            child_state["terminated"] = True

    monkeypatch.setattr("app.schema_benchmark.subprocess.Popen", lambda *args, **kwargs: Child())
    args = SimpleNamespace(traits=mode == "traits", segments=mode == "segments",
                           isolated=True,
                           imports=False, dashboards=False, overview=False, scale="small",
                           test_suite=False, profiles=1500, output_dir=str(tmp_path), seed_dir=None)
    with pytest.raises(KeyboardInterrupt if terminate else RuntimeError):
        run_schema_benchmark(args)
    receipt = json.loads(next(tmp_path.glob("*/isolation.json")).read_text())
    assert receipt["cleanup"] == "complete"
    with engine.connect() as db:
        assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:s)"), {"s": receipt["schema"]})
        assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:s)"), {"s": receipt["schema"]})
    if terminate:
        assert child_state["terminated"]


def test_production_cli_smoke_keeps_public_untouched(admin, tmp_path):
    """Production-configured segments use existing data, never schema fixtures.

    Restricted-role provisioning and interruption cleanup are exercised above;
    this smoke checks the new default rather than running a trait/seed oracle.
    """
    from test_readonly_benchmark_pg import durable_counts

    engine, url = admin
    before = durable_counts(engine)
    with engine.connect() as db:
        roles = set(db.execute(text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'kinship_bench_%'")).scalars())
        schemas = set(db.execute(text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'kinship_bench_%'")).scalars())
    env = dict(os.environ, DATABASE_URL=url, APP_ENV="production", AUTH_MODE="oidc",
               OIDC_ISSUER="https://oidc.invalid", OIDC_CLIENT_ID="fixture",
               OIDC_CLIENT_SECRET="fixture", PUBLIC_BASE_URL="https://fixture.invalid",
               SECRET_KEY="x" * 40, KINSHIP_BENCHMARK_BACKEND="schema",
               KINSHIP_BENCHMARK_OUTPUT=str(tmp_path))
    result = subprocess.run(["kinship", "benchmark", "--segments",
                             "--profiles", "500000", "--as-of", "2025-01-01"],
                            env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=120)
    assert result.returncode == 0, result.stdout[-10000:]
    output = next(tmp_path.glob("segments-readonly-*/segments.json"))
    report = json.loads(output.read_text())
    assert report["status"] == "passed"
    assert report["mode"] == "existing-app-database-readonly"
    assert report["read_only"] is True
    assert report["population"]["profiles"] == before["public.profiles"]
    assert report["profiles_option_ignored"] == 500000
    assert report["as_of"] == "2025-01-01T00:00:00+00:00"
    assert len(report["templates"]) == 23
    assert report["materialization"]["status"] == "skipped"
    assert all(row["status"] == "passed"
               and row["materialization"]["status"] == "skipped"
               for row in report["templates"])
    assert not list(tmp_path.glob("traits-*"))
    assert not list(tmp_path.glob("**/isolation.json"))
    assert not list(tmp_path.glob("**/seed"))
    assert durable_counts(engine) == before
    with engine.connect() as db:
        assert set(db.execute(text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'kinship_bench_%'")).scalars()) == roles
        assert set(db.execute(text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'kinship_bench_%'")).scalars()) == schemas


def test_actual_cli_sigterm_during_workload_cleans_up(admin, tmp_path):
    engine, url = admin
    env = dict(os.environ, DATABASE_URL=url, KINSHIP_BENCHMARK_BACKEND="schema",
               KINSHIP_BENCHMARK_OUTPUT=str(tmp_path))
    with (tmp_path / "cli.txt").open("w") as output:
        process = subprocess.Popen([sys.executable, "-m", "app.cli", "benchmark",
                                    "--traits", "--profiles", "1500"],
                                   env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 45
            while not list(tmp_path.glob("traits-*/traits.json")):
                assert process.poll() is None, (tmp_path / "cli.txt").read_text()
                assert time.monotonic() < deadline, "Child did not enter real workload"
                time.sleep(0.02)
            process.send_signal(signal.SIGTERM)
            time.sleep(0.05)
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=45) != 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=45)
    receipt = json.loads(next(tmp_path.glob("traits-*/isolation.json")).read_text())
    assert receipt["cleanup"] == "complete"
    with engine.connect() as db:
        assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:s)"), {"s": receipt["schema"]})
        assert not db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:s)"), {"s": receipt["schema"]})