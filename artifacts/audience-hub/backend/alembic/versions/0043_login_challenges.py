"""Short-lived, single-use password-to-authenticator sign-in challenges."""
from alembic import op

revision = "0043_login_challenges"
down_revision = "0042_account_delivery"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE local_login_challenges (
        token_hash text PRIMARY KEY, user_id bigint NOT NULL REFERENCES users(id),
        credential_fingerprint text NOT NULL, expires_at timestamptz NOT NULL
      );
      CREATE INDEX local_login_challenge_user ON local_login_challenges(user_id);
      CREATE INDEX local_login_challenge_expiry ON local_login_challenges(expires_at);
    """)


def downgrade():
    op.execute("DROP TABLE local_login_challenges")
