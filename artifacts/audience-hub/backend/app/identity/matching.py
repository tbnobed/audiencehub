"""Versioned Splink/Postgres model. No training or resolver-rule changes at startup."""
MODEL = "kinship_splink_fellegi_sunter_v1"

# Explicit, inspectable priors, not a claim of production-calibrated accuracy.
# Quality review should precede enabling auto-link on a new population.
NICKNAMES = {
    "bob": "robert", "bobby": "robert", "rob": "robert", "robbie": "robert",
    "bill": "william", "billy": "william", "will": "william",
    "jim": "james", "jimmy": "james", "liz": "elizabeth", "beth": "elizabeth",
    "betty": "elizabeth", "lizzy": "elizabeth", "kate": "katherine",
    "kathy": "katherine", "cathy": "katherine", "catherine": "katherine",
    "mike": "michael", "mikey": "michael", "joe": "joseph", "joey": "joseph",
    "tom": "thomas", "tommy": "thomas", "dave": "david", "davy": "david",
    "dick": "richard", "rick": "richard", "rich": "richard", "ricky": "richard",
    "dan": "daniel", "danny": "daniel", "chris": "christopher",
    "steve": "steven", "stephen": "steven", "pat": "patrick",
    "tony": "anthony", "andy": "andrew", "sue": "susan", "susie": "susan",
    "jen": "jennifer", "jenny": "jennifer", "peggy": "margaret", "meg": "margaret",
}


def settings():
    def level(condition, label, m, u):
        return dict(sql_condition=condition, label_for_charts=label,
                    m_probability=m, u_probability=u)

    def compare(name, levels):
        return {"output_column_name": name, "comparison_levels": [
            {"sql_condition": f"{name}_l IS NULL OR {name}_r IS NULL", "is_null_level": True},
            *levels]}

    def edits(name, distance):
        # fuzzystrmatch rejects strings longer than 255 characters. Treat long
        # values as non-fuzzy matches, without truncating distinct names.
        return (f"CASE WHEN length({name}_l)<=255 AND length({name}_r)<=255 "
                f"THEN levenshtein({name}_l,{name}_r)<={distance} ELSE false END")

    return {
        "link_type": "dedupe_only", "unique_id_column_name": "unique_id",
        "probability_two_random_records_match": .000001,
        "retain_matching_columns": True, "retain_intermediate_calculation_columns": True,
        "blocking_rules_to_generate_predictions": [
            "l.address_key=r.address_key",
            "l.phone=r.phone",
            "l.last_name=r.last_name AND l.zip5=r.zip5",
        ],
        "comparisons": [
            compare("first_name", [
                level("first_name_l=first_name_r", "Exact", .80, .005),
                level("nickname_l=nickname_r", "Nickname", .15, .01),
                level(edits("first_name", 1), "One edit", .04, .02),
                level("ELSE", "Different", .01, .965)]),
            compare("last_name", [
                level("last_name_l=last_name_r", "Exact", .92, .003),
                level(edits("last_name", 1), "One edit", .06, .015),
                level("ELSE", "Different", .02, .982)]),
            compare("address", [
                level("address_l=address_r", "Exact", .85, .0001),
                level(edits("address", 3), "Three edits", .10, .005),
                level("ELSE", "Different", .05, .9949)]),
            compare("dob", [
                level("dob_l=dob_r", "Exact", .97, .0001),
                level("ELSE", "Different", .03, .9999)]),
            compare("phone", [
                level("phone_l=phone_r", "Exact", .97, .00001),
                level("ELSE", "Different", .03, .99999)]),
        ],
    }


def conflict(db, left, right):
    """Any differing system key blocks an automatic OR manually approved merge."""
    from sqlalchemy import text
    from app.identity.master import SYSTEM_KEYS
    return bool(db.scalar(text("""
      SELECT EXISTS(SELECT 1 FROM identity_xref x JOIN profiles p ON p.master_id=x.master_id
        WHERE p.id=ANY(:ids) AND x.key_type=ANY(:keys)
        GROUP BY x.key_type HAVING count(DISTINCT x.key_value)>1)
    """), {"ids": [left, right], "keys": list(SYSTEM_KEYS)}))


def link(db, left, right, confidence, rule, job_id=None):
    """Use the existing Master ID winner ordering and full pre-move merge journal."""
    from sqlalchemy import text
    from app.identity.master import journal_merge
    from app.identity.resolver import _move_profile_references
    from app.identity.survivorship import recompute_profiles_fields
    rows = db.execute(text("""
      SELECT p.id,p.master_id FROM profiles p JOIN master_id_registry r ON r.master_id=p.master_id
      WHERE p.id=ANY(:ids) AND NOT p.is_deleted AND p.merged_into_id IS NULL
      ORDER BY left(p.master_id,14),r.issued_at,p.id FOR UPDATE OF p
    """), {"ids": [left, right]}).all()
    if len(rows) != 2:
        raise ValueError("Pair changed since scoring; rerun the identity batch")
    if conflict(db, left, right):
        raise ValueError("Conflicting system keys; this pair cannot be linked")
    winner, loser = rows[0][0], rows[1][0]
    # journal_merge snapshots both sides *before* probabilistic annotations.
    merge_id = journal_merge(db, winner, loser, job_id)
    _move_profile_references(db, winner, loser)
    db.execute(text("""UPDATE profiles SET merged_into_id=:w,household_id=NULL,household_size=1
      WHERE id=:l"""), {"w": winner, "l": loser})
    db.execute(text("""UPDATE profile_merges SET reason=jsonb_build_object(
      'type','probabilistic','model',CAST(:rule AS text),'confidence',CAST(:confidence AS float8))
      WHERE id=:id"""), {"id": merge_id, "rule": rule, "confidence": confidence})
    # Existing deterministic edges on the survivor stay deterministic. Only
    # edges introduced from the other side describe the probabilistic bridge.
    db.execute(text("""
      UPDATE identity_xref x SET method='probabilistic',confidence=:score,rule=:rule
      FROM identity_merge_journal j
      WHERE j.merge_id=:merge AND x.master_id=:master
        AND EXISTS(SELECT 1 FROM jsonb_array_elements(j.sides->'loser'->'xref') r
          WHERE r->>'key_type'=x.key_type AND r->>'key_value'=x.key_value
          AND (r->>'source_id')::bigint IS NOT DISTINCT FROM x.source_id)
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(j.sides->'winner'->'xref') r
          WHERE r->>'key_type'=x.key_type AND r->>'key_value'=x.key_value
          AND (r->>'source_id')::bigint IS NOT DISTINCT FROM x.source_id)
    """), {"score": confidence, "rule": rule, "merge": merge_id, "master": rows[0][1]})
    # Device links return to their original observed people on unmerge via
    # okta_customer_id. Household membership is recomputed, never merged.
    db.execute(text("""INSERT INTO trait_dirty_profiles(profile_id,dirtied_at) VALUES(:w,now())
      ON CONFLICT(profile_id) DO UPDATE SET dirtied_at=EXCLUDED.dirtied_at"""), {"w": winner})
    recompute_profiles_fields(db, {winner})
    from app.identity.devices import rehome_devices
    rehome_devices(db)
    return merge_id
