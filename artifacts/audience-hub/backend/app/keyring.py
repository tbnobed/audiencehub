"""Versionless read keyrings: current key writes, retained keys read."""
import hashlib
import hmac
from cryptography.fernet import Fernet, MultiFernet
from app.config import get_settings


def fernet_ring():
    settings = get_settings()
    keys = [settings.fernet_key, *getattr(settings, "fernet_keys_previous", "").split(",")]
    return MultiFernet([Fernet(key.strip().encode()) for key in keys if key.strip()])


def pii_hashes(value, primary=None):
    settings = get_settings()
    keys = [primary or settings.pii_hash_pepper,
            *(key.strip() for key in getattr(settings, "pii_hash_peppers_previous", "").split(",") if key.strip())]
    return list(dict.fromkeys(hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()
                             for key in keys if key))


def pii_matches(value, expected):
    return any(hmac.compare_digest(candidate, expected) for candidate in pii_hashes(value))
