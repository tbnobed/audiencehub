"""Approved privacy workflows and shared consent precedence."""
from alembic import op
import sqlalchemy as sa

revision = "0026_privacy_workflows"
down_revision = "0025_activation_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("deletion_requests", sa.Column("approved_by", sa.BigInteger(), sa.ForeignKey("users.id")))
    op.add_column("deletion_requests", sa.Column("approved_at", sa.DateTime(timezone=True)))
    op.add_column("deletion_requests", sa.Column("result_counts", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("deletion_requests", sa.Column("error", sa.Text()))
    op.add_column("deletion_requests", sa.Column("identifier_encrypted", sa.LargeBinary()))
    op.execute("""
    CREATE FUNCTION consent_candidate_wins(
      old_status text, old_time timestamptz, old_source bigint,
      new_status text, new_time timestamptz, new_source bigint)
    RETURNS boolean LANGUAGE sql STABLE AS $$
      WITH auth AS (
        SELECT EXISTS(SELECT 1 FROM sources WHERE id=old_source
          AND settings->>'authoritative_for_consent'='true') AS old_auth,
          EXISTS(SELECT 1 FROM sources WHERE id=new_source
          AND settings->>'authoritative_for_consent'='true') AS new_auth
      )
      SELECT CASE
        WHEN new_status='unknown' THEN old_status='unknown' AND new_time>old_time
        WHEN old_status='unknown' THEN true
        WHEN new_status='opted_out' AND old_status='opted_in' AND old_auth THEN new_time>=old_time
        WHEN new_status='opted_out' THEN old_status<>'opted_out' OR new_time>old_time
        WHEN old_status='opted_out' THEN new_auth AND new_time>old_time
        WHEN old_auth AND NOT new_auth THEN false
        ELSE new_time>old_time OR (new_time=old_time AND new_auth AND NOT old_auth)
      END FROM auth
    $$""")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP FUNCTION consent_candidate_wins(text,timestamptz,bigint,text,timestamptz,bigint)")
    for column in ("identifier_encrypted", "error", "result_counts", "approved_at", "approved_by"):
        op.drop_column("deletion_requests", column)
