"""Durable seed registration and identity phase barrier."""
from alembic import op

revision = "0027_identity_batches"
down_revision = "0026_privacy_workflows"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE identity_batches (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          phase text NOT NULL DEFAULT 'registering',
          mode text NOT NULL DEFAULT 'bulk',
          created_at timestamptz NOT NULL DEFAULT now(),
          finished_at timestamptz
        );
        ALTER TABLE imports ADD COLUMN identity_batch_id bigint REFERENCES identity_batches(id);
        CREATE INDEX ON imports(identity_batch_id);
    """)


def downgrade():
    op.execute("ALTER TABLE imports DROP COLUMN identity_batch_id; DROP TABLE identity_batches")
