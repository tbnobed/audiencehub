"""Foundation schema.

Revision ID: 0001_foundation
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def pk():
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True)


def timestamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade():
    op.create_table("users", pk(), sa.Column("subject", sa.Text(), nullable=False, unique=True),
        sa.Column("email", sa.Text(), nullable=False), sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False), sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()), *timestamps())
    op.create_table("audit_log", pk(), sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("actor_type", sa.Text(), nullable=False), sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text()), sa.Column("entity_id", sa.Text()),
        sa.Column("details", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")), sa.Column("ip", sa.Text()))
    op.create_table("jobs", pk(), sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)), sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)), sa.Column("error", sa.Text()),
        sa.Column("progress", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("dedupe_key", sa.Text()), *timestamps())
    op.create_index("ix_jobs_claim", "jobs", ["status", "run_after", "priority", "id"])
    op.create_index("uq_jobs_active_dedupe", "jobs", ["dedupe_key"], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.create_table("scheduled_runs", sa.Column("task", sa.Text(), primary_key=True),
        sa.Column("window_key", sa.Text(), primary_key=True),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("sources", pk(), sa.Column("key", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False), sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("record_types", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("vendor", sa.Text()), sa.Column("license_expires_at", sa.Date()),
        sa.Column("write_key_hash", sa.Text()),
        sa.Column("settings", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()), *timestamps())


def downgrade():
    for table in ("sources", "scheduled_runs", "jobs", "audit_log", "users"):
        op.drop_table(table)