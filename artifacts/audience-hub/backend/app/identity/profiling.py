"""Resolver-only profiling for an explicitly isolated benchmark database.

Fixture imports and extension setup belong to the disposable benchmark runner.
This helper deliberately does not connect using application configuration.
"""

from __future__ import annotations

import cProfile
import io
import json
import os
from pathlib import Path
import pstats
import time
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session


def profile_resolution(
    db: Session,
    output: Path,
    *,
    resolver: Callable[..., dict[str, int]],
    max_seconds: float | None = None,
) -> dict:
    """Profile committed resolver groups, reporting bounded runs as incomplete.

    The runner must preload pg_stat_statements in its private PostgreSQL cluster.
    No extension configuration or server restart is attempted here. A deadline
    is checked between transactions; it is not a SQL cancellation deadline.
    """
    if os.environ.get("APP_ENV") != "test" or os.environ.get(
        "KINSHIP_BENCHMARK_ISOLATED_TESTS"
    ) != "1":
        raise RuntimeError("Resolver profiling requires the isolated benchmark harness")
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    # Fail before touching data when the requested PostgreSQL profiler is absent.
    installed = db.execute(text("""
        SELECT EXISTS (
          SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements'
        )
    """)).scalar_one()
    if not installed:
        raise RuntimeError("Preload and install pg_stat_statements in the private cluster")
    output.mkdir(parents=True, exist_ok=True)
    db.execute(text("SELECT pg_stat_statements_reset()"))
    db.commit()
    counts = {"records": 0, "profiles_created": 0, "merges": 0}
    groups = 0
    complete = False
    profiler = cProfile.Profile()
    started = time.perf_counter()
    failure = None
    try:
        profiler.enable()
        while True:
            result = resolver(db)
            db.commit()
            groups += 1
            for key in counts:
                counts[key] += result[key]
            if result["records"] == 0:
                complete = True
                break
            if max_seconds is not None and time.perf_counter() - started >= max_seconds:
                break
    except Exception as exc:
        db.rollback()
        # Do not persist exception parameters, which can contain contact PII.
        failure = type(exc).__name__
        raise
    finally:
        profiler.disable()
        elapsed = time.perf_counter() - started
        profiler.dump_stats(str(output / "resolver.prof"))
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats(
            "cumulative"
        ).print_stats(20)
        (output / "cprofile-top20.txt").write_text(stream.getvalue())
        report = {
            "complete": complete,
            "profiled": True,
            "seconds": elapsed,
            "committed_groups": groups,
            **counts,
            "failure_type": failure,
            "deadline_between_transactions_seconds": max_seconds,
        }
        (output / "resolver-profile.json").write_text(json.dumps(report, indent=2) + "\n")
        # dbid isolation matters even in a private cluster with multiple databases.
        statements = db.execute(text("""
            SELECT queryid::text, calls, total_exec_time, mean_exec_time,
                   rows, shared_blks_hit, shared_blks_read, temp_blks_written,
                   query
            FROM pg_stat_statements
            WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())
            ORDER BY total_exec_time DESC, queryid LIMIT 10
        """)).mappings().all()
        (output / "pg-stat-statements-top10.json").write_text(
            json.dumps([dict(row) for row in statements], indent=2) + "\n"
        )
        db.rollback()
    return report