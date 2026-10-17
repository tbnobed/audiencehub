"""Planner statistics barrier between identity writes and downstream traits."""
from sqlalchemy import text


IDENTITY_STATISTICS_TABLES = (
    "profiles", "identifiers", "source_records", "gifts", "events",
    "consents", "enrichment_values", "profile_traits", "trait_dirty_profiles",
)


def refresh_identity_statistics(db):
    """Refresh post-attachment distributions in the identity transaction.

    Import-time ANALYZE sees profile_id entirely NULL in transaction tables.
    After attachment those statistics incorrectly estimate one-row joins and
    choose deeply nested loops for traits. Do not wait for autovacuum or the
    seed loader's final ANALYZE (which occurs only after traits finish).
    """
    for table in IDENTITY_STATISTICS_TABLES:
        db.execute(text(f"ANALYZE {table}"))
