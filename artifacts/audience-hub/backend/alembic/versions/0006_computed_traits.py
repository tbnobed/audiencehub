"""Dirty-profile queue and indexes for computed traits.

Revision ID: 0006_computed_traits
Revises: 0005_source_identity_ix
"""
from alembic import op
import sqlalchemy as sa


revision = "0006_computed_traits"
down_revision = "0005_source_identity_ix"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trait_dirty_profiles",
        sa.Column(
            "profile_id",
            sa.BigInteger(),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "dirtied_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_trait_dirty_profiles_dirtied_at",
        "trait_dirty_profiles",
        ["dirtied_at"],
    )
    op.create_index(
        "ix_gifts_profile_recurring_date",
        "gifts",
        ["profile_id", "gift_date"],
        postgresql_where=sa.text("is_recurring=true"),
    )
    op.create_index(
        "ix_gifts_source_profile",
        "gifts",
        ["source_id", "profile_id"],
    )
    op.create_index(
        "ix_events_source_profile",
        "events",
        ["source_id", "profile_id"],
    )


def downgrade():
    op.drop_index("ix_events_source_profile", table_name="events")
    op.drop_index("ix_gifts_source_profile", table_name="gifts")
    op.drop_index("ix_gifts_profile_recurring_date", table_name="gifts")
    op.drop_index("ix_trait_dirty_profiles_dirtied_at", table_name="trait_dirty_profiles")
    op.drop_table("trait_dirty_profiles")