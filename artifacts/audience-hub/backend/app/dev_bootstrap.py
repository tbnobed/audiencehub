"""Stable development keys, passed only through the child process environment."""
import base64
import hashlib
import hmac
import os
import sys


KEYS = ("SECRET_KEY", "FERNET_KEY", "PII_HASH_PEPPER")


def development_keys(environment):
    if environment.get("APP_ENV") == "production":
        raise ValueError("Development key derivation must not run in production.")
    missing = [key for key in KEYS if not environment.get(key)]
    master = environment.get("SESSION_SECRET")
    if missing and not master:
        raise ValueError(
            "Set a stable SESSION_SECRET, or explicitly set SECRET_KEY, FERNET_KEY "
            "and PII_HASH_PEPPER, before starting development. Ephemeral keys are not safe."
        )
    result = {key: environment[key] for key in KEYS if environment.get(key)}
    for key in missing:
        derived = hmac.new(master.encode(), f"kinship:development:v1:{key}".encode(), hashlib.sha256).digest()
        result[key] = base64.urlsafe_b64encode(derived).decode() if key == "FERNET_KEY" else derived.hex()
    return result


def main():
    try:
        keys = development_keys(os.environ)
    except ValueError as exc:
        # Only fixed actionable messages, never keys or environment contents.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    os.environ.update(keys)
    os.execvp("bash", ["bash", *sys.argv[1:]])


if __name__ == "__main__":
    main()
