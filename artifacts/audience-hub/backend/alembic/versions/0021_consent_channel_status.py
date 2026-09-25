"""Index the authoritative consent counts by channel and status."""
from alembic import op

revision = "0021_consent_channel_status"
down_revision = "0020_dashboard_rollups"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_consents_channel_status", "consents", ["channel", "status"])
    # Historical contact imports stored email_consent as an ordinary attribute;
    # the old resolver materialized only _import_consent (ESP/suppression rows).
    # Run once at deployment, never in dashboard requests. Never infer permission
    # from an address or revive an explicit opt-out already in consents.
    op.execute("""
        WITH observations AS (
            SELECT sr.profile_id, sr.source_id, sr.id, sr.updated_at,
                CASE
                    WHEN lower(sr.attributes->>'hard_bounce') IN ('true','yes','y','1','on')
                        THEN 'opted_out'
                    WHEN sr.attributes->>'email_consent' = 'opted_out'
                        THEN 'opted_out'
                    WHEN sr.attributes #>> '{_import_consent,channel}' = 'email'
                        THEN sr.attributes #>> '{_import_consent,status}'
                    ELSE sr.attributes->>'email_consent'
                END AS status
            FROM source_records sr
            WHERE sr.profile_id IS NOT NULL
        ), preferred AS (
            SELECT DISTINCT ON (profile_id) *
            FROM observations
            WHERE status IN ('opted_in', 'opted_out', 'unknown')
            ORDER BY profile_id, (status = 'opted_out') DESC, updated_at DESC, id DESC
        )
        INSERT INTO consents (profile_id, channel, status, source_id, captured_at, evidence)
        SELECT profile_id, 'email', status, source_id, updated_at,
            jsonb_build_object('source_record_id', id, 'backfill', '0021',
                               'capture_time_basis', 'source_record_updated_at')
        FROM preferred
        ON CONFLICT (profile_id, channel) DO UPDATE SET
            status = EXCLUDED.status, source_id = EXCLUDED.source_id,
            captured_at = EXCLUDED.captured_at, evidence = EXCLUDED.evidence
        WHERE EXCLUDED.status = 'opted_out' AND consents.status <> 'opted_out'
    """)


def downgrade():
    op.drop_index("ix_consents_channel_status", table_name="consents")