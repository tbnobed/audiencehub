"""Stable editable partner audience template identities."""
from alembic import op
import sqlalchemy as sa
revision = "0024_segment_templates"
down_revision = "0023_dashboard_default_range"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("segments", sa.Column("template_key", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_segments_template_key", "segments", ["template_key"])


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("uq_segments_template_key", "segments", type_="unique")
    op.drop_column("segments", "template_key")