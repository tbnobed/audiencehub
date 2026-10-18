"""Small dispatch/measurement tests; none connects to the configured app DB."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import pytest

from app.benchmark import add_arguments, run_benchmark
from app.readonly_benchmark import reference_instant


def arguments(*flags):
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    return parser.parse_args(flags)


@pytest.mark.parametrize("backend", ["schema", "local"])
@pytest.mark.parametrize("profiles", [None, "500000", "0"])
def test_segments_default_readonly_even_in_production(monkeypatch, backend, profiles):
    calls = []
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("KINSHIP_BENCHMARK_BACKEND", backend)
    monkeypatch.setattr("app.readonly_benchmark.run_readonly_segments",
                        lambda args: calls.append(args) or 17)
    monkeypatch.setattr("app.schema_benchmark.run_schema_benchmark",
                        lambda args: pytest.fail("must not provision production schema"))
    flags = ["--segments"]
    if profiles is not None:
        flags += ["--profiles", profiles]
    assert run_benchmark(arguments(*flags)) == 17
    assert len(calls) == 1
    assert not calls[0].isolated


@pytest.mark.parametrize("flags", [
    ["--seed-dir", "/private/fixture"], ["--seed-dir", ""],
    ["--scale", "medium"], ["--scale", "small"], ["--rows", "0"],
    ["--gift-rows", "1000000"], ["--contact-rows", "500000"],
    ["--profile"], ["--test-filter", "fixture"],
])
def test_readonly_rejects_synthetic_knobs_before_settings(monkeypatch, flags):
    monkeypatch.setenv("KINSHIP_BENCHMARK_BACKEND", "schema")
    monkeypatch.setattr("app.config.get_settings",
                        lambda: pytest.fail("invalid CLI must not load settings or connect"))
    with pytest.raises(ValueError, match="requires --isolated"):
        run_benchmark(arguments("--segments", *flags))


@pytest.mark.parametrize("flag", ["--traits", "--imports", "--dashboards", "--overview", "--test-suite"])
def test_readonly_rejects_mixed_modes(flag):
    with pytest.raises(ValueError, match="cannot be combined"):
        run_benchmark(arguments("--segments", flag))


@pytest.mark.parametrize("flags", [
    ["--segments", "--isolated"], ["--audit-segments"],
    ["--segments", "--audit-segments"],
])
def test_explicit_isolation_and_audit_keep_schema_backend(monkeypatch, flags):
    seen = []
    monkeypatch.setenv("KINSHIP_BENCHMARK_BACKEND", "schema")
    monkeypatch.setattr("app.schema_benchmark.run_schema_benchmark",
                        lambda args: seen.append(args) or 0)
    monkeypatch.setattr("app.readonly_benchmark.run_readonly_segments",
                        lambda args: pytest.fail("audit/isolation must not use app data"))
    assert run_benchmark(arguments(*flags)) == 0
    assert seen[0].segments and seen[0].profiles == 500_000 and seen[0].scale == "medium"


def test_direct_schema_entry_cannot_bypass_readonly_default(monkeypatch):
    from app.schema_benchmark import run_schema_benchmark
    monkeypatch.setattr("app.readonly_benchmark.run_readonly_segments", lambda args: 31)
    assert run_schema_benchmark(arguments("--segments")) == 31


def test_explicit_local_isolation_retains_production_refusal(monkeypatch):
    monkeypatch.delenv("KINSHIP_BENCHMARK_BACKEND", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValueError, match="forbidden"):
        run_benchmark(arguments("--segments", "--isolated"))


@pytest.mark.parametrize("failure", ["initdb", "workload", "stop"])
def test_local_isolated_failures_clean_private_directories(monkeypatch, tmp_path, failure):
    import app.benchmark as benchmark

    monkeypatch.delenv("KINSHIP_BENCHMARK_BACKEND", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(benchmark.shutil, "which", lambda binary: "/fixture/" + binary)
    real_mkdtemp = benchmark.tempfile.mkdtemp
    created = []

    def mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(benchmark.Path(path))
        return path

    def run(command, **kwargs):
        if command[0] == "initdb":
            benchmark.Path(command[command.index("-D") + 1]).mkdir()
            for child in ("seed", "uploads", "exports"):
                (created[0] / child).mkdir()
        if command[0] == "initdb" and failure == "initdb":
            raise RuntimeError("initdb failed")
        if command[0] == "pg_ctl" and "stop" in command and failure == "stop":
            raise RuntimeError("stop failed")
        if command[0] == benchmark.sys.executable and "-c" in command:
            if failure == "workload":
                raise RuntimeError("workload failed")
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0)

    from types import SimpleNamespace
    monkeypatch.setattr(benchmark.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(benchmark.subprocess, "run", run)
    monkeypatch.setattr("app.readonly_benchmark.run_readonly_segments",
                        lambda args: pytest.fail("explicit local isolation must not read app data"))
    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        run_benchmark(arguments("--segments", "--isolated", "--output-dir", str(tmp_path)))
    # The compact evidence directory remains; its private socket and database
    # artifacts do not. This uses fake subprocesses, never starts PostgreSQL.
    assert len(created) == 2
    assert created[0].exists()
    assert not created[1].exists()
    assert not (created[0] / "postgres").exists()
    assert all(not (created[0] / child).exists() for child in ("seed", "uploads", "exports"))


def test_pinned_reference_is_utc_and_only_sampled_once(monkeypatch):
    import app.readonly_benchmark as benchmark

    class Clock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            cls.calls += 1
            return cls(2031, 1, 2, 5, 6, 7, tzinfo=tz)

    monkeypatch.setattr(benchmark, "datetime", Clock)
    assert benchmark.reference_instant().isoformat() == "2031-01-02T05:06:07+00:00"
    assert Clock.calls == 1
    assert reference_instant("2028-02-29").isoformat() == "2028-02-29T00:00:00+00:00"
    assert reference_instant("2028-03-01T01:00:00+02:00").isoformat() == "2028-02-29T23:00:00+00:00"
    with pytest.raises(ValueError):
        reference_instant("invalid")


def test_all_23_code_templates_first_and_warm_without_write_services(monkeypatch, tmp_path):
    import app.readonly_benchmark as benchmark

    compiled_instants = []

    class Query:
        def subquery(self):
            from sqlalchemy import literal, select
            return select(literal(1).label("id")).subquery()

    class DB:
        def scalar(self, statement):
            sql = str(statement)
            if "active_profiles" in sql:
                return 11
            if "FROM profiles" in sql:
                return 13
            return 2

        @contextmanager
        def begin_nested(self):
            yield

    @contextmanager
    def session(engine):
        yield DB()

    def compile_query(db, definition, instant):
        compiled_instants.append(instant)
        return Query()

    monkeypatch.setattr(benchmark, "readonly_session", session)
    monkeypatch.setattr("app.segments.compiler.compile_definition", compile_query)
    monkeypatch.setattr(benchmark, "_sample", lambda db, definition, instant: {
        "rows": 2, "status_total": 2, "source_groups": 0})
    for name in ("ensure_templates", "materialize", "request_materialization"):
        monkeypatch.setattr("app.segments.service." + name,
                            lambda *args, **kwargs: pytest.fail("read-only must not write"))
    report = benchmark.measure_existing(None, tmp_path, as_of="2028-02-29", profiles_option=500000)
    assert report["status"] == "passed"
    assert report["profiles"] == 11
    assert report["population"] == {"profiles": 13, "active_profiles": 11}
    assert report["profiles_option_ignored"] == 500000
    assert len(report["templates"]) == 23
    assert {instant.isoformat() for instant in compiled_instants} == {"2028-02-29T00:00:00+00:00"}
    for row in report["templates"]:
        assert row["count"] == 2
        assert row["materialization"]["status"] == "skipped"
        assert "read-only" in row["materialization"]["reason"]
        for metric in ("count_latency", "preview_latency", "sample_latency"):
            assert row[metric]["first_ms"] >= 0
            assert len(row[metric]["warm_ms"]) == 3
            assert row[metric]["warm_median_ms"] >= 0
    assert json.loads((tmp_path / "segments.json").read_text())["status"] == "passed"


def test_failed_template_is_explicit_and_rest_still_measured(monkeypatch, tmp_path):
    import app.readonly_benchmark as benchmark
    from sqlalchemy import literal, select

    class DB:
        def scalar(self, statement):
            return 0

        @contextmanager
        def begin_nested(self):
            yield

    @contextmanager
    def session(engine):
        yield DB()

    state = {"calls": 0}

    def compile_query(*args):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("private SQL/parameter must not be exposed")
        return select(literal(1).label("id"))

    monkeypatch.setattr(benchmark, "readonly_session", session)
    monkeypatch.setattr("app.segments.compiler.compile_definition", compile_query)
    monkeypatch.setattr(benchmark, "_sample", lambda *args: {"rows": 0, "status_total": 0})
    report = benchmark.measure_existing(None, tmp_path)
    assert report["status"] == "failed" and len(report["templates"]) == 23
    assert report["templates"][0]["error"] == "RuntimeError: template query failed"
    assert all(row["status"] == "passed" for row in report["templates"][1:])
    assert "private SQL" not in (tmp_path / "segments.json").read_text()


def test_readonly_engine_no_app_engine_or_worker_hooks(monkeypatch):
    import app.readonly_benchmark as benchmark
    captured = {}

    def factory(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return "dedicated-engine"

    monkeypatch.setattr(benchmark, "create_engine", factory)
    assert benchmark.readonly_engine("postgresql://reader@localhost/app") == "dedicated-engine"
    assert "default_transaction_read_only=on" in captured["connect_args"]["options"]
    assert captured["url"].drivername == "postgresql+psycopg"
    assert captured["poolclass"].__name__ == "NullPool"
    with pytest.raises(ValueError, match="PostgreSQL"):
        benchmark.readonly_engine("sqlite:///fixture")


def test_readonly_runner_closes_engine_on_measurement_failure(monkeypatch, tmp_path):
    import app.readonly_benchmark as benchmark
    from types import SimpleNamespace

    state = {"disposed": False}
    engine = SimpleNamespace(dispose=lambda: state.update(disposed=True))
    monkeypatch.setattr("app.config.get_settings",
                        lambda: SimpleNamespace(database_url="fixture"))
    monkeypatch.setattr(benchmark, "readonly_engine", lambda url: engine)
    monkeypatch.setenv("KINSHIP_BENCHMARK_OUTPUT", str(tmp_path))

    def fail(*args, **kwargs):
        raise RuntimeError("measurement failed")

    monkeypatch.setattr(benchmark, "measure_existing", fail)
    with pytest.raises(RuntimeError, match="measurement failed"):
        benchmark.run_readonly_segments(arguments("--segments"))
    assert state["disposed"]


def test_isolated_cleanup_failure_is_explicit_and_admin_is_disposed(monkeypatch):
    from types import SimpleNamespace
    from app.schema_benchmark import isolated_schema

    state = {"connections": 0, "disposed": False}

    class Connection:
        def scalar(self, query, params=None):
            return "rolsuper" in str(query)

        def execute(self, query, params=None):
            if "pg_terminate_backend" in str(query):
                raise RuntimeError("cleanup unavailable")
            return SimpleNamespace(scalars=lambda: ["citext", "pg_trgm"])

        def exec_driver_sql(self, statement):
            pass

    class Engine:
        @contextmanager
        def begin(self):
            state["connections"] += 1
            yield Connection()
            if state["connections"] == 1:
                raise RuntimeError("provision commit interrupted")

        def dispose(self):
            state["disposed"] = True

    monkeypatch.setattr("app.schema_benchmark.create_engine", lambda *args, **kwargs: Engine())
    receipt = {}
    with pytest.raises(RuntimeError, match="cleanup unavailable"):
        with isolated_schema("postgresql://fixture@localhost/fixture", receipt):
            pytest.fail("must not run after provision failure")
    assert receipt["cleanup"] == "failed"
    assert receipt["cleanup_error"] == "RuntimeError"
    assert state["disposed"]
