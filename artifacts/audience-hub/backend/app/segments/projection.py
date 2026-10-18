"""Typed, source-priority-preserving segment properties on profile_traits.

This is imported evidence, not Kinship-computed RFM. Null fields never fall back
to lower-priority evidence after selection. No resolver or identity keys change.
"""
from sqlalchemy import text

PROPERTY_TYPES = {
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


def refresh_projection(db, ids):
    """One priority selection per batch, rather than per rule per profile."""
    assignments = ", ".join(
        f"attr_{key}=v.attr_{key}" for key in PROPERTY_TYPES
    )
    columns = ", ".join(
        (f"(props->>'{key}')::text" if typ == "text"
         else f"NULLIF(btrim(props->>'{key}'),'')::{typ}") + f" AS attr_{key}"
        for key, typ in PROPERTY_TYPES.items()
    )
    db.execute(text(f"""
        WITH winners AS (
          SELECT DISTINCT ON (sr.profile_id, kv.key)
                 sr.profile_id, kv.key, kv.value
          FROM source_records sr JOIN sources s ON s.id=sr.source_id
          CROSS JOIN LATERAL jsonb_each(sr.attributes) kv
          WHERE sr.profile_id=ANY(CAST(:ids AS bigint[]))
            AND kv.key=ANY(CAST(:keys AS text[])) AND kv.value<>'null'::jsonb
          ORDER BY sr.profile_id, kv.key, s.priority, sr.updated_at DESC, sr.id
        ), properties AS (
          SELECT profile_id, jsonb_object_agg(key,value) AS props
          FROM winners GROUP BY profile_id
        ), typed AS (
          SELECT target.profile_id, {columns}
          FROM unnest(CAST(:ids AS bigint[])) AS target(profile_id)
          LEFT JOIN properties USING(profile_id)
        )
        UPDATE profile_traits pt SET {assignments},
          last_activity_at=GREATEST(v.attr_last_seen,v.attr_last_contact)
        FROM typed v WHERE pt.profile_id=v.profile_id
    """), {"ids": ids, "keys": list(PROPERTY_TYPES)})


def refresh_projections(db, profile_ids=None, batch_size=5000):
    if profile_ids is not None:
        for offset in range(0, len(profile_ids), batch_size):
            refresh_projection(db, profile_ids[offset:offset + batch_size])
        return
    cursor = 0
    while True:
        ids = list(db.scalars(text("""
            SELECT pt.profile_id FROM profile_traits pt
            JOIN active_profiles p ON p.id=pt.profile_id
            WHERE pt.profile_id>:cursor ORDER BY pt.profile_id LIMIT :size
        """), {"cursor": cursor, "size": batch_size}))
        if not ids:
            break
        refresh_projection(db, ids)
        cursor = ids[-1]
