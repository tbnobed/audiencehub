"""Local identity credentials, revocable sessions and operations policy."""
from alembic import op

revision = "0041_m8_operations"
down_revision = "0040_hubspot"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE UNIQUE INDEX users_email_local_unique ON users(lower(email));
      CREATE TABLE local_credentials (
        user_id bigint PRIMARY KEY REFERENCES users(id),
        password_hash text, failures integer NOT NULL DEFAULT 0,
        locked_until timestamptz, totp_secret bytea, pending_totp bytea,
        pending_totp_until timestamptz, totp_counter bigint NOT NULL DEFAULT -1,
        password_changed_at timestamptz
      );
      CREATE TABLE local_sessions (
        token_hash text PRIMARY KEY, user_id bigint NOT NULL REFERENCES users(id),
        last_seen timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(),
        mfa_verified boolean NOT NULL DEFAULT false
      );
      CREATE INDEX local_sessions_user ON local_sessions(user_id);
      CREATE INDEX local_sessions_idle ON local_sessions(last_seen);
      CREATE TABLE local_account_tokens (
        token_hash text PRIMARY KEY, user_id bigint NOT NULL REFERENCES users(id),
        purpose text NOT NULL, expires_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE local_login_limits (
        key_hash text PRIMARY KEY, failures integer NOT NULL DEFAULT 0,
        window_started timestamptz NOT NULL DEFAULT now(), locked_until timestamptz
      );
      CREATE TABLE operations_policy (
        id integer PRIMARY KEY CHECK(id=1),
        require_admin_totp boolean NOT NULL DEFAULT false,
        event_retention_days integer NOT NULL DEFAULT 730 CHECK(event_retention_days>=0),
        audit_retention_days integer NOT NULL DEFAULT 0 CHECK(audit_retention_days>=0),
        job_retention_days integer NOT NULL DEFAULT 90 CHECK(job_retention_days>=0),
        enrichment_retention_days integer NOT NULL DEFAULT 0 CHECK(enrichment_retention_days>=0),
        partitions_ahead_months integer NOT NULL DEFAULT 3 CHECK(partitions_ahead_months BETWEEN 1 AND 24)
      );
      INSERT INTO operations_policy(id) VALUES(1);
      CREATE TABLE operations_cleanup_files (
        path text PRIMARY KEY, queued_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE INDEX audit_log_action_at ON audit_log(action,at DESC,id DESC);
      CREATE INDEX audit_log_user_at ON audit_log(user_id,at DESC,id DESC);
      CREATE INDEX audit_log_entity_at ON audit_log(entity_type,entity_id,at DESC,id DESC);
    """)


def downgrade():
    op.execute("""
      DROP INDEX audit_log_entity_at; DROP INDEX audit_log_user_at; DROP INDEX audit_log_action_at;
      DROP TABLE operations_cleanup_files,operations_policy,local_login_limits,local_account_tokens,local_sessions,local_credentials;
      DROP INDEX users_email_local_unique;
    """)
