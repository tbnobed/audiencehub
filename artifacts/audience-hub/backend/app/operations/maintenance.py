"""Bounded, restartable daily maintenance through the existing worker queue."""
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db import engine
from app.jobs.queue import heartbeat, enqueue
from app.identity.locking import IdentityResolutionDeferred
from app.privacy.locking import privacy_shared


def month_after(value, offset):
    index = value.year * 12 + value.month - 1 + offset
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=timezone.utc)


def partitions(db, ahead):
    """Migrate any default-partition rows atomically before attaching a month."""
    current = datetime.now(timezone.utc)
    created = []
    for offset in range(ahead + 1):
        start, end = month_after(current, offset), month_after(current, offset+1)
        name = f"events_{start.year:04d}_{start.month:02d}"
        if db.scalar(text("SELECT to_regclass(:name)"), {"name": name}):
            continue
        # This lock also covers inserts into the default partition. A concurrent
        # creator or writer cannot race the delete/attach operation.
        db.execute(text("LOCK TABLE events IN ACCESS EXCLUSIVE MODE"))
        if db.scalar(text("SELECT to_regclass(:name)"), {"name": name}):
            continue
        db.execute(text(f"CREATE TABLE {name} (LIKE events INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING GENERATED)"))
        db.execute(text(f"""WITH moved AS (
            DELETE FROM events_default WHERE occurred_at>=:start AND occurred_at<:end RETURNING *
        ) INSERT INTO {name} SELECT * FROM moved"""), {"start": start, "end": end})
        db.execute(text(f"""ALTER TABLE events ATTACH PARTITION {name}
            FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"""))
        created.append(name)
    return created


def run(job_id):
    totals = {}
    with engine.connect() as mutex:
        if not mutex.scalar(text("SELECT pg_try_advisory_lock(hashtext('kinship:maintenance'))")):
            raise IdentityResolutionDeferred("Another maintenance run is active")
        mutex.commit()
        try:
            with Session(engine) as db:
                policy = dict(db.execute(text("SELECT * FROM operations_policy WHERE id=1")).mappings().one())
                db.execute(text("SET LOCAL lock_timeout='5s'"))
                if not heartbeat(db, job_id, {"phase": "partitions"}):
                    raise RuntimeError("Maintenance ownership lost")
                totals["partitions_created"] = partitions(db, policy["partitions_ahead_months"])
                db.commit()
            stages = ["licensed_sources", "enrichment", "events", "audit", "jobs", "sessions"]
            for stage in stages:
                total = 0
                while True:
                    with Session(engine) as db:
                        privacy_shared(db)
                        if not heartbeat(db, job_id, {"phase": stage, "rows_removed": total, "totals": totals}):
                            raise RuntimeError("Maintenance ownership lost")
                        if not db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))")):
                            raise IdentityResolutionDeferred("Maintenance waits for trait computation")
                        count = clean_batch(db, stage, policy, job_id)
                        db.commit()
                    total += count
                    if count < 5000:
                        break
                totals[stage] = total
            # Existing durable upload/export retention includes filesystem cleanup
            # and staged HubSpot/Zeta privacy artifacts.
            from app.privacy.jobs import run_upload_retention
            from app.activations.service import expire_exports
            from app.operations.files import cleanup_registered_files
            cleanup_registered_files(engine)
            run_upload_retention()
            with Session(engine) as db:
                expire_exports(db)
                enqueue(db, "traits.recompute", {"mode": "dirty"}, dedupe_key="traits:incremental")
                heartbeat(db, job_id, {"phase": "completed", "totals": totals})
                db.commit()
        finally:
            mutex.execute(text("SELECT pg_advisory_unlock(hashtext('kinship:maintenance'))"))
            mutex.commit()


