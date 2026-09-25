"""Actionable issues, not the size of the unresolved identity backlog."""
from sqlalchemy import text


def health_counts(db, role="admin"):
    from app.dashboards.cache import check_deadline
    check_deadline(db)
    # Count report objects, not rejected rows. A changed rejection count reopens
    # a report; EXISTS avoids multiplying issues when reviews are repeated.
    counts = dict(db.execute(text("""SELECT
      (SELECT count(*) FROM jobs WHERE status='failed') AS failed_jobs,
      (SELECT count(*) FROM imports i WHERE i.rows_rejected>0
        AND i.status IN ('completed','failed')
        AND NOT EXISTS (SELECT 1 FROM audit_log a
          WHERE a.action='data_health.rejected_report.review'
          AND a.entity_id=i.id::text
          AND a.details->>'rows_rejected'=i.rows_rejected::text))
        AS rejected_reports,
      (SELECT count(*) FROM identifier_blocklist b WHERE reason='high_cardinality'
        AND NOT EXISTS (SELECT 1 FROM audit_log a
          WHERE a.action='data_health.blocklist.approve'
          AND a.details->>'blocklist_id'=b.id::text)) AS blocklist_reviews,
      (SELECT count(*) FROM enrichment_values
        WHERE license_expires_at >= CURRENT_DATE
          AND license_expires_at <= CURRENT_DATE + 60) AS expiring_enrichment
    """)).mappings().one())
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
    )
    items = [{"severity": severity, "title": title,
              "explanation": f"{counts[key]:,} {explanation}", "href": href}
             for key, severity, title, explanation, href in definitions if counts[key]]
    if lapsing:
        items.append({"severity": "warning", "title": "Partners are lapsing",
                      "explanation": f"{lapsing:,} partners last gave 366–730 days ago.",
                      "href": "/segments"})
    return items