"""Privileged provisioning, unprivileged execution of disposable schema benchmarks.

No application table is accessed by the provisioning connection. A fresh login
role owns only this invocation's schema; inherited PUBLIC grants are checked
before any migrations or benchmark code runs. There is no unsafe fallback.
"""
from contextlib import contextmanager
import base64
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

NAME = re.compile(r"^kinship_bench_[0-9a-f]{24}$")
RUNTIME_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONUSERBASE", "LD_LIBRARY_PATH")
# This runs only in the restricted child, before any settings or engine exists.
# In particular, a mounted production .env must not undo env sanitization.
CHILD_BOOTSTRAP = "from app.config import Settings; Settings.model_config['env_file']=None; "


def child_environment(schema, url, directory):
    env = {key: os.environ[key] for key in RUNTIME_ENV if key in os.environ}
    env.update(DATABASE_URL=url, APP_ENV="test", AUTH_MODE="dev",
               SECRET_KEY=secrets.token_hex(32), PII_HASH_PEPPER=secrets.token_hex(32),
               FERNET_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
               UPLOAD_DIR=str(directory / "uploads"), EXPORT_DIR=str(directory / "exports"),
               KINSHIP_BENCHMARK_SCHEMA=schema, KINSHIP_BENCHMARK_ISOLATED_TESTS="1")
    return env


@contextmanager
def ignore_termination():
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, signal.SIG_IGN)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def normalized_url(value):
    return make_url(value.replace("postgres://", "postgresql+psycopg://", 1)
                    .replace("postgresql://", "postgresql+psycopg://", 1))


def assert_private(connection, schema):
    if not NAME.fullmatch(schema):
        raise RuntimeError("Invalid benchmark schema")
    actual = connection.execute(text(
        "SELECT current_user,current_schema(),rolsuper,rolcreatedb,rolcreaterole,rolbypassrls "
        "FROM pg_roles WHERE rolname=current_user")).one()
    if actual[0] != schema or actual[1] != schema or any(actual[2:]):
        raise RuntimeError("Benchmark requires its own unprivileged schema owner")
    unsafe = connection.scalar(text("""
        SELECT EXISTS (
          SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname NOT IN (:schema,'pg_catalog','information_schema')
            AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'
            AND c.relkind IN ('r','p','v','m','f')
            AND has_table_privilege(c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        ) OR EXISTS (
          SELECT 1 FROM pg_namespace n
          WHERE n.nspname NOT IN (:schema,'pg_catalog','information_schema')
            AND n.nspname NOT LIKE 'pg_temp_%'
            AND has_schema_privilege(n.oid,'CREATE')
        ) OR EXISTS (
          SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE p.prosecdef AND n.nspname NOT IN (:schema,'pg_catalog','information_schema')
            AND has_function_privilege(p.oid,'EXECUTE')
        ) OR EXISTS (
          SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE c.relkind='S' AND n.nspname NOT IN (:schema,'pg_catalog')
            AND CASE WHEN c.relkind='S' THEN has_sequence_privilege(c.oid,'USAGE,SELECT,UPDATE') ELSE false END
        ) OR EXISTS (
          SELECT 1 FROM pg_foreign_server WHERE has_server_privilege(oid,'USAGE')
        ) OR has_database_privilege(current_database(),'CREATE')
          OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user))
    """), {"schema": schema})
    if unsafe:
        raise RuntimeError("Unsafe PUBLIC privileges: benchmark role can access objects outside its schema; no benchmark was run")


def install_private_locks(connection, schema):
    """Keep benchmark advisory locks out of the production lock namespace."""
    namespace = connection.scalar(text("SELECT oid FROM pg_namespace WHERE nspname=:s"), {"s": schema})
    if namespace >= 2**31 or namespace in (0, 0x414849, 0x41484944):
        raise RuntimeError("Unsupported benchmark advisory-lock namespace")
    for function in ("pg_advisory_xact_lock", "pg_advisory_xact_lock_shared",
                     "pg_try_advisory_xact_lock", "pg_try_advisory_xact_lock_shared",
                     "pg_advisory_lock", "pg_try_advisory_lock", "pg_advisory_unlock"):
        returns = "boolean" if "try" in function or "unlock" in function else "void"
        for signature, value in (("bigint", "$1::text"), ("integer,integer", "$1::text || ':' || $2::text")):
            connection.exec_driver_sql(
                f'CREATE FUNCTION "{schema}".{function}({signature}) RETURNS {returns} '
                f'LANGUAGE sql AS $$ SELECT pg_catalog.{function}({namespace}::integer, '
                f'pg_catalog.hashtext({value})) $$')


