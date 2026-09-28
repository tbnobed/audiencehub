"""Set-based identity resolution for a quiescent import queue.

The bounded resolver remains the cooperative path during active imports. This
path performs one atomic refresh of pending records, with SQL component state
and existing profile nodes; it never materializes records in Python.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.identity.locking import (
    acquire_resolver_lock, IDENTIFIER_LOCK_BUCKETS, IDENTIFIER_LOCK_NAMESPACE,
)

# Python str.strip's whitespace set, including tabs/newlines and Unicode spaces.
# Imported values are already stripped, but older/API-written records need the
# same semantics as normalize_external_id and the legacy identifier helpers.
_WHITESPACE = "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680" + "".join(
    chr(code) for code in range(0x2000, 0x200B)
) + "\u2028\u2029\u202f\u205f\u3000"


def _sql(db, statement, **params):
    return db.execute(text(statement), params)


def resolve_bulk(db: Session, job_id: int | None = None) -> dict[str, int]:
    """Resolve all pending records; caller commits, including an empty result.

    Incremental closure consists of pending records, their unblocked keys and
    every existing profile owning one of those keys. Profile nodes connect all
    pending bridges before a winner is chosen. Unrelated resolved records are
    not rebuilt. All temporary state disappears on transaction completion.
    """
    acquire_resolver_lock(db)
    # Use the existing partial (id WHERE resolved_at IS NULL) index, especially
    # on the worker's final empty call. Do not copy/join/count the whole source
    # table merely to discover that the graph is already drained.
    if _sql(db, """
        SELECT id FROM source_records WHERE resolved_at IS NULL ORDER BY id LIMIT 1
    """).first() is None:
        return {"records": 0, "profiles_created": 0, "merges": 0}
    # A bounded per-operation budget avoids default 4 MB aggregate/hash spills;
    # SET LOCAL disappears at the same commit as all temporary graph state.
    _sql(db, "SET LOCAL work_mem='64MB'")
    for name in ("pending", "edges", "labels", "key_labels", "owners", "winners",
                 "new", "moves", "assign", "affected", "consents", "enrich", "survivors"):
        _sql(db, f"DROP TABLE IF EXISTS pg_temp.ir_{name}")
    _sql(db, """
        CREATE TEMP TABLE ir_pending ON COMMIT DROP AS
        SELECT sr.*, s.key AS source_key
        FROM source_records sr JOIN sources s ON s.id=sr.source_id
        WHERE sr.resolved_at IS NULL
    """)
    count = _sql(db, "SELECT count(*) FROM ir_pending").scalar_one()
    if not count:
        return {"records": 0, "profiles_created": 0, "merges": 0}
    _sql(db, "CREATE UNIQUE INDEX ON ir_pending(id)")
    _sql(db, """
        CREATE TEMP TABLE ir_edges (
          record_id bigint NOT NULL, kind text NOT NULL, value text NOT NULL,
          PRIMARY KEY(record_id,kind,value)
        ) ON COMMIT DROP
    """)
    _sql(db, """
        INSERT INTO ir_edges
        SELECT DISTINCT p.id, k.kind, k.value
        FROM ir_pending p CROSS JOIN LATERAL (
          VALUES
            ('external', p.source_key || ':' || NULLIF(btrim(p.external_id,:whitespace),'')),
            ('email', NULLIF(lower(btrim(p.email_norm::text,:whitespace)),'')),
            ('phone', NULLIF(p.phone_e164,'')),
            ('external', CASE
               WHEN jsonb_typeof(p.attributes->'_identity_reference'->'source_key')='string'
               THEN NULLIF(btrim(p.attributes->'_identity_reference'->>'source_key',:whitespace),'')
                 || ':' || NULLIF(btrim(p.attributes->'_identity_reference'->>'contact_external_id',:whitespace),'')
               END),
            ('user_id', CASE WHEN length(btrim(p.attributes->>'user_id',:whitespace)) BETWEEN 1 AND 200
                 THEN p.source_key || ':' || btrim(p.attributes->>'user_id',:whitespace) END),
            ('anonymous_id', CASE WHEN
                 p.attributes->'identify' IS NOT NULL
                 AND p.attributes->'identify' NOT IN ('null'::jsonb,'false'::jsonb,'0'::jsonb,'""'::jsonb,'[]'::jsonb,'{}'::jsonb)
                 AND length(btrim(p.attributes->>'anonymous_id',:whitespace)) BETWEEN 1 AND 200
                 THEN btrim(p.attributes->>'anonymous_id',:whitespace) END)
        ) AS k(kind,value) WHERE k.value IS NOT NULL
    """, whitespace=_WHITESPACE)
    _sql(db, "CREATE INDEX ON ir_edges(kind,value)")
    _sql(db, "ANALYZE ir_edges")
    # Cardinality is per source, distinct external IDs, and ignores transactional
    # gift/event source records. This runs once, not for every 500-row group.
    for kind, column in (("email", "email_norm"), ("phone", "phone_e164")):
        cast = "candidate.value::citext" if kind == "email" else "candidate.value"
        _sql(db, f"""
            INSERT INTO identifier_blocklist(type,value,reason)
            SELECT :kind, value, 'high_cardinality' FROM (
              SELECT candidate.value, sr.source_id
              FROM (SELECT DISTINCT value FROM ir_edges WHERE kind=:kind) candidate
              JOIN source_records sr ON sr.{column}={cast}
              LEFT JOIN imports i ON i.id=sr.last_import_id
              WHERE i.record_type IS DISTINCT FROM 'gift'
                AND i.record_type IS DISTINCT FROM 'event'
              GROUP BY candidate.value,sr.source_id
              HAVING count(DISTINCT sr.external_id)>25
            ) high GROUP BY value ON CONFLICT(type,value) DO NOTHING
        """, kind=kind)
    _sql(db, """
        DELETE FROM ir_edges e WHERE
          EXISTS(SELECT 1 FROM identifier_blocklist b WHERE b.type=e.kind AND b.value=e.value)
          OR (kind='email' AND (value IN ('test@test.com','none@none.com','no@email.com','na@na.com')
              OR value LIKE 'noemail@%' OR value LIKE '%@test.com'))
          OR (kind='phone' AND regexp_replace(
             regexp_replace(value,'[^0-9]','','g'),'^1([0-9]{10})$','\\1')
             ~ '^([0-9])\\1{9}$')
    """)
    _sql(db, """
        WITH buckets AS MATERIALIZED (
          SELECT DISTINCT ((hashtext(kind || ':' || value) % :buckets)+:buckets)%:buckets AS bucket
          FROM ir_edges
        ), ordered AS MATERIALIZED (SELECT bucket FROM buckets ORDER BY bucket)
        SELECT pg_advisory_xact_lock(CAST(:namespace AS integer),bucket) FROM ordered
    """, buckets=IDENTIFIER_LOCK_BUCKETS, namespace=IDENTIFIER_LOCK_NAMESPACE)
    # Owners are graph nodes, not just candidate winners: two different keys
    # owned by the same profile must connect pending components transitively.
    _sql(db, """
        INSERT INTO ir_edges
        SELECT e.record_id, 'profile', i.profile_id::text
        FROM ir_edges e JOIN identifiers i ON i.type=e.kind AND i.value=e.value
        UNION SELECT id,'profile',profile_id::text FROM ir_pending WHERE profile_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """)
    _sql(db, "ANALYZE ir_edges")
    _sql(db, """
        CREATE TEMP TABLE ir_labels ON COMMIT DROP AS SELECT id, id AS label FROM ir_pending;
        CREATE UNIQUE INDEX ON ir_labels(id);
        ANALYZE ir_labels;
        CREATE TEMP TABLE ir_key_labels(
          kind text, value text, label bigint, PRIMARY KEY(kind,value)
        ) ON COMMIT DROP
    """)
    # The measured medium graph's text-key aggregation is expensive and its
    # millions of groups can spill at 64 MB. Bound the larger budget to this serial
    # phase only; all later materialization/survivorship returns to 64 MB.
    # No labels, graph edges, or convergence rules change.
    _sql(db, "SET LOCAL work_mem='256MB'")
    while True:
        _sql(db, "TRUNCATE ir_key_labels")
        _sql(db, """
            INSERT INTO ir_key_labels
            SELECT e.kind,e.value,min(l.label) FROM ir_edges e
            JOIN ir_labels l ON l.id=e.record_id GROUP BY e.kind,e.value
        """)
        _sql(db, "ANALYZE ir_key_labels")
        changed = _sql(db, """
            UPDATE ir_labels l SET label=n.label FROM (
              SELECT e.record_id,min(k.label) AS label FROM ir_edges e
              JOIN ir_key_labels k USING(kind,value) GROUP BY e.record_id
            ) n WHERE l.id=n.record_id AND l.label>n.label
        """).rowcount
        if not changed:
            break
    _sql(db, "SET LOCAL work_mem='64MB'")
    _sql(db, """
        CREATE INDEX ON ir_labels(label);
        CREATE TEMP TABLE ir_owners ON COMMIT DROP AS
        SELECT DISTINCT l.label,e.value::bigint AS profile_id FROM ir_edges e
        JOIN ir_labels l ON l.id=e.record_id WHERE e.kind='profile';
        CREATE INDEX ON ir_owners(label);
        ANALYZE ir_owners;
        CREATE TEMP TABLE ir_winners ON COMMIT DROP AS
        SELECT DISTINCT ON(o.label) o.label,p.id AS profile_id FROM ir_owners o
        JOIN profiles p ON p.id=o.profile_id ORDER BY o.label,p.first_seen_at,p.id;
        CREATE UNIQUE INDEX ON ir_winners(label);
        CREATE TEMP TABLE ir_new ON COMMIT DROP AS
        SELECT label,nextval(pg_get_serial_sequence('profiles','id')) AS profile_id
        FROM (SELECT DISTINCT label FROM ir_labels EXCEPT SELECT label FROM ir_winners) missing;
        INSERT INTO profiles(id,first_seen_at,last_seen_at) OVERRIDING SYSTEM VALUE
        SELECT profile_id,now(),now() FROM ir_new;
        INSERT INTO ir_winners SELECT * FROM ir_new;
        ANALYZE ir_winners;
        CREATE TEMP TABLE ir_moves ON COMMIT DROP AS
        SELECT DISTINCT o.profile_id AS loser,w.profile_id AS winner
        FROM ir_owners o JOIN ir_winners w USING(label) WHERE o.profile_id<>w.profile_id;
        CREATE UNIQUE INDEX ON ir_moves(loser);
        CREATE TEMP TABLE ir_assign ON COMMIT DROP AS
        SELECT l.id,w.profile_id FROM ir_labels l JOIN ir_winners w USING(label);
        CREATE UNIQUE INDEX ON ir_assign(id);
        CREATE TEMP TABLE ir_affected ON COMMIT DROP AS SELECT DISTINCT profile_id FROM ir_winners;
        CREATE UNIQUE INDEX ON ir_affected(profile_id);
        ANALYZE ir_assign; ANALYZE ir_moves; ANALYZE ir_affected;
    """)
    created = _sql(db, "SELECT count(*) FROM ir_new").scalar_one()
    merges = _sql(db, "SELECT count(*) FROM ir_moves").scalar_one()
    _sql(db, """
        INSERT INTO profile_merges(winner_id,loser_id,reason,job_id)
        SELECT m.winner,m.loser,jsonb_build_object('type',link.kind,'value',link.value),:job_id
        FROM ir_moves m CROSS JOIN LATERAL (
          SELECT i.type AS kind,i.value FROM identifiers i JOIN ir_edges e
          ON e.kind=i.type AND e.value=i.value WHERE i.profile_id=m.loser
          UNION ALL SELECT 'external',p.source_key || ':' || p.external_id
          FROM ir_pending p WHERE p.profile_id=m.loser LIMIT 1
        ) link
    """, job_id=job_id)
    _sql(db, "UPDATE profiles p SET merged_into_id=m.winner FROM ir_moves m WHERE p.id=m.loser")
    for table in ("identifiers", "source_records", "gifts", "events"):
        _sql(db, f"UPDATE {table} t SET profile_id=m.winner FROM ir_moves m WHERE t.profile_id=m.loser")
    _materialize(db)
    _sql(db, """
        UPDATE source_records sr SET profile_id=a.profile_id,resolved_at=now()
        FROM ir_assign a WHERE sr.id=a.id;
        UPDATE gifts g SET profile_id=a.profile_id FROM ir_assign a JOIN ir_pending p ON p.id=a.id
        JOIN imports i ON i.id=p.last_import_id
        WHERE i.record_type='gift' AND g.source_id=p.source_id AND g.external_id=p.external_id
          AND g.profile_id IS NULL;
        UPDATE events e SET profile_id=links.profile_id FROM (
          SELECT DISTINCT ON(p.source_id,k.message_id) p.source_id,k.message_id,a.profile_id
          FROM ir_pending p JOIN ir_assign a ON a.id=p.id
          CROSS JOIN LATERAL (VALUES(p.raw_hash),(p.external_id)) k(message_id)
          ORDER BY p.source_id,k.message_id,p.id
        ) links WHERE e.source_id=links.source_id AND e.message_id=links.message_id
          AND e.profile_id IS NULL;
        UPDATE events e SET profile_id=links.profile_id FROM (
          SELECT DISTINCT ON(p.source_id,k.value) p.source_id,k.value,a.profile_id
          FROM ir_edges k JOIN ir_pending p ON p.id=k.record_id JOIN ir_assign a ON a.id=p.id
          WHERE k.kind='anonymous_id' ORDER BY p.source_id,k.value,a.profile_id
        ) links WHERE e.source_id=links.source_id AND e.anonymous_id=links.value
          AND e.profile_id IS NULL;
        INSERT INTO identifiers(type,value,profile_id)
        SELECT DISTINCT e.kind,e.value,a.profile_id FROM ir_edges e
        JOIN ir_assign a ON a.id=e.record_id WHERE e.kind<>'profile'
        ORDER BY e.kind,e.value
        ON CONFLICT(type,value) DO UPDATE SET profile_id=EXCLUDED.profile_id
        WHERE identifiers.profile_id IS DISTINCT FROM EXCLUDED.profile_id
    """)
    _survivorship(db)
    for table in ("segment_membership", "profile_traits"):
        _sql(db, f"DELETE FROM {table} t USING ir_moves m WHERE t.profile_id=m.loser")
    _sql(db, """
        DELETE FROM profile_traits t USING ir_affected a WHERE t.profile_id=a.profile_id;
        INSERT INTO trait_dirty_profiles(profile_id,dirtied_at)
        SELECT profile_id,clock_timestamp() FROM ir_affected ORDER BY profile_id
        ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at
    """)
    return {"records": count, "profiles_created": created, "merges": merges}


def _materialize(db):
    _sql(db, """
        CREATE TEMP TABLE ir_consents ON COMMIT DROP AS
        SELECT COALESCE(m.winner,c.profile_id) AS profile_id,c.channel,c.status,c.source_id,
               c.captured_at,c.evidence,COALESCE(m.loser,0)::bigint AS tie
        FROM consents c LEFT JOIN ir_moves m ON m.loser=c.profile_id
        WHERE m.loser IS NOT NULL OR c.profile_id IN(SELECT profile_id FROM ir_affected)
        UNION ALL
        SELECT a.profile_id,p.attributes->'_import_consent'->>'channel',
               p.attributes->'_import_consent'->>'status',p.source_id,
               COALESCE((p.attributes->'_import_consent'->>'captured_at')::timestamptz,now()),
               jsonb_build_object('source_record_id',p.id),p.id
        FROM ir_pending p JOIN ir_assign a ON a.id=p.id
        WHERE NULLIF(p.attributes->'_import_consent'->>'channel','') IS NOT NULL
          AND NULLIF(p.attributes->'_import_consent'->>'status','') IS NOT NULL;
        DELETE FROM consents c USING ir_moves m WHERE c.profile_id=m.loser;
        INSERT INTO consents(profile_id,channel,status,source_id,captured_at,evidence)
        SELECT DISTINCT ON(profile_id,channel)
               profile_id,channel,status,source_id,captured_at,evidence FROM ir_consents
        ORDER BY profile_id,channel,(status='opted_out') DESC,captured_at DESC,tie
        ON CONFLICT(profile_id,channel) DO UPDATE SET status=EXCLUDED.status,
          source_id=EXCLUDED.source_id,captured_at=EXCLUDED.captured_at,evidence=EXCLUDED.evidence;
        CREATE TEMP TABLE ir_enrich ON COMMIT DROP AS
        SELECT COALESCE(m.winner,e.profile_id) AS profile_id,e.source_id,e.attribute_key,
               e.value_text,e.value_num,e.value_bool,e.value_date,e.imported_at,e.license_expires_at,
               COALESCE(m.loser,0)::bigint AS tie
        FROM enrichment_values e LEFT JOIN ir_moves m ON m.loser=e.profile_id
        WHERE m.loser IS NOT NULL OR e.profile_id IN(SELECT profile_id FROM ir_affected)
        UNION ALL
        SELECT a.profile_id,p.source_id,k.key,
               CASE WHEN k.value->>'data_type' IN('text','enum') THEN k.value->>'value' END,
               CASE WHEN k.value->>'data_type'='number' THEN (k.value->>'value')::numeric END,
               CASE WHEN k.value->>'data_type'='boolean' THEN (k.value->>'value')::boolean END,
               CASE WHEN k.value->>'data_type'='date' THEN (k.value->>'value')::date END,
               p.updated_at,NULL::timestamptz,p.id
        FROM ir_pending p JOIN ir_assign a ON a.id=p.id
        CROSS JOIN LATERAL jsonb_each(COALESCE(NULLIF(p.attributes->'_import_enrichments','null'::jsonb),'{}'::jsonb)) k
        WHERE k.value->>'data_type' IN('text','enum','number','boolean','date');
        DELETE FROM enrichment_values e USING ir_moves m WHERE e.profile_id=m.loser;
        INSERT INTO enrichment_values(profile_id,source_id,attribute_key,value_text,
          value_num,value_bool,value_date,imported_at,license_expires_at)
        SELECT DISTINCT ON(profile_id,source_id,attribute_key)
          profile_id,source_id,attribute_key,value_text,value_num,value_bool,value_date,
          imported_at,license_expires_at FROM ir_enrich
        ORDER BY profile_id,source_id,attribute_key,imported_at DESC,tie
        ON CONFLICT(profile_id,source_id,attribute_key) DO UPDATE SET
          value_text=EXCLUDED.value_text,value_num=EXCLUDED.value_num,
          value_bool=EXCLUDED.value_bool,value_date=EXCLUDED.value_date,
          imported_at=EXCLUDED.imported_at,license_expires_at=EXCLUDED.license_expires_at
    """)


def _survivorship(db):
    _sql(db, """
        CREATE TEMP TABLE ir_survivors ON COMMIT DROP AS
        SELECT sr.*,s.priority FROM source_records sr JOIN ir_affected a USING(profile_id)
        JOIN sources s ON s.id=sr.source_id;
        CREATE INDEX ON ir_survivors(profile_id,priority,updated_at DESC,id);
        ANALYZE ir_survivors;
        WITH emails AS (
          SELECT DISTINCT ON(profile_id) profile_id,email_norm FROM ir_survivors
          WHERE NULLIF(btrim(email_norm::text),'') IS NOT NULL
          ORDER BY profile_id,priority,updated_at DESC,id
        ), phones AS (
          SELECT DISTINCT ON(profile_id) profile_id,phone_e164 FROM ir_survivors
          WHERE NULLIF(btrim(phone_e164),'') IS NOT NULL
          ORDER BY profile_id,priority,updated_at DESC,id
        ), names AS (
          SELECT DISTINCT ON(profile_id) profile_id,first_name,last_name FROM ir_survivors
          WHERE NULLIF(first_name,'') IS NOT NULL OR NULLIF(last_name,'') IS NOT NULL
          ORDER BY profile_id,priority,updated_at DESC,id
        ), addresses AS (
          SELECT DISTINCT ON(profile_id) profile_id,address1,city,region,postal_code,country
          FROM ir_survivors WHERE NULLIF(address1,'') IS NOT NULL OR NULLIF(city,'') IS NOT NULL
            OR NULLIF(region,'') IS NOT NULL OR NULLIF(postal_code,'') IS NOT NULL OR NULLIF(country,'') IS NOT NULL
          ORDER BY profile_id,priority,updated_at DESC,id
        )
        UPDATE profiles p SET email=e.email_norm,phone=ph.phone_e164,
          first_name=n.first_name,last_name=n.last_name,address1=ad.address1,
          city=ad.city,region=ad.region,postal_code=ad.postal_code,country=ad.country,
          search_text=trim(concat_ws(' ',e.email_norm,ph.phone_e164,n.first_name,n.last_name,
            ad.address1,ad.city,ad.region,ad.postal_code,ad.country))
        FROM ir_affected a LEFT JOIN emails e USING(profile_id) LEFT JOIN phones ph USING(profile_id)
          LEFT JOIN names n USING(profile_id) LEFT JOIN addresses ad USING(profile_id)
        WHERE p.id=a.profile_id
    """)