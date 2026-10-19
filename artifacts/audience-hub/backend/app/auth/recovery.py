"""Single-use, hashed recovery codes; verification runs under credential row lock."""
import re
import secrets
from sqlalchemy import text
from app.auth.passwords import token_hash, totp_counter
from app.activations.security import decrypt


def replace_codes(db, user_id):
    db.execute(text("DELETE FROM local_recovery_codes WHERE user_id=:id"), {"id": user_id})
    codes = []
    for _ in range(10):
        raw = secrets.token_hex(8)
        code = "-".join(raw[i:i+4] for i in range(0, 16, 4))
        db.execute(text("INSERT INTO local_recovery_codes(user_id,code_hash) VALUES(:id,:hash)"),
                   {"id": user_id, "hash": token_hash(f"recovery:{user_id}:{raw}")})
        codes.append(code)
    return codes


def verify(db, user_id, row, code):
    counter = totp_counter(decrypt(row["totp_secret"])["secret"], code, row["totp_counter"])
    if counter is not None:
        db.execute(text("UPDATE local_credentials SET totp_counter=:counter WHERE user_id=:id"),
                   {"counter": counter, "id": user_id})
        return True
    raw = (code or "").replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{16}", raw):
        return False
    return bool(db.execute(text("DELETE FROM local_recovery_codes WHERE user_id=:id AND code_hash=:hash"),
                           {"id": user_id, "hash": token_hash(f"recovery:{user_id}:{raw}")}).rowcount)
