"""Server-console bootstrap, never a public registration endpoint."""
import getpass
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import engine
from app.models import User
from app.auth.passwords import validate_password, hash_password
from app.auth.local import credentials, revoke, audit


def local_admin_command(args):
    if not __import__("sys").stdin.isatty():
        raise SystemExit("Use an interactive terminal: docker compose exec api kinship local-admin --email ...")
    password = getpass.getpass("New password (minimum 14 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    try:
        validate_password(password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    with Session(engine) as db:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('kinship:admin-users'))"))
        user_id = db.scalar(text("SELECT id FROM users WHERE lower(email)=:email"), {"email": args.email.strip().lower()})
        if user_id:
            if not args.reset_password:
                raise SystemExit("User exists; use --reset-password for explicit server-console recovery")
            user = db.get(User, user_id)
        else:
            from email_validator import validate_email
            email = validate_email(args.email, check_deliverability=False).normalized.lower()
            user = User(subject="local:" + str(uuid4()), email=email, name=args.name, role="admin", is_active=True)
            db.add(user)
            db.flush()
        previous_role = user.role
        user.role, user.is_active = "admin", True
        credentials(db, user.id)
        db.execute(text("""UPDATE local_credentials SET password_hash=:hash,failures=0,
            locked_until=NULL,password_changed_at=now() WHERE user_id=:id"""),
            {"hash": hash_password(password), "id": user.id})
        if args.reset_totp:
            db.execute(text("UPDATE local_credentials SET totp_secret=NULL,pending_totp=NULL,totp_counter=-1 WHERE user_id=:id"), {"id": user.id})
        revoke(db, user.id)
        db.execute(text("DELETE FROM local_account_tokens WHERE user_id=:id"), {"id": user.id})
        audit(db, "admin.console_recovery" if user_id else "admin.console_bootstrap", user.id,
              {"reset_totp": args.reset_totp})
        if previous_role != "admin":
            audit(db, "admin.role_changed", user.id, {"before": previous_role, "after": "admin", "via": "server_console"})
        db.commit()
    print("Local administrator ready. Previous sessions and account links revoked.")