@contextmanager
def isolated_schema(url, cleanup_state=None):
    admin_url = normalized_url(str(url))
    admin_options = admin_url.query.get("options", "")
    admin = create_engine(admin_url, hide_parameters=True, connect_args={
        "connect_timeout": 10,
        "options": f"{admin_options} -cstatement_timeout=30000 -clock_timeout=10000",
    })
    schema = "kinship_bench_" + secrets.token_hex(12)
    password = secrets.token_hex(32)
    cleanup_eligible = False
    try:
        with admin.begin() as connection:
            if not connection.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")):
                raise RuntimeError("Schema benchmark provisioning requires a PostgreSQL superuser; no unsafe fallback")
            extensions = set(connection.execute(text(
                "SELECT extname FROM pg_extension WHERE extname IN ('citext','pg_trgm')")).scalars())
            if extensions != {"citext", "pg_trgm"}:
                raise RuntimeError("Install application migrations first: citext and pg_trgm must already exist")
            if connection.scalar(text(
                "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:s) OR "
                "EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:s)"), {"s": schema}):
                raise RuntimeError("Generated benchmark name already exists; refusing cleanup of existing objects")
            # Eligibility precedes DDL/commit: an interruption at commit must
            # still clean up exactly this invocation's generated objects.
            cleanup_eligible = True
            if cleanup_state is not None:
                cleanup_state.update(schema=schema, cleanup="pending")
            # Names/password consist only of generated ASCII hex plus fixed prefix.
            try:
                connection.exec_driver_sql(f'CREATE ROLE "{schema}" LOGIN PASSWORD \'{password}\' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS')
            except Exception:
                # PostgreSQL PASSWORD DDL cannot bind this literal; never expose
                # the SQLAlchemy statement (which contains a generated secret).
                raise RuntimeError("Could not provision the isolated benchmark login") from None
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}" AUTHORIZATION "{schema}"')
        restricted = normalized_url(str(url)).set(username=schema, password=password)
        # Explicit pg_catalog placement lets our schema-local advisory wrappers
        # override the builtins. Public is needed for installed extension types;
        # ACL validation, not search_path, enforces the live-data boundary.
        restricted = restricted.update_query_dict({
            "options": f"-csearch_path={schema},pg_catalog,public",
            "application_name": schema,
            "connect_timeout": "10",
        })
        engine = create_engine(restricted, hide_parameters=True, connect_args={"connect_timeout": 10})
        try:
            with engine.begin() as connection:
                assert_private(connection, schema)
                install_private_locks(connection, schema)
            yield schema, restricted.render_as_string(hide_password=False)
        finally:
            engine.dispose()
    finally:
        with ignore_termination():
            try:
                if cleanup_eligible:
                    with admin.begin() as connection:
                        connection.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=:role AND pid<>pg_backend_pid()"), {"role": schema})
                        connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                        connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{schema}"')
                    if cleanup_state is not None:
                        cleanup_state["cleanup"] = "complete"
            finally:
                admin.dispose()


def run_schema_benchmark(args):
    if not args.traits or args.imports or args.dashboards or args.overview or args.test_suite:
        raise ValueError("Existing-container schema mode supports --traits only")
    if args.profiles < 1000:
        raise ValueError("Trait reference requires at least 1000 generated people (use 1500 or more)")
    from app.config import get_settings
    directory = Path(os.environ.get("KINSHIP_BENCHMARK_OUTPUT", args.output_dir)) / ("traits-" + secrets.token_hex(12))
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    migrations = Path(os.environ.get(
        "KINSHIP_BENCHMARK_MIGRATIONS",
        str(Path(__file__).resolve().parent.parent / "alembic.ini"),
    )).resolve()
    if not migrations.is_file():
        raise RuntimeError("Benchmark migrations are missing; use the built application container")
    backend = migrations.parent
    previous_handlers = {}
    child = None
    receipt = {"mode": "restricted-role-schema", "cleanup": "not_provisioned"}

    def interrupted(signum, frame):
        # A second Ctrl-C/TERM must not interrupt child shutdown or DDL cleanup.
        for termination in (signal.SIGTERM, signal.SIGINT):
            signal.signal(termination, signal.SIG_IGN)
        raise KeyboardInterrupt(f"Benchmark interrupted by signal {signum}")

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        with isolated_schema(get_settings().database_url, receipt) as (schema, url):
            env = child_environment(schema, url, directory)
            receipt.update(schema=schema, cleanup="pending")
            (directory / "isolation.json").write_text(json.dumps(receipt))
            print(f"Isolated schema benchmark: {schema}; evidence: {directory}", flush=True)
            commands = [
                [sys.executable, "-c", CHILD_BOOTSTRAP + "from alembic.config import main; main()",
                 "-c", str(migrations), "upgrade", "head"],
                [sys.executable, "-c", CHILD_BOOTSTRAP + "from pathlib import Path; from app.traits.verification import measure; "
                 f"measure(Path({str(directory)!r}), {args.profiles}, seed_dir={args.seed_dir!r})"],
            ]
            try:
                for command in commands:
                    child = subprocess.Popen(command, cwd=backend, env=env)
                    if child.wait() != 0:
                        raise RuntimeError("Isolated benchmark child failed; see console output and retained evidence")
                    child = None
            finally:
                with ignore_termination():
                    if child is not None and child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=10)
        return 0
    finally:
        (directory / "isolation.json").write_text(json.dumps(receipt))
        for child_directory in ("seed", "uploads", "exports"):
            shutil.rmtree(directory / child_directory, ignore_errors=True)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)