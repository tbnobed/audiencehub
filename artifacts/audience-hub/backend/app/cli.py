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
    args = parser.parse_args()
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
                                   contacts_path=result["files"].get("donor_crm_contacts.csv"))
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


if __name__ == "__main__":
    main()