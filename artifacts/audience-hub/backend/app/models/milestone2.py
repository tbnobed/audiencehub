from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


def identity_pk():
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


class Import(Base):
    __tablename__ = "imports"
    id: Mapped[int] = identity_pk()
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text)
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    record_type: Mapped[str] = mapped_column(Text, nullable=False)
    mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="uploaded")
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_ok: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    warning_counts: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    rows_normalized: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_deduplicated: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_file_path: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = identity_pk()
    merged_into_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("profiles.id"))
    email: Mapped[str | None] = mapped_column(CITEXT)
    phone: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    address1: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    search_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


Index("ix_profiles_active_id", Profile.id, postgresql_where=text("merged_into_id IS NULL AND NOT is_deleted"))
Index("ix_profiles_search_text_trgm", Profile.search_text, postgresql_using="gin",
      postgresql_ops={"search_text": "gin_trgm_ops"})


class SourceRecord(Base):
    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_source_records_source_external"),
        Index("ix_source_records_pending", "id", postgresql_where=text("resolved_at IS NULL")),
        Index("ix_source_records_profile_id", "profile_id"),
        Index("ix_source_records_email_source_external", "email_norm", "source_id", "external_id"),
        Index("ix_source_records_phone_source_external", "phone_e164", "source_id", "external_id"),
    )
    id: Mapped[int] = identity_pk()
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    profile_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("profiles.id"))
    email_norm: Mapped[str | None] = mapped_column(CITEXT)
    phone_e164: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    address1: Mapped[str | None] = mapped_column(Text)
    address2: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    raw_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_import_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("imports.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class Identifier(Base):
    __tablename__ = "identifiers"
    __table_args__ = (UniqueConstraint("type", "value", name="uq_identifiers_type_value"), Index("ix_identifiers_profile_id", "profile_id"))
    id: Mapped[int] = identity_pk()
    type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    profile_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("profiles.id"), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class IdentifierBlocklist(Base):
    __tablename__ = "identifier_blocklist"
    __table_args__ = (UniqueConstraint("type", "value", name="uq_identifier_blocklist_type_value"),)
    id: Mapped[int] = identity_pk()
    type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ProfileMerge(Base):
    __tablename__ = "profile_merges"
    id: Mapped[int] = identity_pk()
    winner_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("profiles.id"), nullable=False)
    loser_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("profiles.id"), nullable=False)
    reason: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    merged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    job_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("jobs.id"))


class Gift(Base):
    __tablename__ = "gifts"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_gifts_source_external"),
        Index("ix_gifts_profile_gift_date", "profile_id", "gift_date"),
        Index("ix_gifts_gift_date", "gift_date"),
    )
    id: Mapped[int] = identity_pk()
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    profile_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("profiles.id"))
    source_record_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("source_records.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    gift_date: Mapped[datetime | None] = mapped_column(Date)
    fund: Mapped[str | None] = mapped_column(Text)
    campaign: Mapped[str | None] = mapped_column(Text)
    appeal_code: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(Text)
    payment_method: Mapped[str | None] = mapped_column(Text)
    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    recurring_plan_id: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("source_id", "message_id", "occurred_at", name="uq_events_source_message_time"),
        Index("ix_events_anonymous_id", "anonymous_id"),
        Index("ix_events_name_occurred", "name", "occurred_at"),
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("profiles.id"))
    anonymous_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    properties: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    message_id: Mapped[str | None] = mapped_column(Text)


Index("ix_events_profile_occurred", Event.profile_id, Event.occurred_at.desc())


class Consent(Base):
    __tablename__ = "consents"
    profile_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("profiles.id"), primary_key=True)
    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class Suppression(Base):
    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("type", "value_hash", name="uq_suppressions_type_hash"),)
    id: Mapped[int] = identity_pk()
    type: Mapped[str] = mapped_column(Text, nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EnrichmentValue(Base):
    __tablename__ = "enrichment_values"
    profile_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("profiles.id"), primary_key=True)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), primary_key=True)
    attribute_key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_num: Mapped[Decimal | None] = mapped_column(Numeric)
    value_bool: Mapped[bool | None] = mapped_column(Boolean)
    value_date: Mapped[datetime | None] = mapped_column(Date)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    license_expires_at: Mapped[datetime | None] = mapped_column(Date)


