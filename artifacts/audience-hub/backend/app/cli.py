import argparse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import engine
from app.models import AuditLog, User


def main():
    parser = argparse.ArgumentParser(
        description="Audience Hub administration and deterministic synthetic CSV generation."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin")
    admin.add_argument("--email", required=True)
    seed = commands.add_parser(
        "seed",
        help="Generate synthetic CSV files under backend/seed-data (or --output-dir).",
        description=(
            "Write five synthetic source CSVs and ground_truth.json to backend/seed-data "
            "by default. --load expects app.importer.import_seed_files(files), where "
            "files maps CSV filenames to Path objects and the callable submits them "
            "through the real import-job pipeline."
        ),
    )
    seed.add_argument("--profiles", type=int, default=50_000)
    seed.add_argument("--scale", choices=("small", "medium", "large"), default="small")
    seed.add_argument("--random-seed", type=int, default=20250308)
    seed.add_argument("--output-dir", help="Output directory (default: backend/seed-data).")
    seed.add_argument(
        "--load", action="store_true",
        help="After writing files, call app.importer.import_seed_files(files) if available.",
    )
    args = parser.parse_args()
    if args.command == "seed":
        from app.seed import generate_seed, load_generated_files

        try:
            result = generate_seed(
                profiles=args.profiles,
                scale=args.scale,
                random_seed=args.random_seed,
                output_dir=args.output_dir,
            )
            print(f"Synthetic CSVs written to: {result['output_dir']}")
            for filename, count in result["counts"].items():
                print(f"  {filename}: {count:,} records")
            print(f"  ground_truth.json: {result['record_count']:,} record mappings")
            print(f"Ground truth: {result['ground_truth']}")
            if args.load:
                load_generated_files(result["files"])
                print("CSV files submitted through app.importer.import_seed_files.")
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
        return
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