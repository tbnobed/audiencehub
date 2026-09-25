"""Bounded privacy-safe client render diagnostics."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0022_client_errors"
down_revision = "0021_consent_channel_status"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_errors",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("message", sa.String(64), nullable=False),
        sa.Column("route", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column("component_stack", JSONB(), nullable=False),
    )
    op.create_index("ix_client_errors_created_at", "client_errors", ["created_at"])


def downgrade():
    op.drop_table("client_errors")