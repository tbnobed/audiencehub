"""Optional organization dashboard default range (resolved by environment if unset)."""
from alembic import op
import sqlalchemy as sa

revision = "0023_dashboard_default_range"
down_revision = "0022_client_errors"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("admin_settings", sa.Column("dashboard_default_preset", sa.String(6), nullable=True))
    op.add_column("admin_settings", sa.Column("dashboard_default_from", sa.Date(), nullable=True))
    op.add_column("admin_settings", sa.Column("dashboard_default_to", sa.Date(), nullable=True))
    op.create_check_constraint(
        "ck_admin_settings_dashboard_default", "admin_settings",
        "(dashboard_default_preset IS NULL AND dashboard_default_from IS NULL AND dashboard_default_to IS NULL) "
        "OR (dashboard_default_preset = '90d' AND dashboard_default_from IS NULL AND dashboard_default_to IS NULL) "
        "OR (dashboard_default_preset = 'custom' AND dashboard_default_from IS NOT NULL "
        "AND dashboard_default_to IS NOT NULL AND dashboard_default_from >= DATE '1900-01-01' "
        "AND dashboard_default_to <= DATE '2999-12-31' "
        "AND dashboard_default_from <= dashboard_default_to "
        "AND dashboard_default_to - dashboard_default_from <= 3660)",
    )


def downgrade():
    op.drop_constraint("ck_admin_settings_dashboard_default", "admin_settings", type_="check")
    op.drop_column("admin_settings", "dashboard_default_to")
    op.drop_column("admin_settings", "dashboard_default_from")
    op.drop_column("admin_settings", "dashboard_default_preset")