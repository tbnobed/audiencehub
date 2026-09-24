"""Index source-record identifiers for batch resolution and cardinality checks.

Revision ID: 0005_source_identity_ix
Revises: 0004_import_metrics
"""
from alembic import op

revision = "0005_source_identity_ix"
down_revision = "0004_import_metrics"
branch_labels = None
depends_on = None


def upgrade():
    # Concurrent builds allow imports to continue while the existing source
    # records table is indexed. Run them outside Alembic's migration transaction.
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_source_records_email_source_external
            ON source_records (email_norm, source_id, external_id)
        """)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_source_records_phone_source_external
            ON source_records (phone_e164, source_id, external_id)
        """)


def downgrade():
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_source_records_phone_source_external")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_source_records_email_source_external")