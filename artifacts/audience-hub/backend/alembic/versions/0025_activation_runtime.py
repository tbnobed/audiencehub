"""Activation execution snapshots, funnels and schedules."""
from alembic import op

revision = "0025_activation_runtime"
down_revision = "0024_segment_templates"
branch_labels = depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("""
      ALTER TABLE activations ADD COLUMN name text NOT NULL DEFAULT 'Partner activation',
        ADD COLUMN timezone text NOT NULL DEFAULT 'America/Chicago',
        ADD COLUMN column_order text[] NOT NULL DEFAULT '{}';
      ALTER TABLE activation_runs ADD COLUMN profiles_excluded_missing_fields integer NOT NULL DEFAULT 0,
        ADD COLUMN snapshot_encrypted bytea,
        ADD COLUMN revoked_at timestamptz;
      CREATE TABLE activation_run_members (
        run_id bigint NOT NULL REFERENCES activation_runs(id) ON DELETE CASCADE,
        profile_id bigint NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        PRIMARY KEY(run_id,profile_id));
      CREATE INDEX ix_activation_run_members_profile ON activation_run_members(profile_id);
      CREATE TABLE activation_last_members (
        activation_id bigint NOT NULL REFERENCES activations(id) ON DELETE CASCADE,
        profile_id bigint NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        PRIMARY KEY(activation_id,profile_id));
      CREATE INDEX ix_activation_last_members_profile ON activation_last_members(profile_id);
    """)


def downgrade():
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("""
      DROP TABLE activation_last_members, activation_run_members;
      ALTER TABLE activation_runs DROP COLUMN profiles_excluded_missing_fields,
        DROP COLUMN snapshot_encrypted, DROP COLUMN revoked_at;
      ALTER TABLE activations DROP COLUMN name, DROP COLUMN timezone, DROP COLUMN column_order;
    """)
