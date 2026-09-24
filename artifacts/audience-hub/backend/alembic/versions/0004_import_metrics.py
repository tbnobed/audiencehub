"""Track import warnings, normalization, deduplication, and elapsed time.

Revision ID: 0004_import_metrics
Revises: 0003_historical_event_partitions
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_import_metrics"
down_revision = "0003_historical_event_partitions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("imports", sa.Column(
        "warning_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("imports", sa.Column(
        "warning_counts", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb")))
    op.add_column("imports", sa.Column(
        "rows_normalized", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("imports", sa.Column(
        "rows_deduplicated", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("imports", sa.Column("duration_ms", sa.Integer()))


def downgrade():
    for column in (
        "duration_ms", "rows_deduplicated", "rows_normalized",
        "warning_counts", "warning_count",
    ):
        op.drop_column("imports", column)