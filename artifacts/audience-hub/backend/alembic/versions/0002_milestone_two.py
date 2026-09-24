"""Milestone-two data model and monthly event partitions.

Revision ID: 0002_milestone_two
Revises: 0001_foundation
"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_milestone_two"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def _month_start(value):
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _shift_month(value, delta):
    month = value.month - 1 + delta
    return value.replace(year=value.year + month // 12, month=month % 12 + 1)


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "imports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text()),
        sa.Column("file_sha256", sa.String(64)),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("mapping", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="uploaded"),
        sa.Column("rows_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_file_path", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "profiles",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("merged_into_id", sa.BigInteger(), sa.ForeignKey("profiles.id")),
        sa.Column("email", postgresql.CITEXT()),
        sa.Column("phone", sa.Text()),
        sa.Column("first_name", sa.Text()),
        sa.Column("last_name", sa.Text()),
        sa.Column("address1", sa.Text()),
        sa.Column("city", sa.Text()),
        sa.Column("region", sa.Text()),
        sa.Column("postal_code", sa.Text()),
        sa.Column("country", sa.Text()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_profiles_active_id", "profiles", ["id"],
        postgresql_where=sa.text("merged_into_id IS NULL AND NOT is_deleted"),
    )
    op.create_index(
        "ix_profiles_search_text_trgm", "profiles", ["search_text"],
        postgresql_using="gin", postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.execute(
        "CREATE VIEW active_profiles AS "
        "SELECT * FROM profiles WHERE merged_into_id IS NULL AND NOT is_deleted"
    )

    op.create_table(
        "source_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), sa.ForeignKey("profiles.id")),
        sa.Column("email_norm", postgresql.CITEXT()),
        sa.Column("phone_e164", sa.Text()),
        sa.Column("first_name", sa.Text()),
        sa.Column("last_name", sa.Text()),
        sa.Column("address1", sa.Text()),
        sa.Column("address2", sa.Text()),
        sa.Column("city", sa.Text()),
        sa.Column("region", sa.Text()),
        sa.Column("postal_code", sa.Text()),
        sa.Column("country", sa.Text()),
        sa.Column("attributes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("raw_hash", sa.Text(), nullable=False),
        sa.Column("last_import_id", sa.BigInteger(), sa.ForeignKey("imports.id")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_id", "external_id", name="uq_source_records_source_external"),
    )
    op.create_index("ix_source_records_profile_id", "source_records", ["profile_id"])
    op.create_index(
        "ix_source_records_pending", "source_records", ["id"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    op.create_table(
        "identifiers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), sa.ForeignKey("profiles.id"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("type", "value", name="uq_identifiers_type_value"),
    )
    op.create_index("ix_identifiers_profile_id", "identifiers", ["profile_id"])

    op.create_table(
        "identifier_blocklist",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("type", "value", name="uq_identifier_blocklist_type_value"),
    )
    op.bulk_insert(
        sa.table(
            "identifier_blocklist",
            sa.column("type", sa.Text()),
            sa.column("value", sa.Text()),
            sa.column("reason", sa.Text()),
        ),
        [
            {"type": "email", "value": value, "reason": "seeded_junk"}
            for value in (
                "test@test.com",
                "none@none.com",
                "noemail@*",
                "no@email.com",
                "na@na.com",
                "*@test.com",
            )
        ]
        + [
            {"type": "phone", "value": value, "reason": "seeded_junk"}
            for value in ("+10000000000", "+11111111111", "+15555555555", "+12345678900")
        ],
    )
    op.create_table(
        "profile_merges",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("winner_id", sa.BigInteger(), sa.ForeignKey("profiles.id"), nullable=False),
        sa.Column("loser_id", sa.BigInteger(), sa.ForeignKey("profiles.id"), nullable=False),
        sa.Column("reason", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id")),
    )

    op.create_table(
        "gifts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), sa.ForeignKey("profiles.id")),
        sa.Column("source_record_id", sa.BigInteger(), sa.ForeignKey("source_records.id")),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("gift_date", sa.Date()),
        sa.Column("fund", sa.Text()),
        sa.Column("campaign", sa.Text()),
        sa.Column("appeal_code", sa.Text()),
        sa.Column("channel", sa.Text()),
        sa.Column("payment_method", sa.Text()),
        sa.Column("is_recurring", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recurring_plan_id", sa.Text()),
        sa.Column("attributes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_id", "external_id", name="uq_gifts_source_external"),
    )
    op.create_index("ix_gifts_profile_gift_date", "gifts", ["profile_id", "gift_date"])
    op.create_index("ix_gifts_gift_date", "gifts", ["gift_date"])

    op.execute("""
        CREATE TABLE events (
            id bigint GENERATED ALWAYS AS IDENTITY,
            profile_id bigint REFERENCES profiles(id),
            anonymous_id text,
            user_id text,
            source_id bigint NOT NULL REFERENCES sources(id),
            type text NOT NULL,
            name text NOT NULL,
            properties jsonb NOT NULL DEFAULT '{}'::jsonb,
            context jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at timestamptz NOT NULL,
            received_at timestamptz NOT NULL DEFAULT now(),
            message_id text,
            PRIMARY KEY (id, occurred_at),
            CONSTRAINT uq_events_source_message_time UNIQUE (source_id, message_id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
    """)
    now_month = _month_start(datetime.now(timezone.utc))
    first_month = _shift_month(now_month, -12)
    end_month = _shift_month(now_month, 24)
    month = first_month
    while month < end_month:
        following = _shift_month(month, 1)
        partition_name = f"events_{month.year:04d}_{month.month:02d}"
        op.execute(
            f"CREATE TABLE {partition_name} PARTITION OF events "
            f"FOR VALUES FROM ('{month.isoformat()}') TO ('{following.isoformat()}')"
        )
        month = following
    op.execute("CREATE TABLE events_default PARTITION OF events DEFAULT")
    op.execute("CREATE INDEX ix_events_profile_occurred ON events (profile_id, occurred_at DESC)")
    op.create_index("ix_events_anonymous_id", "events", ["anonymous_id"])
    op.create_index("ix_events_name_occurred", "events", ["name", "occurred_at"])

    op.create_table(
        "consents",
        sa.Column("profile_id", sa.BigInteger(), sa.ForeignKey("profiles.id"), primary_key=True),
        sa.Column("channel", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_table(
        "suppressions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("type", "value_hash", name="uq_suppressions_type_hash"),
    )

    op.create_table(
        "enrichment_values",
        sa.Column("profile_id", sa.BigInteger(), sa.ForeignKey("profiles.id"), primary_key=True),
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), primary_key=True),
        sa.Column("attribute_key", sa.Text(), primary_key=True),
        sa.Column("value_text", sa.Text()),
        sa.Column("value_num", sa.Numeric()),
        sa.Column("value_bool", sa.Boolean()),
        sa.Column("value_date", sa.Date()),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("license_expires_at", sa.Date()),
    )
    op.create_index("ix_enrichment_values_source_key_text", "enrichment_values", ["source_id", "attribute_key", "value_text"])
    op.create_index("ix_enrichment_values_source_key_num", "enrichment_values", ["source_id", "attribute_key", "value_num"])
    op.create_table(
        "enrichment_attributes",
        sa.Column("source_id", sa.BigInteger(), sa.ForeignKey("sources.id"), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("data_type", sa.Text(), nullable=False),
        sa.Column("enum_values", postgresql.ARRAY(sa.Text())),
        sa.Column("description", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "profile_traits",
        sa.Column("profile_id", sa.BigInteger(), sa.ForeignKey("profiles.id"), primary_key=True),
        sa.Column("gift_count_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ltv_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("gift_amount_12m", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("gift_count_12m", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_gift_date", sa.Date()),
        sa.Column("last_gift_date", sa.Date()),
        sa.Column("largest_gift_amount", sa.Numeric(14, 2)),
        sa.Column("avg_gift_amount", sa.Numeric(14, 2)),
        sa.Column("is_recurring_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("days_since_last_gift", sa.Integer()),
        sa.Column("donor_status", sa.Text(), nullable=False, server_default="prospect"),
        sa.Column("rfm_recency", sa.SmallInteger()),
        sa.Column("rfm_frequency", sa.SmallInteger()),
        sa.Column("rfm_monetary", sa.SmallInteger()),
        sa.Column("rfm_score", sa.String(3)),
        sa.Column("event_count_30d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_event_at", sa.DateTime(timezone=True)),
        sa.Column("video_views_30d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_engagement_channel", sa.Text()),
        sa.Column("source_keys", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for column in ("donor_status", "last_gift_date", "ltv_total", "gift_amount_12m"):
        op.create_index(f"ix_profile_traits_{column}", "profile_traits", [column])

    op.create_table(
        "trait_snapshots",
        sa.Column("month", sa.Date(), primary_key=True),
        sa.Column("donor_status", sa.Text(), primary_key=True),
        sa.Column("profile_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ltv_sum", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("giving_12m_sum", sa.Numeric(14, 2), nullable=False, server_default="0"),
    )
    op.create_table(
        "segments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("definition", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("refresh_schedule", sa.Text()),
        sa.Column("last_materialized_at", sa.DateTime(timezone=True)),
        sa.Column("last_count", sa.Integer()),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("updated_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "segment_membership",
        sa.Column("segment_id", sa.BigInteger(), sa.ForeignKey("segments.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("profile_id", sa.BigInteger(), sa.ForeignKey("profiles.id"), primary_key=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_segment_membership_profile", "segment_membership", ["profile_id"])
    op.create_table(
        "segment_counts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("segment_id", sa.BigInteger(), sa.ForeignKey("segments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("counted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("removed", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "destinations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("secret_encrypted", sa.LargeBinary()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "activations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("segment_id", sa.BigInteger(), sa.ForeignKey("segments.id"), nullable=False),
        sa.Column("destination_id", sa.BigInteger(), sa.ForeignKey("destinations.id"), nullable=False),
        sa.Column("field_mapping", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("required_consent", sa.Text()),
        sa.Column("schedule", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "activation_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("activation_id", sa.BigInteger(), sa.ForeignKey("activations.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("profiles_selected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profiles_excluded_consent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profiles_excluded_suppressed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profiles_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_path", sa.Text()),
        sa.Column("file_sha256", sa.String(64)),
        sa.Column("error", sa.Text()),
        sa.Column("triggered_by", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "deletion_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("requested_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("identifier_type", sa.Text(), nullable=False),
        sa.Column("identifier_value_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("matched_profile_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False, server_default=sa.text("'{}'::bigint[]")),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.execute("DROP VIEW IF EXISTS active_profiles")
    for table in (
        "deletion_requests", "activation_runs", "activations", "destinations",
        "segment_counts", "segment_membership", "segments", "trait_snapshots",
        "profile_traits", "enrichment_attributes", "enrichment_values", "suppressions",
        "consents", "events", "gifts", "profile_merges", "identifier_blocklist",
        "identifiers", "source_records", "profiles", "imports",
    ):
        op.drop_table(table)