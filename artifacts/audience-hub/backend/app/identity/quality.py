"""Admin review decisions use the same locks and reversible merge as batch jobs."""
from sqlalchemy import text

from app.identity.locking import acquire_resolver_lock
from app.identity.matching import link
from app.identity.master import unmerge
from app.identity.addresses import refresh_addresses, refresh_households
from app.jobs.queue import enqueue
from app.privacy.service import audit


def sample(db, count):
    # Include deterministic linked source-record pairs, not only the new
    # probabilistic matches. Hash order avoids a costly random() full sort.
    selected = len(db.execute(text("""UPDATE identity_pairs SET sampled=true WHERE id IN (
      SELECT id FROM identity_pairs WHERE status='linked' AND verdict IS NULL AND NOT sampled
      ORDER BY md5(id::text || current_date::text) LIMIT :n) RETURNING id"""), {"n": count}).all())
    remaining = max(0, count - selected)
    if remaining:
        selected += len(db.execute(text("""
          INSERT INTO identity_pairs(left_id,right_id,left_record_id,right_record_id,
            confidence,rule,status,sampled)
          SELECT p.id,p.id,s.first,s.last,1,'deterministic_shared_master','linked',true
          FROM active_profiles p JOIN LATERAL(
            SELECT min(id) AS first,max(id) AS last FROM source_records WHERE profile_id=p.id
          ) s ON s.first<>s.last
          WHERE NOT EXISTS(SELECT 1 FROM identity_pairs q WHERE q.left_id=p.id AND q.right_id=p.id)
          ORDER BY md5(p.id::text || current_date::text) LIMIT :n RETURNING id
        """), {"n": remaining}).all())
    return selected


def verdict(db, identifier, decision, note, actor):
    acquire_resolver_lock(db)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('audience-hub:trait-recompute'))"))
    row = db.execute(text("SELECT * FROM identity_pairs WHERE id=:id FOR UPDATE"),
                     {"id": identifier}).mappings().first()
    if not row:
        raise LookupError("Identity pair not found")
    if row["verdict"] not in (None, "unsure"):
        raise ValueError("This pair has already been reviewed")
    left, right = row["left_id"], row["right_id"]
    # Source membership is captured before a merge/unmerge changes ownership.
    sources = db.scalars(text("SELECT DISTINCT source_id FROM source_records WHERE profile_id=ANY(:ids)"),
                         {"ids": [left, right]}).all()
    merge_id = row["merge_id"]
    status = row["status"]
    if decision == "match" and status in ("review", "conflict"):
        merge_id = link(db, left, right, row["confidence"], row["rule"] + ":admin_approved")
        status = "linked"
    elif decision == "not_match" and status == "linked" and merge_id:
        unmerge(db, merge_id, actor)
        status = "rejected"
    elif decision == "not_match" and status in ("review", "conflict"):
        status = "rejected"
    db.execute(text("""
      UPDATE identity_pairs SET verdict=:v,note=:note,reviewed_by=:actor,reviewed_at=now(),
        updated_at=now(),status=:status,merge_id=:merge WHERE id=:id
    """), {"id": identifier, "v": decision, "note": note, "actor": actor,
           "status": status, "merge": merge_id})
    # Precision is based on sampled *existing links*, not on approvals in the
    # review band (those would artificially inflate the measured precision).
    if decision != "unsure" and row["sampled"] and row["status"] == "linked":
        for source in sources:
            db.execute(text("""
              INSERT INTO identity_graph_daily(day,source_id,reviewed,correct) VALUES(current_date,:s,1,:correct)
              ON CONFLICT(day,source_id) DO UPDATE SET reviewed=identity_graph_daily.reviewed+1,
                correct=identity_graph_daily.correct+EXCLUDED.correct
            """), {"s": source, "correct": int(decision == "match")})
    refresh_addresses(db)
    refresh_households(db)
    refresh_queue_trends(db)
    enqueue(db, "traits.recompute", {"mode": "dirty"}, dedupe_key="traits:incremental")
    audit(db, actor, "identity.review", "identity_pair", identifier,
          {"verdict": decision, "status": status})
    return {"id": identifier, "verdict": decision, "status": status, "merge_id": merge_id}


def refresh_queue_trends(db):
    db.execute(text("""
      WITH open_pairs AS (
        SELECT id,left_id AS profile_id FROM identity_pairs WHERE status IN ('review','conflict')
          AND (verdict IS NULL OR verdict='unsure')
        UNION SELECT id,right_id FROM identity_pairs WHERE status IN ('review','conflict')
          AND (verdict IS NULL OR verdict='unsure')
      ), totals AS (
        SELECT sr.source_id,count(DISTINCT q.id) AS n FROM open_pairs q
        JOIN source_records sr ON sr.profile_id=q.profile_id GROUP BY sr.source_id
      )
      INSERT INTO identity_graph_daily(day,source_id,queue_open)
      SELECT current_date,s.id,coalesce(t.n,0) FROM sources s LEFT JOIN totals t ON t.source_id=s.id
      ON CONFLICT(day,source_id) DO UPDATE SET queue_open=EXCLUDED.queue_open
    """))
