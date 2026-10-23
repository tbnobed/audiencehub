"""Set-based conflict refinement for bulk and bounded identity resolution."""
from sqlalchemy import text


def constrain_components(db):
    """Remove household contact bridges before selecting any survivor.

    Rebuild components after pruning (rather than choosing an arbitrary spouse).
    Contradictory system-key bridges are quarantined for review, not merged.
    """
    def sql(query):
        return db.execute(text(query))

    def conflicts():
        sql("""
          SET LOCAL work_mem='64MB';
          DROP TABLE IF EXISTS pg_temp.ir_conflicts;
          CREATE TEMP TABLE ir_conflicts ON COMMIT DROP AS
          SELECT label FROM (
            SELECT l.label,e.kind,e.value FROM ir_labels l JOIN ir_keys e ON e.record_id=l.id
            UNION ALL SELECT l.label,x.key_type,x.key_value FROM ir_labels l
            JOIN ir_edges e ON e.record_id=l.id AND e.kind='profile'
            JOIN profiles p ON p.id=e.value::bigint JOIN identity_xref x ON x.master_id=p.master_id
          ) keys WHERE kind IN ('donor_account_number','okta_customer_id','fusionauth_user_id','zeta_cdp_id','zeta_uid')
          GROUP BY label,kind HAVING count(DISTINCT value)>1;
          INSERT INTO identity_reviews(source_record_id,reason)
          SELECT l.id,'system_key_conflict' FROM ir_labels l JOIN ir_conflicts c USING(label)
          ON CONFLICT DO NOTHING
        """)

    conflicts()
    if not sql("SELECT EXISTS(SELECT 1 FROM ir_conflicts)").scalar():
        return 0
    sql("""
      DELETE FROM ir_edges e USING ir_labels l,ir_conflicts c
      WHERE e.record_id=l.id AND l.label=c.label
        AND e.kind IN ('email','phone','email_md5','email_sha256','profile');
      INSERT INTO ir_edges
      SELECT e.record_id,'profile',p.id::text FROM ir_edges e
      JOIN identity_xref x ON x.key_type=e.kind AND x.key_value=e.value
      JOIN profiles p ON p.master_id=x.master_id WHERE p.merged_into_id IS NULL AND NOT p.is_deleted
      UNION SELECT e.record_id,'profile',i.profile_id::text FROM ir_edges e
      JOIN identifiers i ON i.type=e.kind AND i.value=e.value
      WHERE NOT EXISTS(SELECT 1 FROM identity_xref x WHERE x.key_type=e.kind AND x.key_value=e.value)
      UNION SELECT id,'profile',profile_id::text FROM ir_pending WHERE profile_id IS NOT NULL
      ON CONFLICT DO NOTHING;
      UPDATE ir_labels SET label=id
    """)
    while True:
        sql("""
          TRUNCATE ir_key_labels;
          INSERT INTO ir_key_labels SELECT e.kind,e.value,min(l.label)
          FROM ir_edges e JOIN ir_labels l ON l.id=e.record_id GROUP BY e.kind,e.value
        """)
        changed = sql("""
          UPDATE ir_labels l SET label=n.label FROM (
            SELECT e.record_id,min(k.label) AS label FROM ir_edges e
            JOIN ir_key_labels k USING(kind,value) GROUP BY e.record_id
          ) n WHERE l.id=n.record_id AND l.label>n.label
        """).rowcount
        if not changed:
            break
    conflicts()
    # This second conflict cannot be explained by a shared contact. Do not
    # duplicate system keys or move existing owners. Preserve raw source for review.
    result = sql("""
      UPDATE source_records s SET resolved_at=now() FROM ir_labels l,ir_conflicts c
      WHERE s.id=l.id AND l.label=c.label;
      DELETE FROM ir_pending p USING ir_labels l,ir_conflicts c WHERE p.id=l.id AND l.label=c.label;
      DELETE FROM ir_edges e USING ir_labels l,ir_conflicts c WHERE e.record_id=l.id AND l.label=c.label;
      DELETE FROM ir_keys e USING ir_labels l,ir_conflicts c WHERE e.record_id=l.id AND l.label=c.label;
      DELETE FROM ir_labels l USING ir_conflicts c WHERE l.label=c.label
    """)
    return result.rowcount
