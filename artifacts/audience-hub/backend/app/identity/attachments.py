"""Transactions are attachments, never identity graph vertices.

No-match and ambiguous rows are terminal attempts (resolved_at is set); later
identity passes may attach them, but they never keep the work queue alive.
"""
import time
from sqlalchemy import text
from app.identity.locking import acquire_resolver_lock


def attach_transactions(db, progress_callback=None):
    acquire_resolver_lock(db)
    statements = """
        DROP TABLE IF EXISTS pg_temp.ia_assign;
        CREATE TEMP TABLE ia_assign ON COMMIT DROP AS
        WITH pending AS MATERIALIZED (
          SELECT sr.*, i.record_type, s.key AS source_key
          FROM source_records sr JOIN imports i ON i.id=sr.last_import_id
          JOIN sources s ON s.id=sr.source_id
          WHERE i.record_type IN ('gift','event')
            AND (sr.resolved_at IS NULL OR sr.profile_id IS NULL)
        ), keys AS (
          SELECT p.id,k.rank,k.kind,k.value
          FROM pending p CROSS JOIN LATERAL (VALUES
            (0, 'external', COALESCE(p.attributes->'_identity_reference'->>'source_key',p.source_key)
              || ':' || NULLIF(p.attributes->'_identity_reference'->>'contact_external_id','')),
            (1, 'email', NULLIF(lower(btrim(p.email_norm::text)),'')),
            (1, 'phone', NULLIF(p.phone_e164,''))
          ) k(rank,kind,value)
          WHERE k.value IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM identifier_blocklist b
                            WHERE b.type=k.kind AND b.value=k.value)
        ), candidates AS (
          SELECT k.id,k.rank,owner.id AS profile_id FROM keys k
          JOIN identity_xref x ON x.key_type=
            CASE k.kind WHEN 'phone' THEN 'mobile_phone' ELSE k.kind END AND x.key_value=k.value
          JOIN profiles owner ON owner.master_id=x.master_id
            AND NOT owner.is_deleted AND owner.merged_into_id IS NULL
          UNION
          SELECT k.id,k.rank,i.profile_id FROM keys k
          JOIN identifiers i ON i.type=k.kind AND i.value=k.value
          JOIN profiles owner ON owner.id=i.profile_id
            AND NOT owner.is_deleted AND owner.merged_into_id IS NULL
          WHERE NOT EXISTS (SELECT 1 FROM identity_xref x WHERE x.key_type=
            CASE k.kind WHEN 'phone' THEN 'mobile_phone' ELSE k.kind END AND x.key_value=k.value)
        ), ranked AS (
          SELECT *, min(rank) OVER (PARTITION BY id) AS best FROM candidates
        ), winners AS (
          SELECT id, CASE WHEN count(DISTINCT profile_id)=1 THEN min(profile_id) END AS profile_id
          FROM ranked WHERE rank=best GROUP BY id
        )
        SELECT p.id,p.source_id,p.external_id,
          COALESCE(p.attributes->>'_event_message_id',p.raw_hash) AS message_id,
          p.record_type,w.profile_id
        FROM pending p LEFT JOIN winners w USING(id);
        CREATE UNIQUE INDEX ON ia_assign(id);
        ANALYZE ia_assign;
        UPDATE gifts g SET profile_id=a.profile_id FROM ia_assign a
        WHERE a.record_type='gift' AND g.source_record_id=a.id
          AND a.profile_id IS NOT NULL AND g.profile_id IS DISTINCT FROM a.profile_id;
        UPDATE events e SET profile_id=a.profile_id FROM (
          SELECT source_id,message_id,min(profile_id) AS profile_id
          FROM ia_assign WHERE record_type='event'
          GROUP BY source_id,message_id HAVING count(DISTINCT profile_id)=1
        ) a WHERE e.source_id=a.source_id AND e.message_id=a.message_id
          AND e.profile_id IS DISTINCT FROM a.profile_id;
        UPDATE source_records sr SET profile_id=a.profile_id,resolved_at=now()
        FROM ia_assign a WHERE sr.id=a.id
          AND (sr.resolved_at IS NULL OR sr.profile_id IS DISTINCT FROM a.profile_id);
        INSERT INTO trait_dirty_profiles(profile_id,dirtied_at)
        SELECT profile_id,clock_timestamp() FROM (
          SELECT DISTINCT profile_id FROM ia_assign WHERE profile_id IS NOT NULL
        ) dirty
        ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at;
    """
    steps = (
        "drop_assignment", "assignment", "index_assignment", "analyze_assignment",
        "gifts", "events", "source_records", "dirty_profiles",
    )
    # Execute the unchanged SQL statements separately so a long-running UPDATE
    # is identifiable in pg_stat_activity and durable job progress. There is
    # still exactly one UPDATE per table and one caller-owned transaction.
    sql_steps = [sql.strip() for sql in statements.split(";") if sql.strip()]
    if len(sql_steps) != len(steps):
        raise RuntimeError("Attachment SQL and progress steps are inconsistent")
    for step, sql in zip(steps, sql_steps):
        started = time.perf_counter()
        if progress_callback:
            progress_callback(step, "running", 0.0)
        try:
            db.execute(text(sql))
        except Exception:
            if progress_callback:
                progress_callback(step, "failed", (time.perf_counter() - started) * 1000)
            raise
        if progress_callback:
            progress_callback(step, "completed", (time.perf_counter() - started) * 1000)
