"""Portable account email relay and single-use authenticator recovery codes."""
from alembic import op

revision = "0042_account_delivery"
down_revision = "0041_m8_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE account_email_settings (
        id integer PRIMARY KEY CHECK(id=1), enabled boolean NOT NULL DEFAULT false,
        from_email text NOT NULL DEFAULT '', from_name text NOT NULL DEFAULT 'Kinship',
        public_url text NOT NULL DEFAULT '', api_key bytea
      );
      INSERT INTO account_email_settings(id) VALUES(1);
      CREATE TABLE account_email_outbox (
        id bigserial PRIMARY KEY, user_id bigint REFERENCES users(id),
        purpose text NOT NULL, payload bytea, token_hash text,
        expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        accepted_at timestamptz, cancelled_at timestamptz, last_error text,
        job_id bigint REFERENCES jobs(id) ON DELETE SET NULL
      );
      CREATE INDEX account_email_expiry ON account_email_outbox(expires_at);
      CREATE TABLE local_recovery_codes (
        user_id bigint NOT NULL REFERENCES users(id), code_hash text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(user_id,code_hash)
      );
    """)


def downgrade():
    op.execute("DROP TABLE local_recovery_codes,account_email_outbox,account_email_settings")
