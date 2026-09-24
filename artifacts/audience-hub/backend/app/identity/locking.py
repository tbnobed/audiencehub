"""Import/resolver coordination, including callers outside the job worker."""

from contextlib import contextmanager

from sqlalchemy import text


IDENTITY_IMPORT_LOCK_KEY = 0x41484944454E54


class IdentityResolutionDeferred(Exception):
    """Identity work must yield to an import or another resolver."""


@contextmanager
def import_identity_lock(engine):
    """Hold a shared session lock across every transaction of an import.

    A dedicated connection keeps commits in the import session from releasing
    the lock. Never return a locked connection to the pool, even on failure.
    """
    with engine.connect() as connection:
        acquired = False
        try:
            connection.execute(
                text("SELECT pg_advisory_lock_shared(:key)"),
                {"key": IDENTITY_IMPORT_LOCK_KEY},
            )
            acquired = True
            connection.commit()
            yield
        finally:
            if acquired:
                try:
                    connection.rollback()
                    connection.execute(
                        text("SELECT pg_advisory_unlock_shared(:key)"),
                        {"key": IDENTITY_IMPORT_LOCK_KEY},
                    )
                    connection.commit()
                except BaseException:
                    connection.invalidate()
                    raise


def acquire_resolver_lock(db):
    """Nonblocking exclusive transaction lock; held until commit/rollback."""
    if not db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": IDENTITY_IMPORT_LOCK_KEY},
    ):
        raise IdentityResolutionDeferred("Identity resolution is waiting for imports or another resolver")