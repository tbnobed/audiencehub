"""Offline Fernet rewrapping. PII tombstones use retained HMAC read keys."""
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.config import get_settings
from app.db import engine
from app.keyring import fernet_ring
from app.models import AuditLog

ENCRYPTED = (
    ("meta_connections", "source_id", "credentials"),
    ("identity_device_credentials", "source_id", "secret_encrypted"),
    ("account_email_settings", "id", "api_key"),
    ("account_email_outbox", "id", "payload"),
    ("destinations", "id", "secret_encrypted"),
    ("source_connections", "source_id", "credentials"),
    ("activation_runs", "id", "snapshot_encrypted"),
    ("deletion_requests", "id", "identifier_encrypted"),
    ("local_credentials", "user_id", "totp_secret"),
    ("local_credentials", "user_id", "pending_totp"),
)


def rotate_fernet(*, yes=False):
    if not yes:
        raise ValueError("Offline rotation requires --yes, backups, and API/worker stopped")
    settings = get_settings()
    if not settings.fernet_keys_previous.strip():
        raise ValueError("Set FERNET_KEYS_PREVIOUS to the old key(s) through the server secret configuration first")
    current, ring = Fernet(settings.fernet_key.encode()), fernet_ring()
    counts = {}
    with Session(engine) as db:
        db.execute(text("SET LOCAL lock_timeout='5s'"))
        tables = ",".join(sorted({table for table, _, _ in ENCRYPTED} | {"jobs"}))
        db.execute(text(f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE NOWAIT"))
        if db.scalar(text("SELECT count(*) FROM jobs WHERE status='running'")):
            raise ValueError("Rotation refused: recover/finish running jobs with workers stopped first")
        for table, pk, column in ENCRYPTED:
            after, count = 0, 0
            while True:
                rows = db.execute(text(f"""SELECT {pk} AS key,{column} AS value FROM {table}
                    WHERE {pk}>:after AND {column} IS NOT NULL ORDER BY {pk} LIMIT 500"""), {"after": after}).mappings().all()
                if not rows:
                    break
                for row in rows:
                    raw = bytes(row["value"])
                    try:
                        current.decrypt(raw)
                    except InvalidToken:
                        replacement = ring.rotate(raw)
                        db.execute(text(f"UPDATE {table} SET {column}=:value WHERE {pk}=:id"),
                                   {"value": replacement, "id": row["key"]})
                        count += 1
                after = rows[-1]["key"]
            counts[f"{table}.{column}"] = count
        db.add(AuditLog(actor_type="system", action="maintenance.fernet_rotated", entity_type="keys", details=counts))
        db.commit()
    return counts
