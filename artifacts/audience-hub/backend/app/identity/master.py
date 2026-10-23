"""Master identity crosswalk and pre-move merge journal. Caller owns transaction."""
import json
import re

from sqlalchemy import text

SYSTEM_KEYS = ("donor_account_number", "hubspot_record_id", "okta_customer_id", "fusionauth_user_id",
               "zeta_cdp_id", "zeta_uid", "meta_app_user_id")
CONTACT_KEYS = ("email", "email_md5", "email_sha256", "mobile_phone")
KEY_TYPES = SYSTEM_KEYS + CONTACT_KEYS + ("meta_psid",)
MASTER_RE = re.compile(r"^kin_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def normalize_lookup(kind, value):
    from app.identity.normalize import normalize_email, normalize_phone
    if kind not in KEY_TYPES:
        raise ValueError("Unsupported identity key_type")
    value = str(value).strip()
    if not value or len(value) > 512:
        raise ValueError("Identity value must be 1–512 characters")
    if kind == "email":
        value = normalize_email(value)
    elif kind == "mobile_phone":
        value = normalize_phone(value)
    elif kind in ("email_md5", "email_sha256"):
        value = value.lower()
        size = 32 if kind == "email_md5" else 64
        if not re.fullmatch(f"[0-9a-f]{{{size}}}", value):
            raise ValueError("Invalid email hash")
    elif kind == "zeta_uid":
        from uuid import UUID
        value = str(UUID(value))
    elif kind == "zeta_cdp_id" and not value.startswith("cdp-"):
        raise ValueError("zeta_cdp_id must start with cdp-")
    elif kind in ("meta_app_user_id", "meta_psid") and not re.fullmatch(r"\d{1,40}:\d{1,40}", value):
        raise ValueError("Meta keys must be scoped as app_id:user_id or page_id:psid")
    if not value:
        raise ValueError("Invalid contact key")
    return value


def lookup_bulk(db, items):
    """One indexed set-based query, preserving order and duplicates (max 10k)."""
    if not 1 <= len(items) <= 10000:
        raise ValueError("Lookup requires 1–10000 keys")
    normalized = [(i["key_type"], normalize_lookup(i["key_type"], i["value"])) for i in items]
    rows = db.execute(text("""
      WITH RECURSIVE input AS (
        SELECT kind,value,ord FROM unnest(CAST(:k AS text[]),CAST(:v AS text[]))
          WITH ORDINALITY AS i(kind,value,ord)
      ), matches AS (
        SELECT i.ord,x.master_id FROM input i JOIN identity_xref x
          ON x.key_type=i.kind AND x.key_value=i.value
      ), chain(ord,id,path) AS (
        SELECT ord,master_id,ARRAY[master_id] FROM matches
        UNION ALL SELECT c.ord,a.survivor_id,c.path||a.survivor_id FROM chain c
          JOIN master_id_aliases a ON a.retired_id=c.id WHERE NOT a.survivor_id=ANY(c.path)
      ), result AS (
        SELECT c.ord,array_agg(DISTINCT p.master_id) AS masters FROM chain c
        JOIN profiles p ON p.master_id=c.id AND p.merged_into_id IS NULL AND NOT p.is_deleted
        GROUP BY c.ord
      )
      SELECT i.ord,r.masters FROM input i LEFT JOIN result r USING(ord) ORDER BY i.ord
    """), {"k": [x[0] for x in normalized], "v": [x[1] for x in normalized]}).all()
    return [
        {"key_type": normalized[ord - 1][0], "value": normalized[ord - 1][1],
         "status": "not_found" if not masters else "matched" if len(masters) == 1 else "ambiguous",
         "master_id": masters[0] if masters and len(masters) == 1 else None}
        for ord, masters in rows
    ]


def backfill_master_ids(db, batch_size=5000):
    """One resumable batch; commit between calls, stop when profiles == 0.

    Includes merged/deleted rows so historical masters can never be reassigned.
    Registry survives privacy deletion and contains no PII.
    """
    from app.identity.locking import acquire_resolver_lock
    acquire_resolver_lock(db)
    rows = db.execute(text("""
      WITH batch AS (SELECT id FROM profiles WHERE master_id IS NULL
        ORDER BY first_seen_at,id LIMIT :n FOR UPDATE)
      UPDATE profiles p SET master_id=NULL FROM batch b WHERE p.id=b.id RETURNING p.id
    """), {"n": max(1, min(int(batch_size), 50000))}).scalars().all()
    if rows:
        sync_legacy_xref(db, rows)
    # Historical merges have no reversible journal; aliases still resolve.
    db.execute(text("""
      INSERT INTO master_id_aliases(retired_id,survivor_id,merge_id)
      SELECT l.master_id,w.master_id,m.id FROM profiles l
      JOIN profiles w ON w.id=l.merged_into_id
      LEFT JOIN LATERAL (SELECT id FROM profile_merges WHERE loser_id=l.id
        ORDER BY id DESC LIMIT 1) m ON true
      WHERE l.master_id IS NOT NULL AND w.master_id IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM master_id_aliases a WHERE a.retired_id=l.master_id)
      ON CONFLICT(retired_id) DO NOTHING
    """))
    return len(rows)


def sync_legacy_xref(db, ids):
    db.execute(text("""
      INSERT INTO identity_xref(master_id,key_type,key_value,first_seen_at,last_seen_at)
      SELECT p.master_id,CASE i.type WHEN 'phone' THEN 'mobile_phone' ELSE i.type END,
        i.value,p.first_seen_at,p.last_seen_at
      FROM identifiers i JOIN profiles p ON p.id=i.profile_id
      WHERE p.id=ANY(:ids) AND p.master_id IS NOT NULL AND i.type <> 'external'
      ON CONFLICT(master_id,key_type,key_value,source_id) DO NOTHING
    """), {"ids": list(ids)})
    db.execute(text("""
      INSERT INTO identity_xref(master_id,key_type,key_value,source_id,first_seen_at,last_seen_at)
      SELECT p.master_id,k.kind,k.value,s.source_id,min(s.created_at),max(s.updated_at)
      FROM source_records s JOIN profiles p ON p.id=s.profile_id CROSS JOIN LATERAL (
        VALUES ('donor_account_number',NULLIF(btrim(s.attributes->>'donor_account_number'),'')),
          ('hubspot_record_id',NULLIF(btrim(s.attributes->>'hubspot_record_id'),'')),
          ('okta_customer_id',NULLIF(btrim(s.attributes->>'okta_customer_id'),'')),
          ('fusionauth_user_id',NULLIF(btrim(s.attributes->>'fusionauth_user_id'),'')),
          ('zeta_cdp_id',NULLIF(btrim(s.attributes->>'zeta_cdp_id'),'')),
          ('zeta_uid',lower(NULLIF(btrim(s.attributes->>'zeta_uid'),''))),
          ('email_md5',CASE WHEN s.email_norm IS NOT NULL THEN md5(lower(btrim(
            COALESCE(s.attributes->>'email_raw',s.email_norm::text))))
            ELSE lower(NULLIF(btrim(s.attributes->>'email_md5'),'')) END),
          ('email_sha256',CASE WHEN s.email_norm IS NOT NULL THEN encode(digest(lower(btrim(
            COALESCE(s.attributes->>'email_raw',s.email_norm::text))),'sha256'),'hex')
            ELSE lower(NULLIF(btrim(s.attributes->>'email_sha256'),'')) END)
      ) k(kind,value) WHERE p.id=ANY(:ids) AND k.value IS NOT NULL
      GROUP BY p.master_id,k.kind,k.value,s.source_id
      ON CONFLICT(master_id,key_type,key_value,source_id) DO NOTHING
    """), {"ids": list(ids)})


def resolve_master(db, master_id):
    if not MASTER_RE.fullmatch(master_id):
        raise ValueError("Invalid master_id")
    return db.scalar(text("""
      WITH RECURSIVE chain(id,path) AS (
        SELECT CAST(:id AS text),ARRAY[CAST(:id AS text)]
        UNION ALL SELECT a.survivor_id,c.path||a.survivor_id FROM chain c
        JOIN master_id_aliases a ON a.retired_id=c.id
        WHERE NOT a.survivor_id=ANY(c.path)
      )
      SELECT p.master_id FROM chain c JOIN profiles p ON p.master_id=c.id
      WHERE p.merged_into_id IS NULL AND NOT p.is_deleted
      ORDER BY cardinality(c.path) DESC LIMIT 1
    """), {"id": master_id})


def journal_merge(db, winner, loser, job_id=None):
    """Snapshot BOTH sides before any move, including trait source rows.

    Source snapshots are evidence, never replayed over mutable source records.
    """
    locked = db.execute(text("""
      SELECT id,master_id FROM profiles WHERE id=ANY(:ids) ORDER BY id FOR UPDATE
    """), {"ids": [winner, loser]}).all()
    masters = dict(locked)
    if len(masters) != 2 or not all(masters.values()):
        raise ValueError("Backfill master IDs before resolving merges")
    merge_id = db.scalar(text("""
      INSERT INTO profile_merges(winner_id,loser_id,reason,job_id)
      VALUES(:w,:l,'{"type":"identity"}',:j) RETURNING id
    """), {"w": winner, "l": loser, "j": job_id})
    sides = {}
    for label, pid in (("winner", winner), ("loser", loser)):
        side = {"profile_id": pid, "master_id": masters[pid]}
        for table in ("identifiers", "source_records", "gifts", "events",
                      "consents", "enrichment_values", "profile_traits"):
            payload = ("jsonb_build_object('id',t.id,'occurred_at',t.occurred_at,'source_id',t.source_id)"
                       if table == "events" else
                       "jsonb_build_object('id',t.id,'source_record_id',t.source_record_id,'source_id',t.source_id)"
                       if table == "gifts" else "to_jsonb(t)")
            side[table] = db.scalar(text(
                f"SELECT COALESCE(jsonb_agg({payload}),'[]'::jsonb) "
                f"FROM {table} t WHERE profile_id=:id"
            ), {"id": pid})
        side["xref"] = db.scalar(text("""
          SELECT COALESCE(jsonb_agg(to_jsonb(t)),'[]'::jsonb)
          FROM identity_xref t WHERE master_id=:id
        """), {"id": masters[pid]})
        sides[label] = side
    statements = """
      INSERT INTO identity_merge_journal(merge_id,winner_id,loser_id,sides)
      VALUES(:m,:w,:l,CAST(:s AS jsonb));
      INSERT INTO master_id_aliases(retired_id,survivor_id,merge_id) VALUES(:old,:new,:m);
      INSERT INTO identity_xref(master_id,key_type,key_value,source_id,first_seen_at,last_seen_at,status,
        method,confidence,rule)
      SELECT :new,key_type,key_value,source_id,first_seen_at,last_seen_at,status,method,confidence,rule
      FROM identity_xref WHERE master_id=:old
      ON CONFLICT(master_id,key_type,key_value,source_id) DO UPDATE SET
        first_seen_at=least(identity_xref.first_seen_at,EXCLUDED.first_seen_at),
        last_seen_at=greatest(identity_xref.last_seen_at,EXCLUDED.last_seen_at);
      DELETE FROM identity_xref WHERE master_id=:old
    """
    for statement in statements.split(";"):
        if statement.strip():
            db.execute(text(statement), {"m": merge_id, "w": winner, "l": loser,
                       "s": json.dumps(sides, default=str),
                       "old": masters[loser], "new": masters[winner]})
    return merge_id


def unmerge(db, merge_id, actor):
    """Reverse most recent merge first; refuse ambiguous post-merge mutations."""
    from app.identity.locking import acquire_resolver_lock
    from app.identity.survivorship import recompute_profiles_fields
    from app.privacy.service import audit
    acquire_resolver_lock(db)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"))
    journal = db.execute(text("""
      SELECT * FROM identity_merge_journal WHERE merge_id=:id FOR UPDATE
    """), {"id": merge_id}).mappings().first()
    if not journal:
        raise LookupError("Reversible merge not found (legacy or erased provenance)")
    if journal["undone_at"]:
        raise ValueError("Merge already reversed")
    w, l = journal["winner_id"], journal["loser_id"]
    rows = db.execute(text("SELECT * FROM profiles WHERE id=ANY(:ids) ORDER BY id FOR UPDATE"),
                      {"ids": [w, l]}).mappings().all()
    if len(rows) != 2 or any(r["is_deleted"] for r in rows):
        raise ValueError("Erased identity cannot be restored")
    if next(r["merged_into_id"] for r in rows if r["id"] == w) is not None:
        raise ValueError("Reverse later dependent merges first")
    if db.scalar(text("""
      SELECT EXISTS(SELECT 1 FROM identity_merge_journal
       WHERE merge_id>:m AND undone_at IS NULL
       AND (winner_id=ANY(:ids) OR loser_id=ANY(:ids)))
    """), {"m": merge_id, "ids": [w, l]}):
        raise ValueError("Reverse later dependent merges first")
    sides = journal["sides"]
    # Oldest recorded system key determines which logical side retains survivor.
    from datetime import datetime, timezone
    def age(side):
        return min(((datetime.fromisoformat(x["first_seen_at"]), x["id"])
                    for x in side["xref"] if x["key_type"] in SYSTEM_KEYS),
                   default=(datetime.max.replace(tzinfo=timezone.utc), 0))
    retained = "winner" if age(sides["winner"]) <= age(sides["loser"]) else "loser"
    # New incoming source records cannot be guessed into either side.
    known_sources = {r["id"] for s in sides.values() for r in s["source_records"]}
    current_sources = db.execute(text("SELECT id,updated_at FROM source_records WHERE profile_id=:p"),
                                 {"p": w}).all()
    expected = {r["id"]: r["updated_at"] for s in sides.values() for r in s["source_records"]}
    if set(r.id for r in current_sources) != known_sources or any(
        r.updated_at != datetime.fromisoformat(expected[r.id]) for r in current_sources
    ):
        raise ValueError("Source records changed since merge; manual review required")
    for table in ("gifts", "events"):
        if table == "events":
            known = {(r["id"], datetime.fromisoformat(r["occurred_at"]))
                     for side in sides.values() for r in side[table]}
            current = set(db.execute(text("SELECT id,occurred_at FROM events WHERE profile_id=:p"), {"p": w}))
        else:
            known = {r["id"] for side in sides.values() for r in side[table]}
            current = set(db.scalars(text(f"SELECT id FROM {table} WHERE profile_id=:p"), {"p": w}))
        if current != known:
            raise ValueError("Transactions changed since merge; manual review required")
    # A manual preference/enrichment mutation must never be overwritten with a
    # stale snapshot. A current row must be one of the two pre-merge candidates.
    current_consents = db.scalar(text("""
      SELECT COALESCE(jsonb_agg(to_jsonb(c)),'[]'::jsonb) FROM consents c WHERE profile_id=:p
    """), {"p": w})
    for table, current in (("consents", current_consents), ("enrichment_values", db.scalar(text("""
      SELECT COALESCE(jsonb_agg(to_jsonb(e)),'[]'::jsonb) FROM enrichment_values e WHERE profile_id=:p
    """), {"p": w}))):
        def comparable(row):
            return {k: v for k, v in row.items() if k not in ("id", "profile_id")}
        allowed = [comparable(r) for side in sides.values() for r in side[table]]
        key = (lambda r: r["channel"]) if table == "consents" else (
            lambda r: (r["source_id"], r["attribute_key"]))
        if (any(comparable(r) not in allowed for r in current)
                or {key(r) for r in current} != {key(r) for r in allowed}):
            raise ValueError("Consent or enrichment changed since merge; manual review required")
    new = db.execute(text("INSERT INTO profiles DEFAULT VALUES RETURNING id,master_id")).one()
    target = {"winner": w if retained == "winner" else new.id,
              "loser": w if retained == "loser" else new.id}
    for label, side in sides.items():
        pid = target[label]
        for table in ("identifiers", "source_records", "gifts", "events"):
            ids = [r["id"] for r in side[table]]
            if ids and table == "events":
                db.execute(text("""
                  UPDATE events e SET profile_id=:p FROM jsonb_to_recordset(CAST(:rows AS jsonb))
                    AS r(id bigint,occurred_at timestamptz)
                  WHERE e.profile_id=:w AND e.id=r.id AND e.occurred_at=r.occurred_at
                """), {"p": pid, "w": w, "rows": json.dumps(side[table])})
            elif ids:
                db.execute(text(f"UPDATE {table} SET profile_id=:p WHERE profile_id=:w AND id=ANY(:ids)"),
                           {"p": pid, "w": w, "ids": ids})
    # Restore side-specific consent, but an opt-out remains effective on BOTH
    # sides. Never spread a merged opt-in to a side that did not supply it.
    db.execute(text("DELETE FROM consents WHERE profile_id=:w"), {"w": w})
    optouts = {r["channel"]: r for r in current_consents if r["status"] == "opted_out"}
    for label, side in sides.items():
        rows_by_channel = {r["channel"]: r for r in side["consents"]}
        rows_by_channel.update(optouts)
        for row in rows_by_channel.values():
            db.execute(text("""
              INSERT INTO consents(profile_id,channel,status,source_id,captured_at,evidence)
              VALUES(:p,:channel,:status,:source_id,:captured_at,CAST(:evidence AS jsonb))
            """), {**row, "p": target[label], "evidence": json.dumps(row["evidence"])})
    # Enrichment values are recomputed from each side's evidence, not an
    # overwritten merged value. Do not resurrect license-expired rows.
    db.execute(text("DELETE FROM enrichment_values WHERE profile_id=ANY(:ids)"), {"ids": [w, new.id]})
    for label, side in sides.items():
        for row in side["enrichment_values"]:
            row = {**row, "profile_id": target[label]}
            row.pop("id", None)
            columns = ",".join(row)
            db.execute(text(f"""
              INSERT INTO enrichment_values({columns})
              SELECT {columns} FROM jsonb_populate_record(NULL::enrichment_values,CAST(:r AS jsonb))
              WHERE license_expires_at IS NULL OR license_expires_at>now()
            """), {"r": json.dumps(row)})
    survivor = next(r["master_id"] for r in rows if r["id"] == w)
    db.execute(text("DELETE FROM identity_xref WHERE master_id=:m"), {"m": survivor})
    for label, side in sides.items():
        master = survivor if target[label] == w else new.master_id
        for row in side["xref"]:
            # Old journals may contain the retired source-record bookkeeping key.
            if row["key_type"] == "external":
                continue
            db.execute(text("""
              INSERT INTO identity_xref(master_id,key_type,key_value,source_id,first_seen_at,last_seen_at,status,
                method,confidence,rule)
              VALUES(:m,:key_type,:key_value,:source_id,:first_seen_at,:last_seen_at,:status,
                :method,:confidence,:rule)
              ON CONFLICT(master_id,key_type,key_value,source_id) DO NOTHING
            """), {"method": "deterministic", "confidence": 1,
                   "rule": "existing_deterministic_resolver", **row, "m": master})
    # Retired IDs remain retired. Alias still points at original survivor.
    statements = """
      UPDATE identity_merge_journal SET undone_at=clock_timestamp(),undone_by=:a,new_profile_id=:n
      WHERE merge_id=:m;
      DELETE FROM profile_traits WHERE profile_id=ANY(:ids);
      DELETE FROM segment_membership WHERE profile_id=ANY(:ids);
      INSERT INTO trait_dirty_profiles(profile_id,dirtied_at)
      SELECT unnest(CAST(:ids AS bigint[])),clock_timestamp()
      ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at
    """
    for statement in statements.split(";"):
        if statement.strip():
            db.execute(text(statement), {"a": actor, "n": new.id, "m": merge_id, "ids": [w, new.id]})
    recompute_profiles_fields(db, {w, new.id})
    from app.identity.devices import rehome_devices
    from app.identity.addresses import refresh_addresses, refresh_households
    rehome_devices(db)
    # Drop stale parsed addresses for split sides; canonical source fields were
    # just recomputed. Daily maintenance will parse the current addresses.
    db.execute(text("DELETE FROM identity_addresses WHERE profile_id=ANY(:ids)"),
               {"ids": [w, new.id]})
    refresh_addresses(db, [w, new.id])
    refresh_households(db)
    audit(db, actor, "identity.unmerge", "identity_merge", merge_id, {"sides": 2})
    return {"merge_id": merge_id, "retained_master_id": survivor,
            "new_master_id": new.master_id, "retained_side": retained,
            "new_profile_id": new.id, "retained_profile_id": w}
