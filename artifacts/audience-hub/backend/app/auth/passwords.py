"""Local password and RFC 6238 primitives; no external password service."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from pathlib import Path


def validate_password(value):
    if not isinstance(value, str) or len(value) < 14 or len(value) > 1024:
        raise ValueError("Password must contain 14–1024 characters.")
    blocked = set(Path(__file__).with_name("breached_passwords.txt").read_text().splitlines())
    normalized = value.casefold().strip()
    if normalized in blocked or len(set(normalized)) < 5:
        raise ValueError("Choose a password not present in the bundled breached-password list.")
    # Catch common breached bases with padding, years and punctuation.
    base = normalized.rstrip("0123456789!@#$%^&*()_-+. ")
    if base in blocked:
        raise ValueError("Choose a password not based on a common breached password.")


def hash_password(value):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode(), salt=salt, n=32768, r=8, p=1, maxmem=128*1024*1024)
    return "scrypt$32768$8$1$" + salt.hex() + "$" + digest.hex()


def check_password(value, encoded):
    try:
        scheme, n, r, p, salt, expected = (encoded or "").split("$")
        if scheme != "scrypt" or (n, r, p) != ("32768", "8", "1"):
            return False
        digest = hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=int(n),
                                r=int(r), p=int(p), maxmem=128*1024*1024)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_counter(secret, code, previous=-1, now=None):
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    current = int(time.time() if now is None else now) // 30
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    for counter in (current-1, current, current+1):
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 15
        expected = str((struct.unpack(">I", digest[offset:offset+4])[0] & 0x7fffffff) % 1000000).zfill(6)
        if counter > previous and hmac.compare_digest(expected, code):
            return counter
    return None
