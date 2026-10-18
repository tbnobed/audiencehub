"""Rebuild profile-backed views after recently added projection columns."""
from alembic import op
from app.migration_views import refresh_profile_views

revision = "0038_refresh_profile_views"
down_revision = "0037_last_donation_at"
branch_labels = None
depends_on = None


def upgrade():
    refresh_profile_views(op.get_bind())


def downgrade():
    # Column-removal migrations own their view refresh too. Keeping the current
    # full column list here is safe and does not discard data.
    refresh_profile_views(op.get_bind())