Index("ix_enrichment_values_source_key_text", EnrichmentValue.source_id, EnrichmentValue.attribute_key, EnrichmentValue.value_text)
Index("ix_enrichment_values_source_key_num", EnrichmentValue.source_id, EnrichmentValue.attribute_key, EnrichmentValue.value_num)


class EnrichmentAttribute(Base):
    __tablename__ = "enrichment_attributes"
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    data_type: Mapped[str] = mapped_column(Text, nullable=False)
    enum_values: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ProfileTrait(Base):
    __tablename__ = "profile_traits"
    profile_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("profiles.id"), primary_key=True)
    gift_count_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ltv_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")
    gift_amount_12m: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")
    gift_count_12m: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    first_gift_date: Mapped[datetime | None] = mapped_column(Date)
    last_gift_date: Mapped[datetime | None] = mapped_column(Date)
    largest_gift_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    avg_gift_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    is_recurring_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    days_since_last_gift: Mapped[int | None] = mapped_column(Integer)
    donor_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="prospect")
    rfm_recency: Mapped[int | None] = mapped_column(SmallInteger)
    rfm_frequency: Mapped[int | None] = mapped_column(SmallInteger)
    rfm_monetary: Mapped[int | None] = mapped_column(SmallInteger)
    rfm_score: Mapped[str | None] = mapped_column(String(3))
    event_count_30d: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    video_views_30d: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_engagement_channel: Mapped[str | None] = mapped_column(Text)
    source_keys: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


Index("ix_profile_traits_donor_status", ProfileTrait.donor_status)
Index("ix_profile_traits_last_gift_date", ProfileTrait.last_gift_date)
Index("ix_profile_traits_ltv_total", ProfileTrait.ltv_total)
Index("ix_profile_traits_gift_amount_12m", ProfileTrait.gift_amount_12m)


class TraitSnapshot(Base):
    __tablename__ = "trait_snapshots"
    month: Mapped[datetime] = mapped_column(Date, primary_key=True)
    donor_status: Mapped[str] = mapped_column(Text, primary_key=True)
    profile_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ltv_sum: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")
    giving_12m_sum: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")


class Segment(Base):
    __tablename__ = "segments"
    id: Mapped[int] = identity_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    refresh_schedule: Mapped[str | None] = mapped_column(Text)
    last_materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_count: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    updated_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class SegmentMembership(Base):
    __tablename__ = "segment_membership"
    __table_args__ = (Index("ix_segment_membership_profile", "profile_id"),)
    segment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("segments.id", ondelete="CASCADE"), primary_key=True)
    profile_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("profiles.id"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SegmentCount(Base):
    __tablename__ = "segment_counts"
    id: Mapped[int] = identity_pk()
    segment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("segments.id", ondelete="CASCADE"), nullable=False)
    counted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    count: Mapped[int] = mapped_column("count", Integer, nullable=False)
    added: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    removed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class Destination(Base):
    __tablename__ = "destinations"
    id: Mapped[int] = identity_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class Activation(Base):
    __tablename__ = "activations"
    id: Mapped[int] = identity_pk()
    segment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("segments.id"), nullable=False)
    destination_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("destinations.id"), nullable=False)
    field_mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    required_consent: Mapped[str | None] = mapped_column(Text)
    schedule: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ActivationRun(Base):
    __tablename__ = "activation_runs"
    id: Mapped[int] = identity_pk()
    activation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("activations.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profiles_selected: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    profiles_excluded_consent: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    profiles_excluded_suppressed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    profiles_sent: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    file_path: Mapped[str | None] = mapped_column(Text)
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual")
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DeletionRequest(Base):
    __tablename__ = "deletion_requests"
    id: Mapped[int] = identity_pk()
    requested_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    identifier_type: Mapped[str] = mapped_column(Text, nullable=False)
    identifier_value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    matched_profile_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False, server_default=text("'{}'::bigint[]"))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())