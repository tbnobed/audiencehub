"""Meta first-party capture, verified keys and owned-audience reporting."""
from alembic import op

revision = "0045_meta_capture"
down_revision = "0044_first_party_graph"


def upgrade():
    op.execute("""
      CREATE TABLE meta_connections(
        source_id bigint PRIMARY KEY REFERENCES sources(id),
        config jsonb NOT NULL, credentials bytea NOT NULL,
        connected_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE meta_leads(
        source_id bigint NOT NULL REFERENCES sources(id),lead_id text NOT NULL,
        form_id text,source_record_id bigint REFERENCES source_records(id) ON DELETE CASCADE,
        status text NOT NULL DEFAULT 'queued',error text,updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(source_id,lead_id)
      );
      CREATE TABLE meta_verified_keys(
        source_record_id bigint PRIMARY KEY REFERENCES source_records(id) ON DELETE CASCADE,
        meta_app_user_id text,meta_psid text,verified_at timestamptz NOT NULL DEFAULT now(),
        CHECK(meta_app_user_id IS NOT NULL OR meta_psid IS NOT NULL)
      );
      CREATE TABLE meta_pending_contacts(
        source_record_id bigint PRIMARY KEY REFERENCES source_records(id) ON DELETE CASCADE
      );
      CREATE INDEX meta_leads_status ON meta_leads(source_id,status);
      CREATE TABLE meta_attribution_sessions(
        source_id bigint NOT NULL REFERENCES sources(id),session_key text NOT NULL,
        profile_id bigint REFERENCES profiles(id) ON DELETE CASCADE,
        attribution jsonb NOT NULL DEFAULT '{}',first_seen_at timestamptz NOT NULL DEFAULT now(),
        last_seen_at timestamptz NOT NULL DEFAULT now(),attached_at timestamptz,
        PRIMARY KEY(source_id,session_key)
      );
      CREATE INDEX meta_attribution_profile ON meta_attribution_sessions(profile_id);
      CREATE INDEX meta_attribution_retention ON meta_attribution_sessions(last_seen_at);
      CREATE TABLE meta_audiences(
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        segment_id bigint NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        source_id bigint NOT NULL REFERENCES sources(id),
        meta_audience_id text NOT NULL UNIQUE,name text NOT NULL,
        destination_id bigint REFERENCES destinations(id) ON DELETE SET NULL,
        created_at timestamptz NOT NULL DEFAULT now(),last_refreshed_at timestamptz
      );
      CREATE TABLE meta_ad_results(
        audience_id bigint NOT NULL REFERENCES meta_audiences(id) ON DELETE CASCADE,
        adset_id text NOT NULL,date_start date NOT NULL,date_stop date NOT NULL,
        adset_name text,spend numeric NOT NULL DEFAULT 0,reach bigint NOT NULL DEFAULT 0,
        conversions numeric NOT NULL DEFAULT 0,currency text,conversion_action text,
        PRIMARY KEY(audience_id,adset_id,date_start,date_stop)
      );
    """)


def downgrade():
    op.execute("""DROP TABLE meta_ad_results,meta_audiences,meta_attribution_sessions,
      meta_pending_contacts,meta_verified_keys,meta_leads,meta_connections""")
