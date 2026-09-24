"""Import/resolver coordination, including callers outside the job worker."""

from contextlib import contextmanager

from sqlalchemy import text


IDENTITY_IMPORT_LOCK_KEY = 0x41484944454E54
IDENTIFIER_LOCK_NAMESPACE = 0x41484944
IDENTIFIER_LOCK_BUCKETS = 1024


class IdentityResolutionDeferred(Exception):
    """Identity work must yield to an import or another resolver."""


@contextmanager
def import_identity_lock(engine):
    """Compatibility context for ONE batch, never an entire import.

    New import code should acquire_import_lock on its writing session instead.
    This context ends the transaction (and releases its lock) on every exit.
    """
    with engine.begin() as connection:
        acquire_import_lock(connection)
        yield


def acquire_import_lock(db):
    """Acquire on the writing session at the start of each import batch."""
    db.execute(
        text("SELECT pg_advisory_xact_lock_shared(:key)"),
        {"key": IDENTITY_IMPORT_LOCK_KEY},
    )


def acquire_identifier_locks(db, identifiers):
    """Take bounded, distinct bucket locks in transaction-wide numeric order.

    Pass the union of ALL component identifiers in this transaction, not one
    component at a time: lexical key order is not hash bucket order.
    PostgreSQL computes hashtext so Python/platform hash seeds cannot differ.
    """
    keys = sorted({f"{kind}:{value}" for kind, value in identifiers})
    if keys:
        db.execute(
            text("""
                WITH buckets AS MATERIALIZED (
                    SELECT DISTINCT
                      ((hashtext(lock_key) % :buckets) + :buckets) % :buckets AS bucket
                    FROM unnest(CAST(:keys AS text[])) AS keys(lock_key)
                ), ordered_buckets AS MATERIALIZED (
                    SELECT bucket FROM buckets ORDER BY bucket
                )
                SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), bucket)
                FROM ordered_buckets
            """),
            {"keys": keys, "buckets": IDENTIFIER_LOCK_BUCKETS,
             "namespace": IDENTIFIER_LOCK_NAMESPACE},
        )


def acquire_resolver_lock(db):
    """Nonblocking exclusive transaction lock; held until commit/rollback."""
    if not db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": IDENTITY_IMPORT_LOCK_KEY},
    ):
        raise IdentityResolutionDeferred("Identity resolution is waiting for imports or another resolver")