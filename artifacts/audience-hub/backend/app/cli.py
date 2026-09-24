import argparse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import engine
from app.models import AuditLog, User


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin")
    admin.add_argument("--email", required=True)
    commands.add_parser("seed")
    args = parser.parse_args()
    if args.command == "seed":
        raise SystemExit("Synthetic seed generator is scheduled for Milestone 2; no data was written.")
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