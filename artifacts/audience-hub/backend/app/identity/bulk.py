"""Set-based identity resolution for a quiescent import queue.

The job handler waits for the import barrier, then performs one atomic refresh
of pending contact/consent/enrichment records, with SQL component state and
existing profile nodes; it never materializes records in Python. Transactional
gift/event rows are attached separately and never enter this graph.
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


def resolve_bulk(db: Session, job_id: int | None = None, *, mode: str = "auto",
                 limit: int | None = None) -> dict[str, int]:
    """Resolve all pending records; caller commits, including an empty result.

    Incremental closure consists of pending records, their unblocked keys and
    every existing profile owning one of those keys. Profile nodes connect all
    pending bridges before a winner is chosen. Unrelated resolved records are
    not rebuilt. All temporary state disappears on transaction completion.
    ``auto`` uses incremental write minimization only when the pending graph
    touches an existing identity. A fresh full load uses the original write path.
    Explicit ``full`` and ``incremental`` modes support controlled replay/tests.
    """
    if mode not in ("auto", "full", "incremental"):
        raise ValueError("Identity mode must be auto, full, or incremental")
    acquire_resolver_lock(db)
    # Use the existing partial (id WHERE resolved_at IS NULL) index, especially
    # on the worker's final empty call. Do not copy/join/count the whole source
    # table merely to discover that the graph is already drained.
    if _sql(db, """
        SELECT sr.id FROM source_records sr LEFT JOIN imports i ON i.id=sr.last_import_id
        WHERE sr.resolved_at IS NULL AND COALESCE(i.record_type,'contact') NOT IN ('gift','event')
        ORDER BY sr.id LIMIT 1
    """).first() is None:
        return {"records": 0, "profiles_created": 0, "merges": 0}
    # A bounded per-operation budget avoids default 4 MB aggregate/hash spills;
    # SET LOCAL disappears at the same commit as all temporary graph state.
    names = ("pending", "edges", "labels", "key_labels", "owners", "winners",
                 "new", "moves", "assign", "affected", "consents", "enrich", "survivors",
                 "keys", "conflicts", "selected")
    _sql(db, "SET LOCAL work_mem='64MB';" +
         "".join(f"DROP TABLE IF EXISTS pg_temp.ir_{name};" for name in names) + """
        CREATE TEMP TABLE ir_pending ON COMMIT DROP AS
        SELECT sr.*, s.key AS source_key
        FROM source_records sr JOIN sources s ON s.id=sr.source_id
        LEFT JOIN imports i ON i.id=sr.last_import_id
        WHERE sr.resolved_at IS NULL AND COALESCE(i.record_type,'contact') NOT IN ('gift','event')
    """ + (" ORDER BY sr.id LIMIT 10000" if limit is not None else "") + """;
        CREATE UNIQUE INDEX ON ir_pending(id); ANALYZE ir_pending;
        CREATE TEMP TABLE ir_edges (
          record_id bigint NOT NULL, kind text NOT NULL, value text NOT NULL,
          PRIMARY KEY(record_id,kind,value)
        ) ON COMMIT DROP
    """)
    count = _sql(db, "SELECT count(*) FROM ir_pending").scalar_one()
    if not count:
        return {"records": 0, "profiles_created": 0, "merges": 0}
    _sql(db, """
        INSERT INTO ir_edges
        SELECT DISTINCT p.id, k.kind, k.value
        FROM ir_pending p CROSS JOIN LATERAL (
          VALUES
            ('external', p.source_key || ':' || NULLIF(btrim(p.external_id,:whitespace),'')),
            ('email', NULLIF(lower(btrim(p.email_norm::text,:whitespace)),'')),
            ('phone', NULLIF(p.phone_e164,'')),
            ('email_md5', CASE WHEN p.email_norm IS NOT NULL THEN md5(lower(btrim(p.email_norm::text,:whitespace)))
               ELSE lower(NULLIF(btrim(p.attributes->>'email_md5'),'')) END),
            ('email_sha256', CASE WHEN p.email_norm IS NOT NULL
               THEN encode(digest(lower(btrim(p.email_norm::text,:whitespace)),'sha256'),'hex')
               ELSE lower(NULLIF(btrim(p.attributes->>'email_sha256'),'')) END),
            ('donor_account_number',NULLIF(btrim(p.attributes->>'donor_account_number'),'')),
            ('hubspot_record_id',NULLIF(btrim(p.attributes->>'hubspot_record_id'),'')),
            ('okta_customer_id',NULLIF(btrim(p.attributes->>'okta_customer_id'),'')),
            ('zeta_cdp_id',NULLIF(btrim(p.attributes->>'zeta_cdp_id'),'')),
            ('zeta_uid',lower(NULLIF(btrim(p.attributes->>'zeta_uid'),''))),
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
        ) AS k(kind,value) WHERE k.value IS NOT NULL;
        CREATE INDEX ON ir_edges(kind,value); ANALYZE ir_edges
    """.replace(":whitespace", "$kin_ws$" + _WHITESPACE + "$kin_ws$"))
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
        DELETE FROM ir_edges h USING ir_pending p
        WHERE h.record_id=p.id AND h.kind IN ('email_md5','email_sha256')
          AND p.email_norm IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM ir_edges e WHERE e.record_id=p.id AND e.kind='email');
        DELETE FROM ir_edges h USING identifier_blocklist b
        WHERE b.type='email' AND
          ((h.kind='email_md5' AND h.value=md5(lower(b.value)))
            OR (h.kind='email_sha256' AND h.value=encode(digest(lower(b.value),'sha256'),'hex')));
        CREATE TEMP TABLE ir_keys ON COMMIT DROP AS SELECT * FROM ir_edges;
        CREATE INDEX ON ir_keys(record_id);
        CREATE INDEX ON ir_keys(kind,value); ANALYZE ir_keys
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
        WHERE NOT EXISTS(SELECT 1 FROM identity_xref x WHERE x.key_type=
          CASE e.kind WHEN 'phone' THEN 'mobile_phone' ELSE e.kind END AND x.key_value=e.value)
        UNION SELECT e.record_id,'profile',p.id::text
        FROM ir_edges e JOIN identity_xref x ON x.key_type=
          CASE e.kind WHEN 'phone' THEN 'mobile_phone' ELSE e.kind END AND x.key_value=e.value
        JOIN profiles p ON p.master_id=x.master_id WHERE p.merged_into_id IS NULL AND NOT p.is_deleted
        UNION SELECT id,'profile',profile_id::text FROM ir_pending WHERE profile_id IS NOT NULL
        ON CONFLICT DO NOTHING;
        ANALYZE ir_edges;
        CREATE TEMP TABLE ir_labels ON COMMIT DROP AS SELECT id, id AS label FROM ir_pending;
        CREATE UNIQUE INDEX ON ir_labels(id);
        ANALYZE ir_labels;
        CREATE TEMP TABLE ir_key_labels(
          kind text, value text, label bigint, PRIMARY KEY(kind,value)
        ) ON COMMIT DROP;
        SET LOCAL work_mem='256MB'
    """)
    # The measured medium graph's text-key aggregation is expensive and its
    # millions of groups can spill at 64 MB. Bound the larger budget to this serial
    # phase only; all later materialization/survivorship returns to 64 MB.
    # No labels, graph edges, or convergence rules change.
    while True:
        changed = _sql(db, """
            WITH key_labels AS MATERIALIZED (
              SELECT e.kind,e.value,min(l.label) AS label FROM ir_edges e
              JOIN ir_labels l ON l.id=e.record_id GROUP BY e.kind,e.value
            )
            UPDATE ir_labels l SET label=n.label FROM (
              SELECT e.record_id,min(k.label) AS label FROM ir_edges e
              JOIN key_labels k USING(kind,value) GROUP BY e.record_id
            ) n WHERE l.id=n.record_id AND l.label>n.label
        """).rowcount
        if not changed:
            break
    from app.identity.graph import constrain_components
    quarantined = constrain_components(db)
    if limit is not None:
        bounded = max(1, min(int(limit), 500))
        _sql(db, f"""
          CREATE TEMP TABLE ir_selected ON COMMIT DROP AS
          WITH components AS (
            SELECT label,min(id) AS first,count(*) AS n FROM ir_labels GROUP BY label
          ), ranked AS (
            SELECT *,sum(n) OVER(ORDER BY first) AS total,
              row_number() OVER(ORDER BY first) AS pos FROM components
          )
          SELECT l.id FROM ir_labels l JOIN ranked c USING(label)
          WHERE c.total<={bounded} OR (c.pos=1 AND c.n>{bounded})
          ORDER BY l.id LIMIT {bounded};
          DELETE FROM ir_pending p WHERE NOT EXISTS(SELECT 1 FROM ir_selected s WHERE s.id=p.id);
          DELETE FROM ir_keys k WHERE NOT EXISTS(SELECT 1 FROM ir_selected s WHERE s.id=k.record_id);
          DELETE FROM ir_edges e WHERE NOT EXISTS(SELECT 1 FROM ir_selected s WHERE s.id=e.record_id);
          DELETE FROM ir_labels l WHERE NOT EXISTS(SELECT 1 FROM ir_selected s WHERE s.id=l.id);
          ANALYZE ir_pending; ANALYZE ir_keys; ANALYZE ir_edges; ANALYZE ir_labels
        """)
    _sql(db, """
        CREATE INDEX ON ir_labels(label);
        CREATE TEMP TABLE ir_owners ON COMMIT DROP AS
        SELECT DISTINCT l.label,e.value::bigint AS profile_id FROM ir_edges e
        JOIN ir_labels l ON l.id=e.record_id WHERE e.kind='profile';
        CREATE INDEX ON ir_owners(label);
        ANALYZE ir_owners;
        CREATE TEMP TABLE ir_winners ON COMMIT DROP AS
        SELECT DISTINCT ON(o.label) o.label,p.id AS profile_id FROM ir_owners o
        JOIN profiles p ON p.id=o.profile_id
        JOIN master_id_registry r ON r.master_id=p.master_id
        ORDER BY o.label,left(p.master_id,14),r.issued_at,p.id;
        CREATE UNIQUE INDEX ON ir_winners(label);
        CREATE TEMP TABLE ir_new ON COMMIT DROP AS
        SELECT label,nextval(pg_get_serial_sequence('profiles','id')) AS profile_id
        FROM (SELECT DISTINCT label FROM ir_labels EXCEPT SELECT label FROM ir_winners) missing;
        INSERT INTO profiles(id,first_seen_at,last_seen_at) OVERRIDING SYSTEM VALUE
        SELECT profile_id,now(),now() FROM ir_new;
        ANALYZE profiles;
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
    created, merges, selected, has_owners = _sql(db, """
      SELECT (SELECT count(*) FROM ir_new),(SELECT count(*) FROM ir_moves),
        (SELECT count(*) FROM ir_assign),EXISTS(SELECT 1 FROM ir_owners)
    """).one()
    if limit is not None:
        count = selected + quarantined
    skip_unchanged = mode == "incremental" or (mode == "auto" and has_owners)
    # Attribute newly arrived bridge rows to the survivor before journaling;
    # existing rows retain their own side until the journal is durable in this
    # transaction. A merge must be reversible even when triggered by a new row.
    _sql(db, """
      UPDATE source_records sr SET profile_id=COALESCE(sr.profile_id,a.profile_id),resolved_at=now()
      FROM ir_assign a WHERE sr.id=a.id
    """ + (" AND sr.resolved_at IS NULL" if skip_unchanged else "") + """;
      INSERT INTO identifiers(type,value,profile_id)
      SELECT DISTINCT ON(k.kind,k.value) k.kind,k.value,s.profile_id
      FROM ir_keys k JOIN source_records s ON s.id=k.record_id
      WHERE true
    """ + ("""
      AND NOT EXISTS(SELECT 1 FROM identifiers i WHERE i.type=k.kind
        AND i.value=k.value AND i.profile_id=s.profile_id)
    """ if skip_unchanged else "") + """
      ORDER BY k.kind,k.value,s.profile_id
      ON CONFLICT(type,value) DO NOTHING;
      INSERT INTO identity_xref(master_id,key_type,key_value,source_id,first_seen_at,last_seen_at)
      SELECT p.master_id,CASE k.kind WHEN 'phone' THEN 'mobile_phone' ELSE k.kind END,
        k.value,s.source_id,min(s.created_at),max(s.updated_at)
      FROM ir_keys k JOIN source_records s ON s.id=k.record_id
      JOIN profiles p ON p.id=s.profile_id
      WHERE k.kind <> 'external'
      GROUP BY p.master_id,k.kind,k.value,s.source_id
      ON CONFLICT(master_id,key_type,key_value,source_id) DO UPDATE SET
        first_seen_at=least(identity_xref.first_seen_at,EXCLUDED.first_seen_at),
        last_seen_at=greatest(identity_xref.last_seen_at,EXCLUDED.last_seen_at)
      WHERE identity_xref.first_seen_at>EXCLUDED.first_seen_at
         OR identity_xref.last_seen_at<EXCLUDED.last_seen_at
    """)
    from app.identity.master import journal_merge
    from app.identity.resolver import _move_profile_references
    for winner, loser in _sql(db, "SELECT winner,loser FROM ir_moves ORDER BY winner,loser").all():
        journal_merge(db, winner, loser, job_id)
        _move_profile_references(db, winner, loser)
    if merges:
        _sql(db, "UPDATE profiles p SET merged_into_id=m.winner FROM ir_moves m WHERE p.id=m.loser")
        from app.identity.devices import rehome_devices
        rehome_devices(db)
    _materialize(db, skip_unchanged=skip_unchanged)
    _sql(db, """
      UPDATE identity_xref x SET status='duplicate_in_source'
      WHERE key_type='hubspot_record_id' AND master_id IN (
        SELECT master_id FROM identity_xref WHERE key_type='hubspot_record_id'
        GROUP BY master_id HAVING count(DISTINCT key_value)>1)
    """)
    _survivorship(db, skip_unchanged=skip_unchanged)
    _sql(db, """
        DELETE FROM profile_traits t USING ir_affected a WHERE t.profile_id=a.profile_id;
        INSERT INTO trait_dirty_profiles(profile_id,dirtied_at)
        SELECT profile_id,clock_timestamp() FROM ir_affected ORDER BY profile_id
        ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at;
        -- Attachments run before the handler's final statistics refresh. They
        -- must not plan against empty pre-import crosswalk/ownership estimates.
        ANALYZE identity_xref; ANALYZE identifiers; ANALYZE source_records
    """)
    return {"records": count, "profiles_created": created, "merges": merges}


def _materialize(db, *, skip_unchanged: bool):
    consent_scope = """
        m.loser IS NOT NULL OR c.profile_id IN(SELECT winner FROM ir_moves)
        OR c.profile_id IN (
          SELECT a.profile_id FROM ir_assign a JOIN ir_pending p ON p.id=a.id
          WHERE NULLIF(p.attributes->'_import_consent'->>'channel','') IS NOT NULL
            AND NULLIF(p.attributes->'_import_consent'->>'status','') IS NOT NULL
        )
    """ if skip_unchanged else "m.loser IS NOT NULL OR c.profile_id IN(SELECT profile_id FROM ir_affected)"
    enrichment_scope = """
        m.loser IS NOT NULL OR e.profile_id IN(SELECT winner FROM ir_moves)
        OR e.profile_id IN (
          SELECT a.profile_id FROM ir_assign a JOIN ir_pending p ON p.id=a.id
          WHERE COALESCE(NULLIF(p.attributes->'_import_enrichments','null'::jsonb),'{}'::jsonb)<>'{}'::jsonb
        )
    """ if skip_unchanged else "m.loser IS NOT NULL OR e.profile_id IN(SELECT profile_id FROM ir_affected)"
    _sql(db, """
        CREATE TEMP TABLE ir_consents ON COMMIT DROP AS
        SELECT COALESCE(m.winner,c.profile_id) AS profile_id,c.channel,c.status,c.source_id,
               c.captured_at,c.evidence,COALESCE(m.loser,0)::bigint AS tie
        FROM consents c LEFT JOIN ir_moves m ON m.loser=c.profile_id
        WHERE """ + consent_scope + """
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
               profile_id,channel,status,source_id,captured_at,evidence FROM (
          SELECT c.*, s.settings->>'authoritative_for_consent'='true' AS authoritative,
            max(c.captured_at) FILTER(WHERE c.status='opted_out')
              OVER(PARTITION BY c.profile_id,c.channel) AS last_out
          FROM ir_consents c JOIN sources s ON s.id=c.source_id
        ) candidates
        ORDER BY profile_id,channel,
          CASE WHEN status='opted_in' AND authoritative AND
            captured_at>COALESCE(last_out,'-infinity'::timestamptz) THEN 3
            WHEN status='opted_out' THEN 2 WHEN status='opted_in' THEN 1 ELSE 0 END DESC,
          captured_at DESC,tie
        ON CONFLICT(profile_id,channel) DO UPDATE SET status=EXCLUDED.status,
          source_id=EXCLUDED.source_id,captured_at=EXCLUDED.captured_at,evidence=EXCLUDED.evidence
    """ + ("""
        WHERE (consents.status,consents.source_id,consents.captured_at,consents.evidence)
          IS DISTINCT FROM (EXCLUDED.status,EXCLUDED.source_id,EXCLUDED.captured_at,EXCLUDED.evidence)
    """ if skip_unchanged else "") + """;
        CREATE TEMP TABLE ir_enrich ON COMMIT DROP AS
        SELECT COALESCE(m.winner,e.profile_id) AS profile_id,e.source_id,e.attribute_key,
               e.value_text,e.value_num,e.value_bool,e.value_date,e.imported_at,e.license_expires_at,
               e.vendor,e.license_id,e.append_date,e.sensitive,
               COALESCE(m.loser,0)::bigint AS tie
        FROM enrichment_values e LEFT JOIN ir_moves m ON m.loser=e.profile_id
        WHERE """ + enrichment_scope + """
        UNION ALL
        SELECT a.profile_id,p.source_id,k.key,
               CASE WHEN k.value->>'data_type' IN('text','enum') THEN k.value->>'value' END,
               CASE WHEN k.value->>'data_type'='number' THEN (k.value->>'value')::numeric END,
               CASE WHEN k.value->>'data_type'='boolean' THEN (k.value->>'value')::boolean END,
               CASE WHEN k.value->>'data_type'='date' THEN (k.value->>'value')::date END,
               p.updated_at,NULL::timestamptz,k.value->>'vendor',k.value->>'license_id',
               (k.value->>'append_date')::date,COALESCE((k.value->>'sensitive')::boolean,false),p.id
        FROM ir_pending p JOIN ir_assign a ON a.id=p.id
        CROSS JOIN LATERAL jsonb_each(COALESCE(NULLIF(p.attributes->'_import_enrichments','null'::jsonb),'{}'::jsonb)) k
        WHERE k.value->>'data_type' IN('text','enum','number','boolean','date');
        DELETE FROM enrichment_values e USING ir_moves m WHERE e.profile_id=m.loser;
        INSERT INTO enrichment_values(profile_id,source_id,attribute_key,value_text,
          value_num,value_bool,value_date,imported_at,license_expires_at,vendor,license_id,append_date,sensitive)
        SELECT DISTINCT ON(profile_id,source_id,attribute_key)
          profile_id,source_id,attribute_key,value_text,value_num,value_bool,value_date,
          imported_at,license_expires_at,vendor,license_id,append_date,sensitive FROM ir_enrich
        ORDER BY profile_id,source_id,attribute_key,imported_at DESC,tie
        ON CONFLICT(profile_id,source_id,attribute_key) DO UPDATE SET
          value_text=EXCLUDED.value_text,value_num=EXCLUDED.value_num,
          value_bool=EXCLUDED.value_bool,value_date=EXCLUDED.value_date,
          imported_at=EXCLUDED.imported_at,license_expires_at=EXCLUDED.license_expires_at,
          vendor=EXCLUDED.vendor,license_id=EXCLUDED.license_id,
          append_date=EXCLUDED.append_date,sensitive=EXCLUDED.sensitive
    """ + ("""
        WHERE (enrichment_values.value_text,enrichment_values.value_num,
          enrichment_values.value_bool,enrichment_values.value_date,
          enrichment_values.imported_at,enrichment_values.license_expires_at,
          enrichment_values.vendor,enrichment_values.license_id,enrichment_values.append_date,enrichment_values.sensitive)
          IS DISTINCT FROM (EXCLUDED.value_text,EXCLUDED.value_num,
            EXCLUDED.value_bool,EXCLUDED.value_date,EXCLUDED.imported_at,EXCLUDED.license_expires_at,
            EXCLUDED.vendor,EXCLUDED.license_id,EXCLUDED.append_date,EXCLUDED.sensitive)
    """ if skip_unchanged else ""))


def _survivorship(db, *, skip_unchanged: bool):
    _sql(db, """
        CREATE TEMP TABLE ir_survivors ON COMMIT DROP AS
        SELECT sr.*,s.priority FROM source_records sr JOIN ir_affected a USING(profile_id)
        JOIN sources s ON s.id=sr.source_id
        LEFT JOIN imports i ON i.id=sr.last_import_id
        WHERE COALESCE(i.record_type,'contact') NOT IN ('gift','event');
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
    """ + ("""
          AND (p.email::text,p.phone,p.first_name,p.last_name,p.address1,p.city,
            p.region,p.postal_code,p.country,p.search_text)
          IS DISTINCT FROM (e.email_norm::text,ph.phone_e164,n.first_name,n.last_name,
            ad.address1,ad.city,ad.region,ad.postal_code,ad.country,
            trim(concat_ws(' ',e.email_norm,ph.phone_e164,n.first_name,n.last_name,
              ad.address1,ad.city,ad.region,ad.postal_code,ad.country)))
    """ if skip_unchanged else ""))