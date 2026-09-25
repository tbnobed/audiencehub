"""Explicit, transactional dataset reset; configuration and operators are retained."""
import os

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError


# Keep this allowlist in sync with migrations. Never use CASCADE: a new FK from a
# retained table must make the operation fail, not quietly destroy configuration.
DATA_TABLES = (
    "audit_log", "scheduled_runs", "jobs", "imports", "import_batch_diagnostics",
    "profiles", "source_records",
    "identifiers", "identifier_blocklist", "profile_merges", "gifts", "events",
    "consents", "suppressions", "enrichment_values", "profile_traits",
    "trait_snapshots", "trait_dirty_profiles", "segment_membership",
    "segment_counts", "segments", "activation_runs", "activations",
    "destinations", "deletion_requests",
    "dashboard_daily", "dashboard_donor_daily", "dashboard_donor_patterns",
    "dashboard_giving_daily", "dashboard_event_daily", "dashboard_conversion_daily",
    "dashboard_kpis", "dashboard_cache",
)
RETAINED_TABLES = (
    "users", "sources", "enrichment_attributes", "admin_settings",
)
KNOWN_TABLES = frozenset((*DATA_TABLES, *RETAINED_TABLES, "alembic_version"))


def reset_demo(engine: Engine, *, yes: bool = False, force: bool = False) -> None:
    """Reset dataset rows only; refuse active jobs and unexpected schema changes."""
    environment = os.environ.get("APP_ENV", "").strip().lower()
    print("Reset scope (rows and identities): " + ", ".join(DATA_TABLES))
    print("Preserved (including source settings; per-import mappings are erased): "
          + ", ".join(RETAINED_TABLES))
    print("Guard: APP_ENV=production requires both --force and --yes; "
          "all environments require --yes. Running jobs or busy tables block reset.")
    if not yes:
        raise ValueError("Refusing reset-demo without --yes.")
    if environment == "production" and not force:
        raise ValueError("Refusing reset-demo in APP_ENV=production without --force AND --yes.")

    try:
        with engine.begin() as conn:
            # NOWAIT avoids waiting for a live worker. This lock serializes claims,
            # enqueue and heartbeats until the transaction finishes. Check running
            # status only AFTER the lock, to avoid a claim/check race.
            conn.execute(text("LOCK TABLE public.jobs IN ACCESS EXCLUSIVE MODE NOWAIT"))
            running = conn.execute(text(
                "SELECT count(*) FROM public.jobs WHERE status = 'running'"
            )).scalar_one()
            if running:
                raise RuntimeError(
                    f"Refusing reset-demo: {running} running job(s). Stop workers and wait for jobs to finish."
                )

            # Includes partition parents but not their children (TRUNCATE parent
            # includes partitions). Fail closed when migrations introduce a table.
            actual = set(conn.execute(text("""
                SELECT c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                  AND NOT c.relispartition
            """)).scalars())
            if actual != KNOWN_TABLES:
                raise RuntimeError(
                    "Refusing reset-demo: unreviewed public tables; missing="
                    f"{sorted(KNOWN_TABLES - actual)}, unexpected={sorted(actual - KNOWN_TABLES)}."
                )

            # No CASCADE; PostgreSQL verifies every FK, including new ones from
            # protected tables, and rolls back every table if anything fails.
            tables = ", ".join("public." + name for name in DATA_TABLES)
            conn.execute(text(f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE NOWAIT"))
            conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY"))
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise RuntimeError(
                "Refusing reset-demo: a job or dataset table is busy. "
                "Stop workers and other writers before retrying."
            ) from exc
        raise
    print("Dataset reset complete. Users, sources, source definitions and admin settings preserved.")