"""Actionable issues, not the size of the unresolved identity backlog."""
from sqlalchemy import text


PRIVACY_REVIEWS = """
    SELECT a.id, a.entity_id AS segment_id, a.at AS created_at,
           a.details->>'definition_changed' AS definition_changed
    FROM audit_log a
    WHERE a.action='privacy.segment.review_required'
      AND NOT EXISTS (SELECT 1 FROM audit_log reviewed
        WHERE reviewed.action='data_health.privacy_segment.review'
          AND reviewed.entity_id=a.id::text)
"""

REJECTED_REPORTS = """
    SELECT i.id AS import_id,i.created_at::date AS day,s.key AS source,
           i.filename,i.record_type,i.rows_rejected AS rejected
    FROM imports i JOIN sources s ON s.id=i.source_id
    WHERE i.rows_rejected>0 AND i.status IN ('completed','failed')
      AND NOT EXISTS (SELECT 1 FROM audit_log a
        WHERE a.action='data_health.rejected_report.review'
          AND a.entity_id=i.id::text
          AND a.details->>'rows_rejected'=i.rows_rejected::text)
"""


def rejected_reports(db):
    """Current actionable reports, independent of the dashboard's gift date range."""
    return [dict(r) for r in db.execute(text(
        REJECTED_REPORTS + " ORDER BY i.created_at DESC,i.id DESC")).mappings()]


def health_counts(db, role="admin"):
    from app.dashboards.cache import check_deadline
    check_deadline(db)
    # Count report objects, not rejected rows. A changed rejection count reopens
    # a report; EXISTS avoids multiplying issues when reviews are repeated.
    counts = dict(db.execute(text("""SELECT
      (SELECT count(*) FROM jobs WHERE status='failed') AS failed_jobs,
      (SELECT count(*) FROM identifier_blocklist b WHERE reason='high_cardinality'
        AND NOT EXISTS (SELECT 1 FROM audit_log a
          WHERE a.action='data_health.blocklist.approve'
          AND a.details->>'blocklist_id'=b.id::text)) AS blocklist_reviews,
      (SELECT count(*) FROM enrichment_values
        WHERE license_expires_at >= CURRENT_DATE
          AND license_expires_at <= CURRENT_DATE + 60) AS expiring_enrichment
    """)).mappings().one())
    counts["rejected_reports"] = db.scalar(text(
        "SELECT count(*) FROM (" + REJECTED_REPORTS + ") reports"))
    counts["privacy_segment_reviews"] = db.scalar(text(
        "SELECT count(*) FROM (" + PRIVACY_REVIEWS + ") issues"))
    if role != "admin":
        counts["failed_jobs"] = 0
        counts["blocklist_reviews"] = 0
    if role == "viewer":
        counts["rejected_reports"] = 0
    return counts


def attention(db, role, lapsing):
    counts = health_counts(db, role)
    definitions = (
        ("failed_jobs", "error", "Failed background jobs", "jobs need investigation.", "/system"),
        ("rejected_reports", "warning", "Rejected-row reports need review",
         "import reports await review.", "/imports"),
        ("blocklist_reviews", "warning", "Identifiers need review",
         "auto-blocklisted identifiers await approval.", "/data-health"),
        ("expiring_enrichment", "warning", "Enrichment licenses expiring",
         "enrichment values expire within 60 days.", "/data-health"),
        ("privacy_segment_reviews", "warning", "Audience privacy changes need review",
         "audience changes await review; audiences and activations remain running.", "/data-health"),
    )
    items = [{"severity": severity, "title": title,
              "explanation": f"{counts[key]:,} {explanation}", "href": href}
             for key, severity, title, explanation, href in definitions if counts[key]]
    if lapsing:
        items.append({"severity": "warning", "title": "Partners are lapsing",
                       "explanation": f"{lapsing:,} partners are lapsing as of today (current profile traits).",
                      "href": "/segments"})
    return items