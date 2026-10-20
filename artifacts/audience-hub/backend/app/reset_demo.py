"""Explicit, transactional dataset reset; configuration and operators are retained."""
import os
import re

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError


# Keep this allowlist in sync with migrations. Never use CASCADE: a new FK from a
# retained table must make the operation fail, not quietly destroy configuration.
DATA_TABLES = (
    "meta_ad_results", "meta_audiences", "meta_attribution_sessions", "meta_pending_contacts",
    "meta_verified_keys", "meta_leads",
    "identity_pairs", "identity_devices", "identity_addresses", "identity_graph_daily",
    "identity_graph_runs", "identity_device_receipts",
    "audit_log", "client_errors", "scheduled_runs", "jobs", "source_import_receipts",
    "zeta_migrations", "zeta_migration_files",
    "hubspot_pulls",
    "imports", "import_batch_diagnostics", "identity_batches",
    "profiles", "source_records", "ingestion_receipts",
    "identifiers", "identifier_blocklist", "profile_merges", "gifts", "events",
    "source_codes", "products", "vod_titles",
    "identity_merge_journal", "identity_reviews", "identity_xref", "master_id_aliases",
    "consents", "suppressions", "enrichment_values", "profile_traits",
    "trait_snapshots", "trait_dirty_profiles", "segment_membership",
    "segment_counts", "segments", "activation_runs", "activations",
    "destinations", "deletion_requests", "activation_run_members", "activation_last_members",
    "dashboard_daily", "dashboard_donor_daily", "dashboard_donor_patterns",
    "dashboard_giving_daily", "dashboard_event_daily", "dashboard_conversion_daily",
    "dashboard_kpis", "dashboard_cache",
)
RETAINED_TABLES = (
    "meta_connections",
    "identity_graph_policy", "identity_device_credentials",
    "users", "sources", "enrichment_attributes", "admin_settings", "master_id_registry",
    "source_connections", "canonical_field_catalog",
    "local_credentials", "local_sessions", "local_account_tokens",
    "local_login_limits", "operations_policy",
    "operations_cleanup_files",
)
KNOWN_TABLES = frozenset((*DATA_TABLES, *RETAINED_TABLES, "alembic_version"))


def safe_query_preview(query):
    """SQL structure only: pg_stat_activity may contain bound PII or credentials."""
    query = re.sub(r"\$([A-Za-z_][A-Za-z_0-9]*|)\$.*?\$\1\$", "?", query or "", flags=re.S)
    query = re.sub(r"\$(?:[A-Za-z_][A-Za-z_0-9]*|)\$.*$", "?", query, flags=re.S)
    query = re.sub(r"/\*.*?(?:\*/|$)|--[^\n]*", " ", query, flags=re.S)
    query = re.sub(r"'(?:''|\\.|[^'])*(?:'|$)|\"(?:\"\"|[^\"])*(?:\"|$)", "?", query, flags=re.S)
    query = re.sub(r"\b\d+(?:\.\d+)?\b", "?", query)
    return " ".join(query.split())[:120]


