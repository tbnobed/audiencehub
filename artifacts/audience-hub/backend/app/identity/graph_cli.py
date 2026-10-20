"""Server-side graph operations; importing this file does not start work."""
import json
import secrets

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine


def add_commands(commands):
    graph = commands.add_parser("identity-graph", help="First-party identity graph batch and household operations.")
    actions = graph.add_subparsers(dest="graph_command", required=True)
    run = actions.add_parser("run", help="Queue a dry run by default; --apply explicitly permits graph writes.")
    run.add_argument("--apply", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    actions.add_parser("status", help="Read graph run history.")
    actions.add_parser("households", help="Normalize current addresses and recompute households; no person merges.")
    key = actions.add_parser("device-key", help="Rotate a source's private device-event HMAC key; output once.")
    key.add_argument("--source-id", required=True, type=int)


def command(args):
    with Session(engine) as db:
        if args.graph_command == "run":
            if args.apply and args.dry_run:
                raise SystemExit("Choose --apply or --dry-run, not both")
            from app.identity.graph_jobs import queue_run
            identifier = queue_run(db, dry_run=not args.apply)
            db.commit()
            print(json.dumps({"job_id": identifier, "dry_run": not args.apply}))
        elif args.graph_command == "status":
            rows = db.execute(text("""SELECT r.*,j.status AS worker_status FROM identity_graph_runs r
              LEFT JOIN jobs j ON j.id=r.job_id ORDER BY r.id DESC LIMIT 100""")).mappings()
            print(json.dumps([dict(r) for r in rows], default=str, indent=2))
        elif args.graph_command == "households":
            from app.identity.addresses import refresh_addresses, refresh_households
            from app.identity.locking import acquire_resolver_lock
            from app.identity.quality import refresh_queue_trends
            acquire_resolver_lock(db)
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"))
            count = refresh_addresses(db)
            refresh_households(db)
            refresh_queue_trends(db)
            from app.privacy.service import audit
            audit(db, None, "identity.households.refresh", "identity", None, {"addresses": count})
            db.commit()
            print(json.dumps({"addresses_updated": count}))
        elif args.graph_command == "device-key":
            if not db.scalar(text("SELECT EXISTS(SELECT 1 FROM sources WHERE id=:s AND is_active)"),
                             {"s": args.source_id}):
                raise SystemExit("Active source not found")
            from app.activations.security import encrypt
            from app.privacy.service import audit
            secret = secrets.token_urlsafe(48)
            db.execute(text("""INSERT INTO identity_device_credentials(source_id,secret_encrypted)
              VALUES(:s,:key) ON CONFLICT(source_id) DO UPDATE SET secret_encrypted=EXCLUDED.secret_encrypted"""),
              {"s": args.source_id, "key": encrypt({"secret": secret})})
            audit(db, None, "identity.device.rotate", "source", args.source_id, {})
            db.commit()
            print("Private server device-event key (shown once; never use in browsers):")
            print(secret)
