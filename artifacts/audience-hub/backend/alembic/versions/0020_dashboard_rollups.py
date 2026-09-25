"""Persist dashboard aggregates and shared cache."""
from alembic import op

revision = "0020_dashboard_rollups"
down_revision = "0008_import_checkpoints"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE dashboard_daily (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      day date NOT NULL, source_id bigint NOT NULL, fund text, campaign text,
      channel text, gift_count bigint NOT NULL, gift_amount numeric NOT NULL,
      donor_count bigint NOT NULL, new_donor_count bigint NOT NULL,
      active boolean NOT NULL);
    CREATE INDEX ON dashboard_daily(day);
    CREATE TABLE dashboard_donor_daily (
      profile_id bigint NOT NULL, day date NOT NULL, gift_count bigint NOT NULL,
      gift_amount numeric NOT NULL, recurring boolean NOT NULL,
      PRIMARY KEY(profile_id,day));
    CREATE INDEX ON dashboard_donor_daily(day,profile_id);
    CREATE TABLE dashboard_donor_patterns (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      days date[] NOT NULL, gift_counts bigint[] NOT NULL,
      recurring_flags boolean[] NOT NULL, donors bigint NOT NULL);
    CREATE TABLE dashboard_giving_daily (
      day date NOT NULL, appeal_code text, bucket text NOT NULL,
      sort_order integer NOT NULL, gift_count bigint NOT NULL, gift_amount numeric NOT NULL);
    CREATE INDEX ON dashboard_giving_daily(day);
    CREATE TABLE dashboard_event_daily (
      day date NOT NULL, source text NOT NULL, name text NOT NULL, type text NOT NULL,
      events bigint NOT NULL);
    CREATE INDEX ON dashboard_event_daily(day);
    CREATE TABLE dashboard_conversion_daily(day date PRIMARY KEY, conversions bigint NOT NULL);
    CREATE TABLE dashboard_kpis (
      as_of date NOT NULL, key text NOT NULL, value jsonb NOT NULL,
      PRIMARY KEY(as_of,key));
    CREATE TABLE dashboard_cache (
      key text PRIMARY KEY, payload jsonb NOT NULL, computed_at timestamptz NOT NULL,
      generation text NOT NULL);
    """)


def downgrade():
    for table in ("dashboard_cache", "dashboard_kpis", "dashboard_conversion_daily",
                  "dashboard_event_daily", "dashboard_giving_daily",
                  "dashboard_donor_patterns", "dashboard_donor_daily", "dashboard_daily"):
        op.execute(f"DROP TABLE {table}")