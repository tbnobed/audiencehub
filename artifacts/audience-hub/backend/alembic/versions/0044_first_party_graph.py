"""First-party evidence, address/household, device and match-review graph."""
from alembic import op

revision = "0044_first_party_graph"
down_revision = "0043_login_challenges"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE identity_xref
        ADD COLUMN method text NOT NULL DEFAULT 'deterministic'
          CHECK(method IN ('deterministic','probabilistic')),
        ADD COLUMN confidence double precision NOT NULL DEFAULT 1 CHECK(confidence BETWEEN 0 AND 1),
        ADD COLUMN rule text NOT NULL DEFAULT 'existing_deterministic_resolver';
      CREATE TABLE identity_graph_policy(
        id integer PRIMARY KEY CHECK(id=1), enabled boolean NOT NULL DEFAULT false,
        review_threshold double precision NOT NULL DEFAULT .85,
        auto_threshold double precision NOT NULL DEFAULT .995,
        device_expiry_days integer NOT NULL DEFAULT 90 CHECK(device_expiry_days BETWEEN 1 AND 3650),
        household_surname boolean NOT NULL DEFAULT true,
        schedule_hour integer NOT NULL DEFAULT 3 CHECK(schedule_hour BETWEEN 0 AND 23),
        CHECK(review_threshold>0 AND auto_threshold<1 AND review_threshold<auto_threshold)
      );
      INSERT INTO identity_graph_policy(id) VALUES(1);
      ALTER TABLE profiles ADD COLUMN household_id text,
        ADD COLUMN household_size integer NOT NULL DEFAULT 1;
      CREATE INDEX profiles_household_idx ON profiles(household_id);
      CREATE TABLE identity_addresses(
        profile_id bigint PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
        input_hash text NOT NULL, number text, street text, unit text, city text, state text,
        zip5 text, zip4 text, normalized_key text, parser text NOT NULL,
        vendor text, vendor_result jsonb, standardized_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE INDEX identity_addresses_key ON identity_addresses(normalized_key)
        WHERE normalized_key IS NOT NULL;
      CREATE TABLE identity_graph_runs(
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
        dry_run boolean NOT NULL, status text NOT NULL DEFAULT 'queued',
        started_at timestamptz, finished_at timestamptz, counts jsonb NOT NULL DEFAULT '{}',
        error text
      );
      CREATE TABLE identity_pairs(
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        left_id bigint NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        right_id bigint NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        confidence double precision NOT NULL CHECK(confidence BETWEEN 0 AND 1),
        rule text NOT NULL, status text NOT NULL CHECK(status IN ('review','linked','rejected','conflict')),
        run_id bigint REFERENCES identity_graph_runs(id) ON DELETE SET NULL,
        merge_id bigint REFERENCES profile_merges(id) ON DELETE SET NULL,
        left_record_id bigint REFERENCES source_records(id) ON DELETE CASCADE,
        right_record_id bigint REFERENCES source_records(id) ON DELETE CASCADE,
        sampled boolean NOT NULL DEFAULT false, verdict text CHECK(verdict IN ('match','not_match','unsure')),
        note text, reviewed_by bigint REFERENCES users(id), reviewed_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
        CHECK(left_id<=right_id), UNIQUE(left_id,right_id)
      );
      CREATE INDEX identity_pairs_queue ON identity_pairs(status,verdict);
      CREATE INDEX identity_pairs_right ON identity_pairs(right_id);
      CREATE TABLE identity_graph_daily(
        day date NOT NULL, source_id bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        candidates bigint NOT NULL DEFAULT 0, linked bigint NOT NULL DEFAULT 0,
        review bigint NOT NULL DEFAULT 0, reviewed bigint NOT NULL DEFAULT 0,
        correct bigint NOT NULL DEFAULT 0, queue_open bigint NOT NULL DEFAULT 0,
        PRIMARY KEY(day,source_id)
      );
      CREATE TABLE identity_devices(
        key_type text NOT NULL CHECK(key_type IN
          ('roku_rida','samsung_tifa','apple_idfa','google_aaid','app_install_id','web_cookie_id')),
        device_id text NOT NULL,
        profile_id bigint NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        source_id bigint NOT NULL REFERENCES sources(id),
        okta_customer_id text NOT NULL,
        first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        PRIMARY KEY(key_type,device_id,profile_id)
      );
      CREATE INDEX identity_devices_expiry ON identity_devices(expires_at);
      CREATE INDEX identity_devices_profile ON identity_devices(profile_id);
      CREATE TABLE identity_device_credentials(
        source_id bigint PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
        secret_encrypted bytea NOT NULL
      );
      CREATE TABLE identity_device_receipts(
        source_id bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        event_hash text NOT NULL, received_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(source_id,event_hash)
      );
      ALTER TABLE activations ADD COLUMN one_per_household boolean NOT NULL DEFAULT false;
      ALTER TABLE activation_runs ADD COLUMN profiles_excluded_household bigint NOT NULL DEFAULT 0;
    """)
    # Also support migration runners that invoke upgrade() directly (the
    # disposable-schema suite), not only Alembic's on_version_apply guard.
    from app.migration_views import refresh_profile_views
    refresh_profile_views(op.get_bind())


def downgrade():
    from app.migration_views import refresh_profile_views
    # Remove full-row views before removing their new profile columns, then
    # restore definitions, filters, grants and materialized-view indexes.
    refresh_profile_views(op.get_bind(), before_recreate=lambda: op.execute(
        "ALTER TABLE profiles DROP COLUMN household_size,DROP COLUMN household_id"))
    op.execute("""
      ALTER TABLE activation_runs DROP COLUMN profiles_excluded_household;
      ALTER TABLE activations DROP COLUMN one_per_household;
      DROP TABLE identity_devices,identity_device_credentials,identity_device_receipts,
        identity_graph_daily,identity_pairs,identity_graph_runs,identity_addresses;
      DROP TABLE identity_graph_policy;
      ALTER TABLE identity_xref DROP COLUMN method,DROP COLUMN confidence,DROP COLUMN rule;
    """)
