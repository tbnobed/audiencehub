"""Durable import batch cursor and transactional diagnostics."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_import_checkpoints"
down_revision = "0007_admin_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("imports", sa.Column("last_committed_record_number", sa.BigInteger(),
                                      nullable=False, server_default="1"))
    op.create_table(
        "import_batch_diagnostics",
        sa.Column("import_id", sa.BigInteger(), sa.ForeignKey("imports.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("record_number", sa.BigInteger(), primary_key=True),
        sa.Column("diagnostics", JSONB(), nullable=False),
    )


def downgrade():
    op.drop_table("import_batch_diagnostics")
    op.drop_column("imports", "last_committed_record_number")