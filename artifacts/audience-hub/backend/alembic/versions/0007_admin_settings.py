"""Persist the organization fiscal year start month.

Revision ID: 0007_admin_settings
Revises: 0006_computed_traits
"""
from alembic import op
import sqlalchemy as sa


revision = "0007_admin_settings"
down_revision = "0006_computed_traits"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "admin_settings",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column(
            "fiscal_year_start_month",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.CheckConstraint("id = 1", name="ck_admin_settings_singleton"),
        sa.CheckConstraint(
            "fiscal_year_start_month BETWEEN 1 AND 12",
            name="ck_admin_settings_fiscal_year_month",
        ),
    )
    op.execute(
        "INSERT INTO admin_settings (id, fiscal_year_start_month) VALUES (1, 1)"
    )


def downgrade():
    op.drop_table("admin_settings")