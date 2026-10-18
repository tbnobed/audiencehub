"""Do not recalculate contact hashes on ownership-only identity moves."""
from alembic import op

revision = "0031_identity_write_guard"
down_revision = "0030_master_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      DROP TRIGGER kin_source_identity ON source_records;
      CREATE TRIGGER kin_source_identity
        BEFORE INSERT OR UPDATE OF email_norm,attributes ON source_records
        FOR EACH ROW EXECUTE FUNCTION kin_source_identity()
    """)


def downgrade():
    op.execute("""
      DROP TRIGGER kin_source_identity ON source_records;
      CREATE TRIGGER kin_source_identity BEFORE INSERT OR UPDATE ON source_records
        FOR EACH ROW EXECUTE FUNCTION kin_source_identity()
    """)