def clean_batch(db, stage, policy, job_id):
    if stage == "licensed_sources":
        from app.identity.master import SYSTEM_KEYS
        keys = list(SYSTEM_KEYS) + ["email_md5", "email_sha256", "_import_consent", "_hubspot_preferences"]
        rows = db.execute(text("""WITH doomed AS (
            SELECT r.id FROM source_records r JOIN sources s ON s.id=r.source_id
            WHERE s.license_expires_at<current_date AND (r.attributes-CAST(:keys AS text[]))<>'{}'::jsonb
            LIMIT 5000
        ) UPDATE source_records r SET attributes=COALESCE((
            SELECT jsonb_object_agg(k,v) FROM jsonb_each(r.attributes) AS a(k,v) WHERE k=ANY(CAST(:keys AS text[]))
        ),'{}'::jsonb) FROM doomed d WHERE r.id=d.id RETURNING r.profile_id"""), {"keys": keys}).all()
        ids = sorted({row[0] for row in rows if row[0] is not None})
    elif stage == "enrichment":
        days = policy["enrichment_retention_days"]
        rows = db.execute(text("""WITH doomed AS (
            SELECT e.ctid,e.tableoid FROM enrichment_values e JOIN sources s ON s.id=e.source_id
            WHERE e.license_expires_at<current_date OR s.license_expires_at<current_date
               OR (:days>0 AND e.imported_at<now()-make_interval(days=>:days))
            LIMIT 5000
        ) DELETE FROM enrichment_values e USING doomed d
          WHERE e.ctid=d.ctid AND e.tableoid=d.tableoid
          RETURNING e.profile_id,e.source_id,e.attribute_key"""), {"days": days}).all()
        for profile_id, source_id, key in rows:
            # Erase the licensed value from retained evidence so a later resolver
            # pass cannot resurrect it. Keep identity/provenance columns intact.
            db.execute(text("""UPDATE source_records SET
                attributes=(attributes - :key) #- ARRAY['_import_enrichments',:key]
                WHERE profile_id=:profile AND source_id=:source AND
                (attributes ? :key OR attributes->'_import_enrichments' ? :key)"""),
                {"key": key, "profile": profile_id, "source": source_id})
        ids = sorted({row[0] for row in rows})
    elif stage == "events":
        days = policy["event_retention_days"]
        if not days:
            return 0
        # Never expire financial evidence: audited donor audiences need it.
        rows = db.execute(text("""WITH doomed AS (
            SELECT id,occurred_at FROM events WHERE occurred_at<now()-make_interval(days=>:days)
            AND name NOT IN ('donation','donation_return') LIMIT 5000
        ) DELETE FROM events e USING doomed d WHERE e.id=d.id AND e.occurred_at=d.occurred_at
          RETURNING e.profile_id"""), {"days": days}).all()
        ids = sorted({row[0] for row in rows if row[0] is not None})
    elif stage == "audit":
        days = policy["audit_retention_days"]
        if not days:
            return 0
        return db.execute(text("""DELETE FROM audit_log WHERE id IN (
            SELECT id FROM audit_log WHERE at<now()-make_interval(days=>:days) LIMIT 5000)"""),
            {"days": days}).rowcount
    elif stage == "jobs":
        days = policy["job_retention_days"]
        if not days:
            return 0
        # Keep every job referenced by a source/import or durable schedule receipt.
        return db.execute(text("""DELETE FROM jobs j WHERE j.id IN (
            SELECT id FROM jobs WHERE status IN ('succeeded','failed','cancelled')
            AND finished_at<now()-make_interval(days=>:days) AND id<>:job
            AND NOT EXISTS(SELECT 1 FROM imports i WHERE i.job_id=jobs.id)
            AND NOT EXISTS(SELECT 1 FROM scheduled_runs s WHERE s.job_id=jobs.id) LIMIT 5000)"""),
            {"days": days, "job": job_id}).rowcount
    else:
        count = db.execute(text("""DELETE FROM local_sessions WHERE token_hash IN (
            SELECT token_hash FROM local_sessions WHERE last_seen<now()-interval '8 hours' LIMIT 5000)""")).rowcount
        db.execute(text("DELETE FROM local_account_tokens WHERE expires_at<now()"))
        db.execute(text("DELETE FROM local_login_challenges WHERE expires_at<now()"))
        db.execute(text("""UPDATE account_email_outbox SET payload=NULL,cancelled_at=now()
            WHERE payload IS NOT NULL AND expires_at<=now()"""))
        db.execute(text("""DELETE FROM account_email_outbox WHERE payload IS NULL
            AND created_at<now()-interval '90 days'"""))
        db.execute(text("""UPDATE local_credentials SET pending_totp=NULL,pending_totp_until=NULL
            WHERE pending_totp_until<now()"""))
        db.execute(text("DELETE FROM local_login_limits WHERE window_started<now()-interval '2 days' AND (locked_until IS NULL OR locked_until<now())"))
        return count
    if ids:
        db.execute(text("""INSERT INTO trait_dirty_profiles(profile_id,dirtied_at)
            SELECT unnest(CAST(:ids AS bigint[])),now()
            ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at"""), {"ids": ids})
    return len(rows)
