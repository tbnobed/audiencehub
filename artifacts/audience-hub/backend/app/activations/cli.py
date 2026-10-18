"""Operator CLI: python -m app.activations.cli bootstrap|run|destination-config."""
import argparse
import json
from pathlib import Path
from sqlalchemy.orm import Session


def main():
    parser = argparse.ArgumentParser(description="Activation administration; secrets are read from files, never printed.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap")
    run = commands.add_parser("run")
    run.add_argument("activation_id", type=int)
    run.add_argument("--admin-id", required=True, type=int)
    config = commands.add_parser("destination-config")
    config.add_argument("destination_id", type=int)
    config.add_argument("--admin-id", required=True, type=int)
    config.add_argument("--file", required=True, help="JSON DestinationPatch contract including write-only credentials.")
    args = parser.parse_args()
    from app.db import engine
    from app.activations import service
    from app.models import Destination, User
    with Session(engine) as db:
        if args.command == "bootstrap":
            from app.activations.bootstrap import bootstrap
            result = bootstrap(db)
            db.commit()
            print(json.dumps(result))
            return
        user = db.get(User, args.admin_id)
        if not user or not user.is_active or user.role != "admin":
            parser.exit(1, "An active admin is required.\n")
        if args.command == "run":
            run, job = service.enqueue_run(db, args.activation_id, user.id)
            service.audit(db, "activation.queued", "activation", args.activation_id, user.id, run_id=run.id)
            db.commit()
            print(json.dumps({"run_id": run.id, "job_id": job.id}))
        else:
            from app.activations.api import DestinationPatch, save_destination
            row = db.get(Destination, args.destination_id)
            if not row:
                parser.exit(1, "Destination not found.\n")
            body = DestinationPatch.model_validate(json.loads(Path(args.file).read_text()))
            save_destination(db, row, body, user)
            print(json.dumps({"updated": row.id}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Pydantic exceptions include offending inputs, potentially private keys.
        raise SystemExit("Activation operation failed; verify configuration, permissions and run history.") from None
