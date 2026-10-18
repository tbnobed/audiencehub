"""Encrypted pull sources and durable content receipts."""
from alembic import op

revision = "0033_source_pulls"
down_revision = "0032_zeta_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE source_connections (
        source_id bigint PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
        credentials bytea NOT NULL,
        config jsonb NOT NULL,
        state jsonb NOT NULL DEFAULT '{}',
        updated_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE source_import_receipts (
        source_id bigint NOT NULL REFERENCES sources(id),
        digest text NOT NULL,
        record_type text NOT NULL,
        import_id bigint REFERENCES imports(id),
        completed boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(source_id,digest,record_type)
      );
    """)


def downgrade():
    op.execute("DROP TABLE source_import_receipts; DROP TABLE source_connections")
