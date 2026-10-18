"""Real PostgreSQL safety tests, ONLY via the existing private socket harness."""
import json

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.readonly_benchmark import measure_existing, readonly_engine, readonly_session
from test_schema_benchmark_pg import admin


def durable_counts(engine):
    with engine.connect() as db:
        tables = db.execute(text(
            "SELECT n.nspname,c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN ('r','p') "
            "ORDER BY n.nspname,c.relname"
        )).all()
        quote = engine.dialect.identifier_preparer.quote
        return {
            f"{schema}.{table}": db.scalar(text(
                f"SELECT count(*) FROM {quote(schema)}.{quote(table)}"
            )) for schema, table in tables
        }


@pytest.mark.parametrize("statement", [
    "INSERT INTO public.jobs DEFAULT VALUES",
    "UPDATE public.jobs SET status=status",
    "DELETE FROM public.jobs WHERE false",
    "CREATE TEMP TABLE readonly_benchmark_forbidden(value int)",
])
def test_postgres_refuses_insert_update_delete_and_temp_creation(admin, statement):
    admin_engine, url = admin
    before = durable_counts(admin_engine)
    engine = readonly_engine(url)
    seen = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, sql, params, context, many: seen.append(sql))
    try:
        with pytest.raises(DBAPIError) as raised:
            with readonly_session(engine) as db:
                assert db.scalar(text("SHOW transaction_read_only")) == "on"
                db.execute(text(statement))
        assert raised.value.orig.sqlstate == "25006"
        assert seen[0] == "SET TRANSACTION READ ONLY"
        assert durable_counts(admin_engine) == before
    finally:
        engine.dispose()


def test_entire_existing_data_run_has_unchanged_durable_rows(admin, tmp_path):
    admin_engine, url = admin
    before = durable_counts(admin_engine)
    engine = readonly_engine(url)
    statements = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, sql, params, context, many: statements.append(sql))
    try:
        report = measure_existing(engine, tmp_path, as_of="2025-01-01", profiles_option=500000)
        assert report["status"] == "passed", report
        assert len(report["templates"]) == 23
        assert report["population"]["profiles"] == before["public.profiles"]
        assert report["materialization"]["status"] == "skipped"
        assert report["profiles_option_ignored"] == 500000
        assert all(row["status"] == "passed" for row in report["templates"])
        assert statements[0] == "SET TRANSACTION READ ONLY"
        assert not any(sql.lstrip().upper().startswith((
            "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE"
        )) for sql in statements)
        assert durable_counts(admin_engine) == before
        assert json.loads((tmp_path / "segments.json").read_text())["status"] == "passed"
    finally:
        engine.dispose()
