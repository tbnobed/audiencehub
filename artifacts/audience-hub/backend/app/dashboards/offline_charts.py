"""Expensive diagnostic charts, evaluated exclusively by the traits refresh."""
from sqlalchemy import text


def static_charts(db):
    queries = {
        "profiles_by_source": """
          SELECT s.key AS source,count(DISTINCT sr.profile_id) AS profiles
          FROM source_records sr JOIN sources s ON s.id=sr.source_id
          JOIN active_profiles p ON p.id=sr.profile_id GROUP BY s.key ORDER BY profiles DESC,source""",
        "overlap_matrix": """
          WITH m AS (SELECT DISTINCT sr.profile_id,s.key AS source FROM source_records sr
            JOIN sources s ON s.id=sr.source_id JOIN active_profiles p ON p.id=sr.profile_id)
          SELECT a.source AS source_a,b.source AS source_b,count(*) AS profiles
          FROM m a JOIN m b USING(profile_id) WHERE a.source<=b.source
          GROUP BY 1,2 ORDER BY 1,2""",
        "identifier_coverage": """
          SELECT s.key AS source,count(DISTINCT sr.profile_id) AS profiles,
            count(DISTINCT sr.profile_id) FILTER(WHERE COALESCE(sr.email_norm::text,'')<>'') AS email_profiles,
            count(DISTINCT sr.profile_id) FILTER(WHERE COALESCE(sr.phone_e164,'')<>'') AS phone_profiles,
            count(DISTINCT sr.profile_id) FILTER(WHERE COALESCE(sr.address1,sr.address2,sr.city,sr.postal_code,'')<>'') AS address_profiles
          FROM source_records sr JOIN sources s ON s.id=sr.source_id
          JOIN active_profiles p ON p.id=sr.profile_id GROUP BY s.key ORDER BY s.key""",
        "pending_resolutions": """
          SELECT s.key AS source,count(*) AS pending FROM source_records sr
          JOIN sources s ON s.id=sr.source_id WHERE sr.resolved_at IS NULL GROUP BY s.key ORDER BY s.key""",
        "blocklist_hits": """
          SELECT b.type,count(DISTINCT sr.id) AS hits FROM identifier_blocklist b
          JOIN source_records sr ON
            (b.type='email' AND lower(sr.email_norm::text) LIKE lower(replace(b.value,'*','%')))
            OR (b.type='phone' AND sr.phone_e164=b.value) GROUP BY b.type ORDER BY b.type""",
        "retention_by_year": """
          WITH y AS (SELECT DISTINCT profile_id,extract(year FROM gift_date)::int AS year
            FROM gifts WHERE profile_id IS NOT NULL AND gift_date IS NOT NULL)
          SELECT a.year,count(*) FILTER(WHERE b.profile_id IS NOT NULL)::int AS retained,
            count(*)::int AS donors,
            100.0*count(*) FILTER(WHERE b.profile_id IS NOT NULL)/count(*) AS retention_rate
          FROM y a LEFT JOIN y b ON b.profile_id=a.profile_id AND b.year=a.year+1
          GROUP BY a.year ORDER BY a.year""",
        "cohorts": """
          WITH y AS (SELECT DISTINCT profile_id,extract(year FROM gift_date)::int AS year
            FROM gifts WHERE profile_id IS NOT NULL AND gift_date IS NOT NULL),
          f AS (SELECT profile_id,min(year) AS first_year FROM y GROUP BY profile_id),
          n AS (SELECT first_year,count(*) AS size FROM f GROUP BY first_year)
          SELECT first_year,y.year-first_year AS years_after_first,count(*)::int AS retained,
            100.0*count(*)/n.size AS retention_pct
          FROM f JOIN y USING(profile_id) JOIN n USING(first_year)
          GROUP BY first_year,y.year,n.size ORDER BY first_year,y.year""",
    }
    result = {key: [dict(r) for r in db.execute(text(sql)).mappings()]
              for key, sql in queries.items()}
    for row in result["identifier_coverage"]:
        for field in ("email", "phone", "address"):
            row[field+"_pct"] = 100*row[field+"_profiles"]/row["profiles"] if row["profiles"] else 0
    # JSON must contain numbers rather than decimal strings.
    from decimal import Decimal
    return {key: [{k: float(v) if isinstance(v, Decimal) else v for k, v in r.items()}
                  for r in rows] for key, rows in result.items()}