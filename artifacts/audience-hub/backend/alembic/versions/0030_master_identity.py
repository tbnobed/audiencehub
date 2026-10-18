"""Permanent master identity and reversible merge provenance.

Existing profiles are deliberately backfilled separately in committed batches.
"""
from alembic import op

revision = "0030_master_identity"
down_revision = "0029_dashboard_rolling_default"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE EXTENSION IF NOT EXISTS pgcrypto;
      CREATE FUNCTION kin_new_master_id(at_time timestamptz DEFAULT clock_timestamp())
      RETURNS text LANGUAGE plpgsql VOLATILE AS $$
      DECLARE n numeric; out text := ''; alphabet text := '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
              bytes bytea := gen_random_bytes(10); i integer;
      BEGIN
        n := floor(extract(epoch FROM at_time)*1000);
        FOR i IN 0..9 LOOP n := n*256 + get_byte(bytes,i); END LOOP;
        FOR i IN 1..26 LOOP
          out := substr(alphabet,mod(n,32)::integer+1,1)||out; n := floor(n/32);
        END LOOP;
        RETURN 'kin_'||out;
      END $$;
      CREATE TABLE master_id_registry(
        master_id text PRIMARY KEY CHECK(master_id ~ '^kin_[0-7][0-9A-HJKMNP-TV-Z]{25}$'),
        issued_at timestamptz NOT NULL DEFAULT clock_timestamp()
      );
      ALTER TABLE profiles ADD COLUMN master_id text UNIQUE;
      CREATE INDEX ix_profiles_master_backfill ON profiles(first_seen_at,id) WHERE master_id IS NULL;
      CREATE OR REPLACE VIEW active_profiles AS
        SELECT * FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted;
      CREATE FUNCTION kin_guard_master_id() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='UPDATE' AND OLD.master_id IS NOT NULL
           AND NEW.master_id IS DISTINCT FROM OLD.master_id THEN
          RAISE EXCEPTION 'master_id is immutable';
        END IF;
        IF NEW.master_id IS NULL THEN
          NEW.master_id := kin_new_master_id(NEW.first_seen_at);
        END IF;
        IF TG_OP='INSERT' OR OLD.master_id IS NULL THEN
          INSERT INTO master_id_registry(master_id) VALUES(NEW.master_id);
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER kin_master_id BEFORE INSERT OR UPDATE OF master_id ON profiles
        FOR EACH ROW EXECUTE FUNCTION kin_guard_master_id();
      CREATE TABLE master_id_aliases(
        retired_id text PRIMARY KEY REFERENCES master_id_registry(master_id),
        survivor_id text NOT NULL REFERENCES master_id_registry(master_id),
        merge_id bigint REFERENCES profile_merges(id) ON DELETE SET NULL,
        merged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CHECK(retired_id<>survivor_id)
      );
      CREATE INDEX ON master_id_aliases(survivor_id);
      CREATE TABLE identity_xref(
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        master_id text NOT NULL REFERENCES profiles(master_id) ON DELETE CASCADE,
        key_type text NOT NULL, key_value text NOT NULL,
        source_id bigint NOT NULL DEFAULT 0,
        first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        status text NOT NULL DEFAULT 'active',
        UNIQUE(master_id,key_type,key_value,source_id)
      );
      CREATE INDEX ON identity_xref(key_type,key_value);
      CREATE INDEX ON identity_xref(master_id);
      CREATE TABLE identity_merge_journal(
        merge_id bigint PRIMARY KEY REFERENCES profile_merges(id) ON DELETE CASCADE,
        winner_id bigint NOT NULL REFERENCES profiles(id),
        loser_id bigint NOT NULL REFERENCES profiles(id),
        sides jsonb NOT NULL, undone_at timestamptz,
        undone_by bigint REFERENCES users(id),
        new_profile_id bigint REFERENCES profiles(id)
      );
      CREATE TABLE identity_reviews(
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        source_record_id bigint REFERENCES source_records(id) ON DELETE CASCADE,
        reason text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(source_record_id,reason)
      );
      CREATE FUNCTION kin_source_identity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.email_norm IS NOT NULL THEN
          NEW.email_norm := lower(btrim(COALESCE(NEW.attributes->>'email_raw',NEW.email_norm::text)));
          NEW.attributes := NEW.attributes || jsonb_build_object(
            'email_md5',md5(NEW.email_norm::text),
            'email_sha256',encode(digest(NEW.email_norm::text,'sha256'),'hex'));
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER kin_source_identity BEFORE INSERT OR UPDATE ON source_records
        FOR EACH ROW EXECUTE FUNCTION kin_source_identity();
    """)


def downgrade():
    raise RuntimeError("Permanent master IDs cannot be downgraded or reused.")