def busy_sessions(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT pid, application_name, query_start, query
            FROM pg_stat_activity
            WHERE datname=current_database() AND pid<>pg_backend_pid()
              AND (state IS DISTINCT FROM 'idle' OR EXISTS (
                  SELECT 1 FROM pg_locks l WHERE l.pid=pg_stat_activity.pid
                    AND l.relation IS NOT NULL))
            ORDER BY query_start NULLS LAST, pid
        """)).all()
    return "\n".join(
        f"pid={row.pid} application_name={row.application_name!r} "
        f"query_start={row.query_start} query={safe_query_preview(row.query)!r}"
        for row in rows
    ) or "No other busy sessions visible (check PostgreSQL monitoring permissions)."


def reset_demo(engine: Engine, *, yes: bool = False, force: bool = False,
               confirm_database: str | None = None, go_live: bool = False) -> None:
    """Reset dataset rows only; refuse active jobs and unexpected schema changes."""
    from app.operations.guards import production_guard
    production_guard(engine, allow=force, database=confirm_database)
    dataset_tables = tuple(name for name in DATA_TABLES
                           if not go_live or name not in ("destinations", "audit_log"))
    print("Reset scope (rows and identities): " + ", ".join(dataset_tables))
    print("Preserved (including source settings; per-import mappings are erased): "
          + ", ".join(RETAINED_TABLES))
    print("Source connections preserve encrypted credentials/configuration; incremental cursors "
          "and pending pull recovery state are cleared atomically with receipts.")
    print("Guard: APP_ENV=production requires --force, --yes and --confirm-database; "
          "all environments require --yes. Running jobs or busy tables block reset.")
    if not yes:
        raise ValueError("Refusing reset-demo without --yes.")

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
                    f"Refusing reset-demo: {running} running job(s). Stop workers and recover stale jobs.\n"
                    + busy_sessions(engine)
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
            tables = ", ".join("public." + name for name in dataset_tables)
            conn.execute(text(f"LOCK TABLE {tables}, public.source_connections IN ACCESS EXCLUSIVE MODE NOWAIT"))
            if go_live:
                nonseed = conn.scalar(text("""SELECT count(*) FROM source_records r
                    LEFT JOIN imports i ON i.id=r.last_import_id
                    WHERE i.id IS NULL OR NOT coalesce(i.file_path ~ '/seed-[a-f0-9]{16}-',false)"""))
                nonsynthetic_events = conn.scalar(text("""SELECT count(*) FROM events
                    WHERE NOT context @> '{"synthetic":true}'::jsonb"""))
                orphan_profiles = conn.scalar(text("""SELECT count(*) FROM profiles p WHERE NOT EXISTS
                    (SELECT 1 FROM source_records r WHERE r.profile_id=p.id)
                    AND NOT p.is_deleted AND p.merged_into_id IS NULL"""))
                nonseed_gifts = conn.scalar(text("""SELECT count(*) FROM gifts g
                    LEFT JOIN source_records r ON r.id=g.source_record_id
                    LEFT JOIN imports i ON i.id=r.last_import_id
                    WHERE i.id IS NULL OR NOT coalesce(i.file_path ~ '/seed-[a-f0-9]{16}-',false)"""))
                if nonseed or nonsynthetic_events or orphan_profiles or nonseed_gifts:
                    raise ValueError("Go-live wipe refused: real, mixed or unproven dataset evidence exists. No rows changed.")
                # Keep every destination/config/secret, but prevent a scheduled
                # empty managed-list sync before the real dataset is loaded.
                conn.execute(text("UPDATE destinations SET is_active=false,updated_at=now()"))
                # Durable cleanup receipts survive the dataset wipe. If the
                # process dies after commit, daily maintenance finishes removal.
                conn.execute(text("""INSERT INTO operations_cleanup_files(path)
                    SELECT file_path FROM imports WHERE file_path IS NOT NULL
                    UNION SELECT error_file_path FROM imports WHERE error_file_path IS NOT NULL
                    UNION SELECT file_path FROM activation_runs WHERE file_path IS NOT NULL
                    ON CONFLICT DO NOTHING"""))
            # CLI pulls also hold this advisory mutex, even while waiting on
            # remote I/O with no active database transaction or running job.
            for source_id in conn.execute(text("SELECT source_id FROM public.source_connections")).scalars():
                if not conn.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                                   {"key": f"source-pull:{source_id}"}):
                    raise RuntimeError("Refusing reset-demo: a source pull is busy. Stop source pull processes before retrying.")
            conn.execute(text("UPDATE public.source_connections SET state='{}'::jsonb,updated_at=now()"))
            from app.identity.graph_jobs import purge_scratch
            purge_scratch(conn)
            conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY"))
            # Bootstrap audiences atomically with the reset, retaining the first
            # admin as owner. Counts are populated after the subsequent seed load.
            from sqlalchemy.orm import Session
            from app.segments.service import ensure_templates
            with Session(bind=conn) as db:
                ensure_templates(db)
                if go_live:
                    from app.models import AuditLog
                    db.add(AuditLog(actor_type="system", action="maintenance.demo_wiped",
                                    entity_type="dataset", details={"destinations_preserved": True}))
                db.commit()  # Joins, but does not commit, the outer transaction.
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise RuntimeError(
                "Refusing reset-demo: a job or dataset table is busy. "
                "Stop workers and other writers before retrying.\n" + busy_sessions(engine)
            ) from exc
        raise
    if go_live:
        from app.operations.files import cleanup_registered_files
        cleanup_registered_files(engine)
        print("Destinations preserved but paused for safety. Review real consented data before re-enabling deliveries.")
    print("Dataset reset complete. Users, sources, encrypted connections and admin settings preserved; source cursors and recovery state cleared.")