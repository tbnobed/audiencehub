import time


def run(job_type: str, payload: dict, job_id: int | None = None) -> None:
    if job_type == "identity.resolve_batch":
        from sqlalchemy.orm import Session

        from app.db import engine
        from app.identity.resolver import resolve_batch

        with Session(engine) as db:
            limit = min(max(int(payload.get("limit", 10_000)), 1), 10_000)
            while True:
                result = resolve_batch(db, limit=limit, job_id=job_id)
                db.commit()
                # A complete component may not fit the remaining group space.
                # Underfull groups are not EOF; every call has its own commit.
                if result["records"] == 0:
                    break
        return
    if job_type == "traits.recompute":
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from app.db import engine
        from app.traits.engine import backfill_trait_snapshots, recompute_traits

        with Session(engine) as db:
            # Acquire before FOR UPDATE of dirty rows. Waiting for the lock
            # after selecting them can deadlock with a full recompute's cleanup.
            db.execute(text(
                "SELECT pg_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"
            ))
            if payload.get("mode") == "dirty":
                profile_ids = db.execute(text("""
                    SELECT profile_id FROM trait_dirty_profiles
                    WHERE dirtied_at <= clock_timestamp() - interval '10 minutes'
                    ORDER BY dirtied_at, profile_id
                    LIMIT 50000
                    FOR UPDATE SKIP LOCKED
                """)).scalars().all()
                if profile_ids:
                    recompute_traits(db, profile_ids=profile_ids)
                    db.execute(text("""
                        DELETE FROM trait_dirty_profiles WHERE profile_id=ANY(:profile_ids)
                    """), {"profile_ids": profile_ids})
            elif payload.get("mode") == "full":
                recompute_traits(db)
                backfill_trait_snapshots(db)
                db.execute(text("DELETE FROM trait_dirty_profiles"))
            else:
                raise ValueError("traits.recompute requires mode=dirty or mode=full")
            db.commit()
        # Import only after the trait transaction commits: this avoids coupling
        # dashboard route initialization to the job handler (and cache failures
        # can never invalidate data before a failed recompute).
        from app.dashboards.api import invalidate_cache

        invalidate_cache()
        return
    if job_type == "sleep":
        # Bounded, no-I/O delay for exercising manual worker recovery. Defaults
        # to 60 seconds; shorter values are useful for isolated integration tests.
        seconds = min(max(float(payload.get("seconds", 60)), 0), 60)
        time.sleep(seconds)
        return
    if job_type != "noop":
        raise ValueError("Unknown job type")
    # A bounded delay makes the running state visible without external I/O.
    time.sleep(min(max(float(payload.get("seconds", 0.4)), 0), 30))