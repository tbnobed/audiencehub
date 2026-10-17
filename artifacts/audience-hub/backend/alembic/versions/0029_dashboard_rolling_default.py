"""Allow a rolling twelve-month dashboard default without changing saved choices."""
from alembic import op

revision = "0029_dashboard_rolling_default"
down_revision = "0028_event_ingestion"
branch_labels = None
depends_on = None


def _constraint(relative_presets):
    return (
        "(dashboard_default_preset IS NULL AND dashboard_default_from IS NULL AND dashboard_default_to IS NULL) "
        "OR (dashboard_default_preset IS NOT NULL AND ("
        f"(dashboard_default_preset IN ({relative_presets}) AND dashboard_default_from IS NULL AND dashboard_default_to IS NULL) "
        "OR (dashboard_default_preset = 'custom' AND dashboard_default_from IS NOT NULL "
        "AND dashboard_default_to IS NOT NULL AND dashboard_default_from >= DATE '1900-01-01' "
        "AND dashboard_default_to <= DATE '2999-12-31' "
        "AND dashboard_default_from <= dashboard_default_to "
        "AND dashboard_default_to - dashboard_default_from <= 3660)))"
    )


def upgrade():
    op.drop_constraint("ck_admin_settings_dashboard_default", "admin_settings", type_="check")
    op.create_check_constraint(
        "ck_admin_settings_dashboard_default", "admin_settings", _constraint("'90d', '12m'")
    )


def downgrade():
    # An unset default is the only compatible representation in the older schema.
    op.execute("UPDATE admin_settings SET dashboard_default_preset = NULL WHERE dashboard_default_preset = '12m'")
    op.drop_constraint("ck_admin_settings_dashboard_default", "admin_settings", type_="check")
    op.create_check_constraint(
        "ck_admin_settings_dashboard_default", "admin_settings", _constraint("'90d'")
    )
