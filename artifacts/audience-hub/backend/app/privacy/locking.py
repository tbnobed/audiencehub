from sqlalchemy import text


def privacy_shared(db):
    """Acquire before identity/trait/segment locks; held until transaction ends."""
    db.execute(text("SELECT pg_catalog.pg_advisory_xact_lock_shared(hashtext(current_schema() || :scope))"),
               {"scope": ":audience-hub:privacy"})


def privacy_exclusive(db):
    """Serialize privacy changes against selection, sending and import writes."""
    db.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(hashtext(current_schema() || :scope))"),
               {"scope": ":audience-hub:privacy"})
