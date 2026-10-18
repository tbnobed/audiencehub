import argparse
import os
import time
from datetime import date, datetime, timezone
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session


def _compute_seed_traits(import_ids: list[int], *, as_of: date | None = None) -> None:
    """Wait for queued identity resolution, then compute seeded traits/history."""
    from app.traits.engine import backfill_trait_snapshots, recompute_traits
    from app.db import engine

    deadline = time.monotonic() + int(os.environ.get("SEED_LOAD_TIMEOUT_SECONDS", "7200"))
    while True:
        with Session(engine) as db:
            pending = db.execute(text(
                "SELECT count(*) FROM source_records WHERE resolved_at IS NULL "
                "AND last_import_id = ANY(:ids)"
            ), {"ids": import_ids}).scalar_one()
        if not pending:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Seed trait computation timed out waiting for identity resolution.")
        time.sleep(2)
    with Session(engine) as db:
        reference = as_of or datetime.now(timezone.utc).date()
        recompute_traits(db, as_of=reference)
        backfill_trait_snapshots(db, as_of=reference)
        db.execute(text("DELETE FROM trait_dirty_profiles"))
        db.commit()


def main():
    parser = argparse.ArgumentParser(
        description="Audience Hub administration and deterministic synthetic CSV generation."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    pull = commands.add_parser("source-pull", help="Pull a configured SFTP/Snowflake/Zeta source through COPY imports.")
    pull.add_argument("--source-id", required=True, type=int)
    pull.add_argument("--feed", choices=["people", "models", "events", "resources", "enrichment", "lookups"])
    pull.add_argument("--dry-run", action="store_true", help="Validate up to 100 remote rows per mapping without writes.")
    master = commands.add_parser("master-id", aliases=["masterID"], help="Permanent Master ID maintenance.")
    master_commands = master.add_subparsers(dest="master_command", required=True)
    backfill = master_commands.add_parser("backfill", help="Resumable backfill; commit each batch and safely restart.")
    backfill.add_argument("--batch-size", type=int, default=5000)
    backfill.add_argument("--max-batches", type=int, help="Stop after this many committed batches (default: drain).")
    filedrop = commands.add_parser("identity-filedrop", help="Resolve CSV identity keys from a configured remote SFTP folder.")
    filedrop.add_argument("--config", required=True,
                          help="Fernet-encrypted config file (FERNET_KEY); never a plaintext JSON or private-key file.")
    filedrop.add_argument("--watch", action="store_true", help="Keep polling; default is one folder pass.")
    filedrop.add_argument("--interval", type=float, default=60, help="Polling interval in seconds, minimum 5.")
    jobs = commands.add_parser("jobs", help="Worker job recovery.")
    job_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    cancel = job_commands.add_parser("cancel", help="Terminate stale owned SQL sessions and recover jobs.")
    cancel.add_argument("--all-stale", action="store_true", required=True,
                        help="Recover only jobs older than JOB_STALE_SECONDS; respects retry limits.")
    status = commands.add_parser(
        "load-status", help="Report a fixed selection of imports and dependent work (read-only).")
    status.add_argument("--wait", action="store_true", help="Poll to completion; progress every 30 seconds.")
    status.add_argument("--import-id", type=int, action="append",
                        help="Monitor only these IDs (repeatable); default: all imports existing at invocation.")
    status.add_argument("--timeout", type=float, default=14400,
                        help="Maximum monitoring wait in seconds (default: 14400 / 4h); does not cancel work.")
    admin = commands.add_parser("create-admin")
    admin.add_argument("--email", required=True)
    reset = commands.add_parser("reset-demo", help="Erase dataset rows, preserving users and configuration.")
    reset.add_argument("--yes", action="store_true", help="Required explicit acknowledgement.")
    reset.add_argument("--force", action="store_true", help="With --yes, permit APP_ENV=production.")
    from app.benchmark import add_arguments, run_benchmark
    benchmark = commands.add_parser("benchmark", help="Run isolated synthetic benchmarks (local database or container schema).")
    add_arguments(benchmark)
    seed = commands.add_parser(
        "seed",
        help="Generate synthetic CSV files under UPLOAD_DIR/seed-data (or --output-dir).",
        description=(
            "Write five synthetic source CSVs and ground_truth.json to "
            "UPLOAD_DIR/seed-data (default /data/uploads/seed-data). "
            "Without --load, no database or credentials are needed. "
            "--load expects app.importer.import_seed_files(files), where "
            "files maps CSV filenames to Path objects and the callable submits them "
            "through the real import-job pipeline."
        ),
    )
    seed.add_argument("--profiles", type=int, default=50_000)
    seed.add_argument("--scale", choices=("small", "medium", "large"), default="small")
    seed.add_argument("--mode", choices=("audit", "legacy"), default="audit")
    seed.add_argument("--random-seed", type=int, default=20250308)
    seed.add_argument("--as-of", type=date.fromisoformat,
                      default=None, metavar="YYYY-MM-DD",
                      help="UTC reference date; history covers the preceding five years through yesterday (default: today UTC).")
    seed.add_argument("--events", action="store_true",
                      help="Also send synthetic web/app traffic through the real API (INGEST_WRITE_KEY required).")
    seed.add_argument("--events-host", default="http://127.0.0.1:8000",
                      help="API origin for --events (HTTPS required outside localhost).")
    seed.add_argument("--output-dir", help="Output directory (default: UPLOAD_DIR/seed-data; UPLOAD_DIR defaults to /data/uploads).")
    seed.add_argument(
        "--load", action="store_true",
        help="After writing files, call app.importer.import_seed_files(files) if available.",
    )
    verify = commands.add_parser("verify-seed", help="Read-only gift, refund and profile reconciliation.")
    source_commands = commands.add_parser("sources", help="Admin source connection, preview and queued run.").add_subparsers(
        dest="source_command", required=True)
    for operation in ("test", "preview", "run"):
        command = source_commands.add_parser(operation)
        command.add_argument("source_id", type=int)
        if operation != "test":
            command.add_argument("mapping")
    zeta = commands.add_parser("import-zeta", help="Snapshot a Zeta export folder and queue its resumable migration.")
    zeta.add_argument("folder")
    zeta.add_argument("--source-id", type=int)
    zeta.add_argument("--counts", help="JSON file containing the 23 real Zeta audience counts.")
    verify.add_argument("--timeout-seconds", type=int, default=600,
                        help="Per-query timeout (default: 600 seconds). No data is changed.")
    verify.add_argument("--as-of", type=date.fromisoformat,
                        help="Reference date matching seed --as-of (default: today UTC, never the maximum gift date).")
    repair = commands.add_parser(
        "repair-seed", help="Repair only the existing synthetic audit seed (default: read-only dry run).")
    repair.add_argument("--apply", action="store_true", help="Apply atomically; also requires --yes.")
    repair.add_argument("--yes", action="store_true", help="Explicit acknowledgement; also requires --apply.")
    repair.add_argument("--timeout-seconds", type=int, default=7200,
                        help="Per-query timeout, 1..14400 seconds (default: 7200).")
    args = parser.parse_args()
    if args.command == "repair-seed":
        from app.repair_seed import repair_seed
        from sqlalchemy.exc import SQLAlchemyError
        try:
            raise SystemExit(repair_seed(
                apply=args.apply, yes=args.yes, timeout_seconds=args.timeout_seconds))
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from None
        except SQLAlchemyError as exc:
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            if sqlstate is None and getattr(exc, "orig", None) is not None:
                # Binding/compiler failures have no server SQLSTATE. Do not
                # print the outer exception, which includes SQL and parameters.
                original = exc.orig
                from sqlalchemy.exc import InvalidRequestError
                if isinstance(original, InvalidRequestError):
                    raise SystemExit(
                        f"Repair query construction failed: {original}. "
                        "No repair was committed.") from None
            reason = {
                "55P03": "Maintenance lock unavailable: concurrent database work is active.",
                "57014": "Database query timeout/cancellation.",
                "42501": "Database role lacks required permissions.",
                "25006": "Database/role is read-only.",
            }.get(sqlstate, f"Database request failed ({type(exc).__name__}).")
            raise SystemExit(
                f"{reason} Repair failed; transaction rolled back. "
                f"SQLSTATE={sqlstate or 'unavailable'}. Check database availability, permissions, "
                "maintenance-window locks and query timeout. No partial repair was committed.") from None
    if args.command == "verify-seed":
        from app.verify_seed import verify_seed
        raise SystemExit(verify_seed(args.timeout_seconds, as_of=args.as_of))
    if args.command in {"sources", "import-zeta"}:
        import json
        from pathlib import Path
        from app.zeta_cli import sources_command, import_folder, print_result
        if args.command == "sources":
            print_result(sources_command(args.source_command, args.source_id, getattr(args, "mapping", None)))
        else:
            counts = json.loads(Path(args.counts).read_text()) if args.counts else None
            print_result(import_folder(args.folder, args.source_id, counts))
        return
    if args.command == "source-pull":
        from app.source_pulls import run_source, collect, connection_row
        from app.db import engine
        import json
        try:
            if args.dry_run:
                with Session(engine) as db:
                    result = collect(connection_row(db, args.source_id), args.feed, dry_run=True)
            else:
                result = run_source(args.source_id, feed=args.feed)
            print(json.dumps(result))
        except Exception:
            raise SystemExit("Source pull failed; verify configuration and canonical feed data. Retry resumes durable checkpoints.") from None
        return
    if args.command in ("master-id", "masterID"):
        if not 1 <= args.batch_size <= 50000 or (args.max_batches is not None and args.max_batches < 1):
            parser.error("batch-size must be 1..50000 and max-batches must be positive.")
        from app.db import engine
        total = run_master_id_backfill(engine, args.batch_size, args.max_batches)
        print(f"Master ID backfill committed: {total} profiles; restart safely to resume.")
        return
    if args.command == "identity-filedrop":
        if args.interval < 5 or not __import__("math").isfinite(args.interval):
            parser.error("interval must be finite and at least 5 seconds.")
        from app.identity.filedrop import FiledropError, load_config, poll
        try:
            config = load_config(args.config)
            from app.db import engine
            while True:
                with Session(engine) as db:
                    # Lookup polling must never change identity or application rows.
                    db.execute(text("SET TRANSACTION READ ONLY"))
                    counts = poll(db, config)
                print(f"Identity filedrop: {counts['processed']} processed, {counts['skipped']} skipped.", flush=True)
                if not args.watch:
                    return
                time.sleep(args.interval)
        except FiledropError as exc:
            raise SystemExit(str(exc)) from None
        except KeyboardInterrupt:
            return
        except Exception:
            raise SystemExit("Identity filedrop failed; verify database and encrypted SFTP configuration.") from None
    if args.command == "jobs":
        from datetime import timedelta
        from app.config import get_settings
        from app.db import engine
        from app.jobs.queue import requeue_stale
        with Session(engine) as db:
            count = requeue_stale(db, timedelta(seconds=get_settings().job_stale_seconds))
            db.commit()
        print(f"Recovered {count} stale job(s); exhausted attempts remain failed.")
        return
    if args.command == "load-status":
        from app.db import engine
        from app.load_status import run_load_status
        try:
            raise SystemExit(run_load_status(
                engine, wait=args.wait, import_ids=args.import_id, timeout=args.timeout))
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
    if args.command == "benchmark":
        try:
            raise SystemExit(run_benchmark(args))
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
    if args.command == "reset-demo":
        from app.reset_demo import reset_demo
        # Do not even create a database engine before the environment guard.
        if not args.yes or (os.environ.get("APP_ENV", "").strip().lower() == "production"
                            and not args.force):
            try:
                reset_demo(None, yes=args.yes, force=args.force)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
        from app.db import engine
        try:
            reset_demo(engine, yes=args.yes, force=args.force)
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
        return
    if args.command == "seed":
        from app.seed import generate_seed, load_generated_files

        try:
            reference = args.as_of or datetime.now(timezone.utc).date()
            result = generate_seed(
                profiles=args.profiles,
                scale=args.scale,
                random_seed=args.random_seed,
                output_dir=args.output_dir,
                as_of=reference,
                mode=args.mode,
            )
            print(f"Synthetic CSVs written to: {result['output_dir']}")
            for filename, count in result["counts"].items():
                print(f"  {filename}: {count:,} records")
            print(f"  ground_truth.json: {result['record_count']:,} record mappings")
            print(f"Ground truth: {result['ground_truth']}")
            if args.load:
                import_ids = load_generated_files(result["files"])
                print("CSV files submitted through app.importer.import_seed_files.")
                _compute_seed_traits(import_ids, as_of=reference)
                from app.db import engine
                from app.segments.service import ensure_templates
                with Session(engine) as db:
                    ensure_templates(db)
                    db.commit()
                print("Computed profile traits and backfilled 24 monthly snapshots.")
            if args.events:
                from app.ingestion.seed_events import send_events
                sent = send_events(profiles=args.profiles, random_seed=args.random_seed, host=args.events_host,
                                   as_of=reference,
                                   contacts_path=(result["files"].get("donor_direct.csv")
                                                  or result["files"].get("donor_crm_contacts.csv")))
                print(f"Event API accepted {sent:,} synthetic messages.")
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
        return
    from app.db import engine
    from app.models import AuditLog, User
    with Session(engine) as db:
        # Break-glass bootstrap uses a future OIDC subject linked by verified email on login.
        user = db.scalar(select(User).where(User.email == args.email))
        if not user:
            user = User(subject="bootstrap:" + args.email, email=args.email,
                        name="Bootstrap Admin", role="admin")
            db.add(user)
            db.flush()
        else:
            user.role = "admin"
        db.add(AuditLog(user_id=user.id, actor_type="system", action="user.create_admin",
                        entity_type="user", entity_id=str(user.id), details={}))
        db.commit()
    print("Admin provisioned. Production login still requires an OIDC admin group.")


def run_master_id_backfill(engine, batch_size=5000, max_batches=None):
    """A stopped/crashed invocation loses at most its uncommitted final batch."""
    from app.identity.master import backfill_master_ids
    total = batches = 0
    while max_batches is None or batches < max_batches:
        with Session(engine) as db:
            updated = backfill_master_ids(db, batch_size=batch_size)
            db.commit()
        total += updated
        batches += 1
        if not updated:
            break
    return total


if __name__ == "__main__":
    main()