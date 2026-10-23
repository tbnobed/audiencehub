"""FusionAuth login identity alongside legacy Okta; no people or keys removed."""
from alembic import op

revision = "0046_fusionauth_identity"
down_revision = "0045_meta_capture"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE identity_devices
        ALTER COLUMN okta_customer_id DROP NOT NULL,
        ADD COLUMN auth_key_type text,
        ADD COLUMN auth_key_value text;
      UPDATE identity_devices SET auth_key_type='okta_customer_id',
        auth_key_value=okta_customer_id;
      ALTER TABLE identity_devices
        ALTER COLUMN auth_key_type SET NOT NULL,
        ALTER COLUMN auth_key_value SET NOT NULL,
        ADD CONSTRAINT identity_devices_login_kind CHECK
          (auth_key_type IN ('okta_customer_id','fusionauth_user_id'));
      CREATE INDEX identity_devices_login ON identity_devices(auth_key_type,auth_key_value);

      CREATE FUNCTION enforce_fusionauth_owner() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        -- Deferred so a reversible merge may move every source's evidence in
        -- one transaction. Multiple source rows for ONE person remain valid.
        PERFORM pg_advisory_xact_lock(hashtextextended(
          'kinship:fusionauth:' || NEW.key_value,0));
        IF (SELECT count(DISTINCT master_id) FROM identity_xref
            WHERE key_type='fusionauth_user_id' AND key_value=NEW.key_value)>1 THEN
          RAISE EXCEPTION 'FusionAuth identity already belongs to another person';
        END IF;
        RETURN NULL;
      END $$;
      CREATE CONSTRAINT TRIGGER fusionauth_one_person
        AFTER INSERT OR UPDATE ON identity_xref
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        WHEN (NEW.key_type='fusionauth_user_id')
        EXECUTE FUNCTION enforce_fusionauth_owner();
    """)


def downgrade():
    # Never silently discard device links collected during the cutover.
    op.execute("""
      DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM identity_devices WHERE okta_customer_id IS NULL) THEN
          RAISE EXCEPTION 'Cannot downgrade while FusionAuth-only device links exist';
        END IF;
      END $$;
      DROP TRIGGER fusionauth_one_person ON identity_xref;
      DROP FUNCTION enforce_fusionauth_owner();
      DROP INDEX identity_devices_login;
      ALTER TABLE identity_devices DROP CONSTRAINT identity_devices_login_kind,
        DROP COLUMN auth_key_type,DROP COLUMN auth_key_value,
        ALTER COLUMN okta_customer_id SET NOT NULL;
    """)
