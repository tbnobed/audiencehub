"""Checkpointed HubSpot inbound pages and outbound run summaries."""
from alembic import op

revision = "0040_hubspot"
down_revision = "0039_zeta_migrations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE hubspot_pulls (
        id bigserial PRIMARY KEY, source_id bigint NOT NULL REFERENCES sources(id),
        hub_id bigint NOT NULL, phase text NOT NULL DEFAULT 'collecting',
        since_ms bigint NOT NULL, until_ms bigint NOT NULL, after_id text NOT NULL DEFAULT '0',
        contacts_done boolean NOT NULL DEFAULT false, events_done boolean NOT NULL DEFAULT false,
        consent_after_id bigint NOT NULL DEFAULT 0,
        event_cursor text, manifests jsonb NOT NULL DEFAULT '[]', batch_id bigint,
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE UNIQUE INDEX ix_hubspot_active_pull ON hubspot_pulls(source_id)
        WHERE phase NOT IN ('completed','revoked');
      ALTER TABLE activation_runs ADD COLUMN hubspot_result jsonb NOT NULL DEFAULT '{}';
    """)


def downgrade():
    op.execute("ALTER TABLE activation_runs DROP COLUMN hubspot_result; DROP TABLE hubspot_pulls")
