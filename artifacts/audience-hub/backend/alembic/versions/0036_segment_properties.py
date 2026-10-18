"""Typed canonical segment projection; restartable, committed batch backfill.

Run once as a dedicated migration before starting upgraded API/worker images.
No identity/resolver state is changed. Nullable ADD COLUMN is metadata-only.
"""
from alembic import op
from sqlalchemy import text

revision = "0036_segment_properties"
down_revision = "0035_xref_real_keys"
branch_labels = None
depends_on = None

# Frozen schema for this revision; do not use the mutable runtime field catalog.
TYPES = {
    "tbn_plus_subscription": "text", "tbn_plus_subscription_update": "text",
    "donor_account_status": "text", "donor_account_number": "text",
    "rfm_date": "date", "rfm_recency_score": "numeric",
    "rfm_raw_frequency": "numeric", "has_recurring_donation": "boolean",
    "has_active_email": "boolean", "mobile_mktg_optin_tbn": "boolean",
    "tbn_call_center_sms_optin": "boolean", "mobile_mktg_optout_date_tbn": "timestamptz",
    "tbn_video_on_demand_updates_opt_status": "boolean",
    "donation_likelihood_prob": "numeric", "wealth_score": "numeric",
    "amt_suggested": "numeric", "lifetime_amount": "numeric",
    "last_seen": "timestamptz", "last_contact": "timestamptz",
}


def upgrade():
    # Each statement commits independently. An interrupted migration can restart:
    # nullable DDL/index existence is guarded and batch upserts are idempotent.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(text("SET lock_timeout='5s'"))
        try:
            for key, typ in TYPES.items():
                bind.execute(text(
                    f"ALTER TABLE profile_traits ADD COLUMN IF NOT EXISTS attr_{key} {typ}"
                ))
            bind.execute(text(
                "ALTER TABLE profile_traits ADD COLUMN IF NOT EXISTS last_activity_at timestamptz"
            ))
            cursor = 0
            cols = ", ".join(
                (f"(props->>'{k}')::text" if t == "text"
                 else f"NULLIF(btrim(props->>'{k}'),'')::{t}") + f" AS attr_{k}"
                for k, t in TYPES.items()
            )
            names = ", ".join("attr_" + k for k in TYPES)
            assignments = ", ".join(f"attr_{k}=EXCLUDED.attr_{k}" for k in TYPES)
            while True:
                ids = list(bind.execute(text("""
                    SELECT id FROM active_profiles WHERE id>:cursor ORDER BY id LIMIT 5000
                """), {"cursor": cursor}).scalars())
                if not ids:
                    break
                bind.execute(text(f"""
                    WITH winners AS (
                      SELECT DISTINCT ON (sr.profile_id, kv.key)
                             sr.profile_id, kv.key, kv.value
                      FROM source_records sr JOIN sources s ON s.id=sr.source_id
                      CROSS JOIN LATERAL jsonb_each(sr.attributes) kv
                      WHERE sr.profile_id=ANY(CAST(:ids AS bigint[]))
                        AND kv.key=ANY(CAST(:keys AS text[]))
                        AND kv.value<>'null'::jsonb
                      ORDER BY sr.profile_id, kv.key, s.priority, sr.updated_at DESC, sr.id
                    ), properties AS (
                      SELECT profile_id,jsonb_object_agg(key,value) AS props
                      FROM winners GROUP BY profile_id
                    ), typed AS (
                      SELECT target.profile_id,{cols}
                      FROM unnest(CAST(:ids AS bigint[])) target(profile_id)
                      LEFT JOIN properties USING(profile_id)
                    )
                    INSERT INTO profile_traits(profile_id,{names},last_activity_at)
                    SELECT profile_id,{names},GREATEST(attr_last_seen,attr_last_contact)
                    FROM typed
                    ON CONFLICT(profile_id) DO UPDATE SET {assignments},
                      last_activity_at=EXCLUDED.last_activity_at
                """), {"ids": ids, "keys": list(TYPES)})
                cursor = ids[-1]
                print(f"Segment properties backfilled through profile {cursor}", flush=True)
            for key in [*("attr_" + k for k in TYPES), "last_activity_at"]:
                name = "ix_pt_seg_" + key.removeprefix("attr_")
                # A cancelled concurrent build leaves an invalid index; repair
                # that state before retrying CREATE INDEX IF NOT EXISTS.
                invalid = bind.scalar(text("""
                    SELECT NOT i.indisvalid FROM pg_index i
                    JOIN pg_class c ON c.oid=i.indexrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE c.relname=:name AND n.nspname=current_schema()
                """), {"name": name})
                if invalid:
                    bind.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
                ordering = f"{key} text_pattern_ops, profile_id" if TYPES.get(
                    key.removeprefix("attr_")) == "text" else f"{key}, profile_id"
                bind.execute(text(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                    f"ON profile_traits ({ordering})"
                ))
            bind.execute(text("ANALYZE profile_traits"))
        finally:
            bind.execute(text("RESET lock_timeout"))


def downgrade():
    with op.get_context().autocommit_block():
        for key in [*("attr_" + k for k in TYPES), "last_activity_at"]:
            op.execute(f"ALTER TABLE profile_traits DROP COLUMN IF EXISTS {key}")
