"""Cross-partition ingestion retry receipts; no raw identifiers or payload."""
from alembic import op

revision = "0028_event_ingestion"
down_revision = "0027_identity_batches"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE ingestion_receipts (
        source_id bigint NOT NULL REFERENCES sources(id),
        message_hash text NOT NULL,
        PRIMARY KEY(source_id,message_hash)
      );
      CREATE UNIQUE INDEX uq_sources_write_key_hash ON sources(write_key_hash)
        WHERE write_key_hash IS NOT NULL;
      CREATE INDEX ix_events_source_id ON events(source_id,id DESC);
    """)


def downgrade():
    op.execute("DROP INDEX ix_events_source_id; DROP INDEX uq_sources_write_key_hash; DROP TABLE ingestion_receipts")
