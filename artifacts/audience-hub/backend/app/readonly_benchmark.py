"""Existing-data segment timings: no fixture preparation or database writes.

Use a dedicated engine, not app.db's worker-registration checkout hooks.
PostgreSQL enforces read-only mode, including refusal to CREATE TEMP TABLE.
Every invocation owns one repeatable-read snapshot and always rolls it back.
"""
from contextlib import contextmanager
from datetime import date, datetime, time as midnight, timezone
import json
import os
from pathlib import Path
import statistics
import tempfile
import time

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool


def reference_instant(value=None):
    """Pin one UTC instant before measuring any template."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, midnight(), timezone.utc)
    if not isinstance(value, datetime):
        raise ValueError("--as-of requires an ISO date or timestamp")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def readonly_engine(url):
    url = make_url(str(url).replace("postgres://", "postgresql+psycopg://", 1)
                   .replace("postgresql://", "postgresql+psycopg://", 1))
    if url.get_backend_name() != "postgresql":
        raise ValueError("Read-only segment benchmarking requires PostgreSQL")
    options = url.query.get("options", "")
    return create_engine(
        url, poolclass=NullPool, hide_parameters=True,
        connect_args={
            "connect_timeout": 10,
            "application_name": "kinship-benchmark-segments-readonly",
            # Startup protects even dialect initialization queries. Explicit
            # SET TRANSACTION below additionally guards the measured transaction.
            "options": options + " -cdefault_transaction_read_only=on"
                       " -cstatement_timeout=10000 -clock_timeout=5000",
        },
    )


@contextmanager
def readonly_session(engine):
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            # This MUST precede the first application query, not follow a
            # catalog, profile count, compiler or template-installation call.
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            if connection.scalar(text("SHOW transaction_read_only")) != "on":
                raise RuntimeError("PostgreSQL did not enforce a read-only transaction")
            with Session(bind=connection, autoflush=False,
                         join_transaction_mode="rollback_only") as db:
                yield db
        finally:
            if transaction.is_active:
                transaction.rollback()


def _sample(db, definition, instant):
    """Same SQL work as the API sample: 25 rows plus full status/source counts.

    Do not import the HTTP module: it initializes the application's write-capable
    engine and worker hooks. No contact data is retained in benchmark evidence.
    """
    from app.models import Profile, ProfileTrait
    from app.segments.compiler import compile_definition

    query = compile_definition(db, definition, instant)
    ids = list(db.scalars(query.limit(25)))
    sub = query.subquery()
    status = db.execute(select(ProfileTrait.donor_status, func.count()).select_from(
        sub.outerjoin(ProfileTrait, ProfileTrait.profile_id == sub.c.id)
    ).group_by(ProfileTrait.donor_status)).all()
    sources = db.execute(select(
        func.unnest(ProfileTrait.source_keys).label("key"), func.count()
    ).select_from(sub.join(ProfileTrait, ProfileTrait.profile_id == sub.c.id))
      .group_by(text("key"))).all()
    rows = db.execute(select(
        Profile.id, Profile.first_name, Profile.last_name, Profile.email,
        Profile.phone, ProfileTrait.donor_status, ProfileTrait.source_keys,
    ).outerjoin(ProfileTrait, ProfileTrait.profile_id == Profile.id)
      .where(Profile.id.in_(ids)).order_by(Profile.id)).all()
    return {"rows": len(rows), "status_total": sum(row[1] for row in status),
            "source_groups": len(sources)}


def _repeated(fn):
    values, durations = [], []
    for _ in range(4):
        start = time.perf_counter()
        values.append(fn())
        durations.append(round((time.perf_counter() - start) * 1000, 3))
    return values, {"first_ms": durations[0], "warm_ms": durations[1:],
                    "warm_median_ms": statistics.median(durations[1:])}


def measure_existing(engine, directory, *, as_of=None, profiles_option=500_000):
    from app.segments.compiler import compile_definition
    from app.segments.templates import template_definitions

    instant = reference_instant(as_of)
    directory = Path(directory)
    report = {
        "mode": "existing-app-database-readonly", "read_only": True, "status": "running",
        "as_of": instant.isoformat(), "timezone": "UTC",
        "profiles_option_ignored": profiles_option,
        "materialization": {"status": "skipped", "reason": "read-only existing-data benchmark"},
        "database_writes": "forbidden by PostgreSQL READ ONLY; transaction always rolled back",
        "scope": "Code-defined templates against existing data; no seed, migration, template installation, materialization, jobs or audit writes. Count executes a compiled query; preview includes compilation; sample includes 25 rows and status/source breakdowns. No HTTP transport or contact data in evidence.",
        "first_definition": "First execution of each operation per template, not cold PostgreSQL/OS caches. Count precedes preview and sample; one connection and pinned snapshot for the entire run.",
        "warm_definition": "Three repetitions of each operation in the same read-only snapshot.",
        "templates": [],
    }

    def checkpoint():
        (directory / "segments.json").write_text(json.dumps(report, indent=2) + "\n")

    try:
        templates = template_definitions()
        if len(templates) != 23 or len({t["key"] for t in templates}) != 23:
            raise RuntimeError("Expected exactly 23 unique code-defined segment templates")
        with readonly_session(engine) as db:
            report["profiles"] = db.scalar(text("SELECT count(*) FROM active_profiles"))
            report["population"] = {
                "active_profiles": report["profiles"],
                "profiles": db.scalar(text("SELECT count(*) FROM profiles")),
            }
            checkpoint()
            for template in templates:
                row = {"key": template["key"], "name": template["name"],
                       "materialization": report["materialization"], "status": "running"}
                report["templates"].append(row)
                try:
                    # Timeouts/errors roll back only this template, retaining
                    # READ ONLY and the pinned snapshot for subsequent templates.
                    with db.begin_nested():
                        query = compile_definition(db, template["definition"], instant)
                        counts, row["count_latency"] = _repeated(
                            lambda: db.scalar(select(func.count()).select_from(query.subquery())))

                        def preview():
                            compiled = compile_definition(db, template["definition"], instant)
                            return db.scalar(select(func.count()).select_from(compiled.subquery()))

                        previews, row["preview_latency"] = _repeated(preview)
                        samples, row["sample_latency"] = _repeated(
                            lambda: _sample(db, template["definition"], instant))
                        row.update(count=counts[0], sample_rows=samples[0]["rows"])
                        row["status"] = "passed" if (
                            all(value == counts[0] for value in counts + previews)
                            and all(sample["rows"] == min(25, counts[0])
                                    and sample["status_total"] == counts[0] for sample in samples)
                        ) else "failed"
                        if row["status"] == "failed":
                            row["error"] = "Count/preview/sample parity failed in the pinned snapshot"
                except Exception as exc:
                    # DBAPI SQL parameters can contain sensitive data; retain
                    # only the explicit type/SQLSTATE, not statement or values.
                    row.update(status="failed", error=f"{type(exc).__name__}: template query failed",
                               sqlstate=getattr(getattr(exc, "orig", None), "sqlstate", None))
                print(f'{row["key"]}: {row["status"]}; count={row.get("count", "error")}; '
                      f'count_latency={row.get("count_latency", {})}; '
                      f'preview={row.get("preview_latency", {})}; sample={row.get("sample_latency", {})}; '
                      "materialization skipped (read-only)", flush=True)
                checkpoint()
        report["status"] = "passed" if all(row["status"] == "passed" for row in report["templates"]) else "failed"
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: read-only benchmark could not complete")
        checkpoint()
        raise
    checkpoint()
    return report


def run_readonly_segments(args):
    # Settings are consulted only here, after CLI ambiguity validation. Do not
    # import app.db: its checkout hooks are intended for write-capable workers.
    from app.config import get_settings

    instant = reference_instant(getattr(args, "as_of", None))
    root = Path(os.environ.get("KINSHIP_BENCHMARK_OUTPUT", args.output_dir)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="segments-readonly-", dir=root))
    engine = readonly_engine(get_settings().database_url)
    try:
        print(f"Read-only existing-data segment benchmark evidence: {directory}", flush=True)
        report = measure_existing(engine, directory, as_of=instant,
                                  profiles_option=getattr(args, "profiles", 500_000))
        print(f'Actual active profiles: {report["profiles"]}; --profiles ignored; materialization skipped (read-only)', flush=True)
        return 0 if report["status"] == "passed" else 1
    finally:
        engine.dispose()
