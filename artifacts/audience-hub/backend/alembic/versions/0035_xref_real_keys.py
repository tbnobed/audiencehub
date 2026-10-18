"""Remove source-record bookkeeping keys from the public identity crosswalk.

Source record IDs and internal resolver identifiers remain untouched.
"""
from alembic import op

revision = "0035_xref_real_keys"
down_revision = "0034_activation_sftp"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DELETE FROM identity_xref WHERE key_type='external'")
    op.create_check_constraint(
        "ck_identity_xref_no_external", "identity_xref", "key_type <> 'external'"
    )


def downgrade():
    op.drop_constraint("ck_identity_xref_no_external", "identity_xref", type_="check")
    # Do not recreate meaningless crosswalk rows; real keys are unchanged.
