"""Indexed latest non-returned donation; committed 5,000-profile batches."""
from alembic import op
from sqlalchemy import text

revision = "0037_last_donation_at"
down_revision = "0036_segment_properties"
branch_labels = None
depends_on = None

# Frozen equivalent of app.segments.donations.UPDATE_LAST_DONATION.
BACKFILL = """
WITH returned AS MATERIALIZED (
  SELECT r.profile_id,r.source_id,ref.value
  FROM events r CROSS JOIN LATERAL (VALUES
    (r.properties->>'original_donation_id'),
    (r.properties->>'original_gift_external_id')) ref(value)
  WHERE r.profile_id=ANY(CAST(:ids AS bigint[]))
    AND r.name='donation_return' AND ref.value IS NOT NULL
  UNION ALL
  SELECT r.profile_id,r.source_id,ref.value
  FROM gifts r CROSS JOIN LATERAL (VALUES
    (r.attributes->>'original_donation_id'),
    (r.attributes->>'original_gift_external_id')) ref(value)
  WHERE r.profile_id=ANY(CAST(:ids AS bigint[]))
    AND (r.amount<0 OR r.attributes->>'is_refund'='true')
    AND ref.value IS NOT NULL
), donations AS (
  SELECT e.profile_id,max(e.occurred_at) AS latest
  FROM events e
  WHERE e.profile_id=ANY(CAST(:ids AS bigint[])) AND e.name='donation'
    AND COALESCE(e.properties->>'is_refund','false')<>'true'
    AND NOT EXISTS (
      SELECT 1 FROM returned r WHERE r.profile_id=e.profile_id
        AND r.source_id=e.source_id AND r.value=ANY(ARRAY[
          e.message_id,'gift:'||e.message_id,'donation:'||e.message_id,
          'donation:_event:'||e.message_id])
    )
  GROUP BY e.profile_id
)
UPDATE profiles p SET last_donation_at=d.latest
FROM (
  SELECT target.id,donations.latest
  FROM unnest(CAST(:ids AS bigint[])) target(id)
  LEFT JOIN donations ON donations.profile_id=target.id
) d WHERE p.id=d.id AND p.last_donation_at IS DISTINCT FROM d.latest
"""


def upgrade():
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(text("SET lock_timeout='5s'"))
        try:
            bind.execute(text(
                "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS last_donation_at timestamptz"
            ))
            cursor = 0
            while True:
                ids = list(bind.execute(text("""
                    SELECT id FROM active_profiles WHERE id>:cursor ORDER BY id LIMIT 5000
                """), {"cursor": cursor}).scalars())
                if not ids:
                    break
                bind.execute(text(BACKFILL), {"ids": ids})
                cursor = ids[-1]
                print(f"Last donation backfilled through profile {cursor}", flush=True)
            invalid = bind.scalar(text("""
                SELECT NOT i.indisvalid FROM pg_index i
                JOIN pg_class c ON c.oid=i.indexrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE c.relname='ix_profiles_last_donation_at'
                  AND n.nspname=current_schema()
            """))
            if invalid:
                bind.execute(text("DROP INDEX CONCURRENTLY IF EXISTS ix_profiles_last_donation_at"))
            bind.execute(text("""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_profiles_last_donation_at
                ON profiles(last_donation_at,id)
            """))
            bind.execute(text("ANALYZE profiles"))
        finally:
            bind.execute(text("RESET lock_timeout"))


def downgrade():
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_profiles_last_donation_at")
        op.execute("ALTER TABLE profiles DROP COLUMN IF EXISTS last_donation_at")
