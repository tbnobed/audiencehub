"""Independent disk-backed CSV/SQL audit oracle, not segment templates.

Run: python -m app.seed_audit_report /path/to/seed-data
The query predicates are intentionally independent of cohort generation.
"""
import csv
import json
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path


def measure_audit(directory):
    directory = Path(directory)
    metadata = json.loads((directory / "generator_stats.json").read_text())
    audit_files = metadata.get("source_file_mapping")
    split_activity = audit_files is not None and "people_feed_events.csv" in audit_files
    if audit_files is None:
        inputs = (("c", "donor_crm_contacts.csv"), ("g", "giving_platform_gifts.csv"),
                  ("e", "five9_calls.csv"), ("z", "zeta_enrichment.csv"))
        gift_event_key = "e.external_id"
        event_gift_join = "e.external_id=g.external_id"
    else:
        expected = {
            "donor_direct.csv": ("donor_direct", "contact"),
            "people_feed.csv": ("people_feed", "contact"),
            "people_feed_events.csv": ("people_feed", "event"),
            "hubspot.csv": ("hubspot", "contact"),
            "okta_tbn_plus.csv": ("okta_tbn_plus", "contact"),
            "donation_events_gifts.csv": ("donation_events", "gift"),
            "donation_events.csv": ("donation_events", "event"),
            "vod_viewed_events.csv": ("vod_viewed_events", "event"),
            "model_scores.csv": ("model_scores", "enrichment"),
        }
        expected_mapping = {name: {"source": source, "record_type": kind}
                            for name, (source, kind) in expected.items()}
        previous_mapping = {name: value for name, value in expected_mapping.items()
                            if name != "people_feed_events.csv"}
        if (audit_files not in (expected_mapping, previous_mapping)
                or metadata.get("reference_source") != "donor_direct"):
            raise ValueError("Unsupported audit source-file/reference contract")
        inputs = (("c", "donor_direct.csv"), ("g", "donation_events_gifts.csv"),
                  ("e", "donation_events.csv"), ("e", "vod_viewed_events.csv"),
                  ("z", "model_scores.csv"))
        if audit_files == expected_mapping:
            inputs += (("e", "people_feed_events.csv"),)
        gift_event_key = "'gift:' || e.external_id"
        # Keep the reverse lookup indexed on the unmodified event key.
        event_gift_join = "e.external_id=substr(g.external_id,6) AND substr(g.external_id,1,5)='gift:'"
    reference = date.fromisoformat(metadata["as_of"])
    catalog = json.loads((Path(__file__).resolve().parents[2] /
                          "docs/ZETA_AUDIENCE_TARGETS.json").read_text())
    with tempfile.TemporaryDirectory(prefix="seed-oracle-") as temp:
        db = sqlite3.connect(str(Path(temp) / "oracle.sqlite"))
        try:
            table_columns = {}
            event_feed_violations = 0
            for table, filename in inputs:
                with (directory / filename).open(newline="") as handle:
                    reader = csv.DictReader(handle)
                    columns = reader.fieldnames
                    if table not in table_columns:
                        db.execute(f"CREATE TABLE {table} ({','.join(chr(34)+c+chr(34)+' TEXT' for c in columns)})")
                        table_columns[table] = columns
                    elif columns != table_columns[table]:
                        raise ValueError(f"Incompatible split event columns in {filename}")
                    def file_values():
                        nonlocal event_feed_violations
                        for row in reader:
                            if split_activity and table == "e":
                                event_name = row.get("name")
                                expected_file = (
                                    "donation_events.csv" if event_name in ("donation", "donation_return")
                                    else "vod_viewed_events.csv" if event_name == "vod_viewed"
                                    else "people_feed_events.csv"
                                )
                                event_feed_violations += filename != expected_file
                            yield tuple(row[c] for c in columns)
                    db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})",
                                   file_values())
            db.commit()
            def count(sql, params=()):
                return db.execute(sql, params).fetchone()[0]
            total = count("SELECT count(*) FROM c")
            predicates = {
                "All TBN Reference": "1",
                "Has Active Email": "has_active_email='true'",
                "Active (120 days)": f"substr(last_seen,1,10)>='{reference-timedelta(days=120)}'",
                "TBN Plus Subscription": "tbn_plus_subscription<>''",
                "TBN Plus Free Users": "tbn_plus_subscription='basic'",
                "TBN Plus Premium": "tbn_plus_subscription LIKE 'premium_%'",
                "Premium Upgraders": "tbn_plus_subscription_update LIKE 'Upgrade%'",
                "TBN App Non-Donors": "tbn_plus_subscription<>'' AND donor_account_number='' AND external_id NOT IN "
                f"(SELECT contact_external_id FROM g WHERE is_refund='false' AND gift_date>='{reference-timedelta(days=720)}')",
                "TBN App Donors": "tbn_plus_subscription<>'' AND external_id IN "
                f"(SELECT contact_external_id FROM g WHERE is_refund='false' AND gift_date>='{reference-timedelta(days=720)}')",
                "Basic Donors": "tbn_plus_subscription='basic' AND donor_account_number<>'' AND CAST(lifetime_amount AS REAL)>0",
                "Basic Recurring Donors": "tbn_plus_subscription='basic' AND has_recurring_donation='true'",
                "Basic One-Time Donors": "tbn_plus_subscription='basic' AND CAST(rfm_raw_frequency AS REAL)=1",
                "RFM TBN All Donor Reference": "rfm_date<>''",
                "RFM Lapsed Donors": "rfm_date<>'' AND CAST(rfm_recency_score AS REAL)=0",
                "Partner Names Non-Donors": "donor_account_number<>'' AND rfm_date=''",
                "Living Legacy Prospects V2": "CAST(donation_likelihood_prob AS REAL)>.75 AND "
                "CAST(wealth_score AS REAL)>4 AND CAST(amt_suggested AS REAL)>500 AND external_id IN "
                f"(SELECT contact_external_id FROM g WHERE is_refund='false' AND gift_date>='{metadata['start_date']}')",
                "Mobile Subscribed": "mobile_mktg_optin_tbn='true' AND phone<>''",
                "Call Center SMS Opt-ins": "tbn_call_center_sms_optin='true' AND phone<>''",
                "SMS Opt-Out Date Exists": "mobile_mktg_optout_date_tbn<>''",
                "VOD Optouts": "tbn_video_on_demand_updates_opt_status='false'",
            }
            observed = {name: count(f"SELECT count(*) FROM c WHERE {where}")
                        for name, where in predicates.items()}
            observed["Donors Past 30 Days"] = count(
                "SELECT count(DISTINCT contact_external_id) FROM g WHERE is_refund='false' AND gift_date>=? AND gift_date<?",
                (str(reference-timedelta(days=30)), str(reference)))
            observed["Giving Tuesday 2024"] = count(
                "SELECT count(DISTINCT contact_external_id) FROM g WHERE gift_date='2024-12-03' AND "
                "source_code IN ('T4XWDGT','T4XPCGT','T4YWDGT','T4YPCGT') AND is_refund='false'")
            observed["Customers with VOD Viewed"] = count(
                "SELECT count(DISTINCT contact_external_id) FROM e WHERE name='vod_viewed'")
            rows = []
            for name, full in catalog["audiences"].items():
                expected = total * full / catalog["denominator"]
                applicable = name != "Giving Tuesday 2024" or metadata["giving_tuesday_in_window"]
                tolerance = max(25, expected * .2)
                actual = observed[name]
                rows.append(dict(metric=name, actual=actual, target_full=full, expected=expected,
                                 proportion=actual/total, tolerance=tolerance, applicable=applicable,
                                 passed=abs(actual-expected)<=tolerance if applicable else actual == 0))
            additional = [
                ("CDP identity", "SELECT count(*) FROM c WHERE zeta_cdp_id<>''", .93),
                ("UID only identity", "SELECT count(*) FROM c WHERE zeta_uid<>'' AND zeta_cdp_id=''", .07),
                ("Donor accounts", "SELECT count(*) FROM c WHERE donor_account_number<>''", .301),
                ("Known to Zeta", "SELECT count(*) FROM c WHERE known_to_zeta='true'", .49),
                ("No active phone", "SELECT count(*) FROM c WHERE phone=''", .92),
                ("Synthetic enrichment coverage", "SELECT count(DISTINCT contact_external_id) FROM z", .49),
            ]
            for name, sql, share in additional:
                actual, expected = count(sql), total*share
                rows.append(dict(metric=name, actual=actual, target_full=share*catalog["denominator"],
                                 expected=expected, proportion=actual/total, tolerance=expected*.2,
                                 applicable=True, passed=abs(actual-expected)<=expected*.2))
            giver_count = observed["RFM TBN All Donor Reference"]
            for name, actual, share in (
                ("Imported RFM zero among givers", observed["RFM Lapsed Donors"], .91),
                ("Recent donors among givers", observed["Donors Past 30 Days"], .011),
            ):
                expected = giver_count*share
                rows.append(dict(metric=name, actual=actual, expected=expected,
                                 target_full=catalog["audiences"]["RFM TBN All Donor Reference"]*share,
                                 proportion=actual/giver_count if giver_count else 0,
                                 tolerance=expected*.2, applicable=True,
                                 passed=abs(actual-expected)<=expected*.2))
            recent_rows = count("SELECT count(*) FROM e WHERE name='donation' AND substr(occurred_at,1,10)>=? "
                                "AND substr(occurred_at,1,10)<?",
                                (str(reference-timedelta(days=30)), str(reference)))
            recent_people = observed["Donors Past 30 Days"]
            rows.append(dict(metric="30 day donation rows", actual=recent_rows, expected=1.4*recent_people,
                             target_full=1.4*catalog["audiences"]["Donors Past 30 Days"],
                             proportion=recent_rows/recent_people if recent_people else 0,
                             tolerance=max(1, .2*recent_people), applicable=True,
                             passed=abs(recent_rows-1.4*recent_people) <= max(1, .2*recent_people)))
            for name, rate in (("donation_return", 27), ("order_shipped", 171)):
                actual = count("SELECT count(*) FROM e WHERE name=? AND substr(occurred_at,1,10)>=? "
                               "AND substr(occurred_at,1,10)<?",
                               (name, str(reference-timedelta(days=30)), str(reference)))
                expected = total*rate*30/7830000
                rows.append(dict(metric=f"30 day {name} rows", actual=actual, expected=expected,
                                 target_full=rate*30, proportion=actual/total, tolerance=expected*.2,
                                 applicable=True, passed=abs(actual-expected)<=expected*.2))
            invalid_enrichment = count(
                "SELECT count(*) FROM z LEFT JOIN c ON c.external_id=z.contact_external_id "
                "WHERE c.known_to_zeta IS NULL OR c.known_to_zeta<>'true' OR "
                "z.vendor<>'kinship-synthetic-demo' OR z.license_id<>'synthetic-demo-only'")
            db.execute("CREATE INDEX contacts_external ON c(external_id)")
            db.execute("CREATE INDEX events_external ON e(external_id)")
            db.execute("CREATE INDEX gifts_external ON g(external_id)")
            orphan_rows = sum(count(
                f"SELECT count(*) FROM {table} LEFT JOIN c ON c.external_id={table}.contact_external_id "
                "WHERE c.external_id IS NULL") for table in ("g", "e", "z"))
            ledger_mismatches = count(
                f"SELECT count(*) FROM e LEFT JOIN g ON g.external_id={gift_event_key} "
                "WHERE e.name IN ('donation','donation_return') AND g.external_id IS NULL")
            ledger_mismatches += count(
                f"SELECT count(*) FROM g LEFT JOIN e ON {event_gift_join} "
                "WHERE e.external_id IS NULL")
            duplicate_gifts = count("SELECT count(*)-count(DISTINCT external_id) FROM g")
            invalid_refund_references = 0
            if split_activity:
                invalid_refund_references = count("""
                    SELECT count(*) FROM g r LEFT JOIN g original
                      ON original.external_id=r.original_donation_id
                    WHERE r.is_refund='true' AND (
                      original.external_id IS NULL OR original.is_refund<>'false'
                      OR original.contact_external_id<>r.contact_external_id
                      OR r.gift_date<original.gift_date
                      OR CAST(r.amount AS REAL)<=0
                      OR CAST(r.amount AS REAL)>CAST(original.amount AS REAL)
                    )
                """)
                invalid_refund_references += count("""
                    SELECT count(*) FROM (
                      SELECT r.original_donation_id FROM g r JOIN g original
                        ON original.external_id=r.original_donation_id
                      WHERE r.is_refund='true'
                      GROUP BY r.original_donation_id
                      HAVING sum(CAST(r.amount AS REAL))>max(CAST(original.amount AS REAL))+0.000001
                    )
                """)
            date_violations = count(
                "SELECT count(*) FROM g WHERE gift_date<? OR gift_date>=?",
                (metadata["start_date"], str(reference)))
            date_violations += count(
                "SELECT count(*) FROM e WHERE substr(occurred_at,1,10)<? OR substr(occurred_at,1,10)>=?",
                (metadata["start_date"], str(reference)))
            gift_count = count("SELECT count(*) FROM g WHERE is_refund='false'")
            middle = [r[0] for r in db.execute(
                "SELECT CAST(amount AS REAL) FROM g WHERE is_refund='false' "
                "ORDER BY CAST(amount AS REAL) LIMIT ? OFFSET ?",
                (2 if gift_count % 2 == 0 else 1, max(0, (gift_count-1)//2)))]
            summary = {"as_of": str(reference), "profiles": total, "metrics": rows,
                       "invalid_enrichment": invalid_enrichment,
                       "orphan_rows": orphan_rows, "ledger_mismatches": ledger_mismatches,
                       "duplicate_gifts": duplicate_gifts, "date_violations": date_violations,
                       "event_feed_violations": event_feed_violations,
                       "invalid_refund_references": invalid_refund_references,
                       "gift_median": sum(middle)/len(middle) if middle else None,
                       "gift_maximum": count("SELECT max(CAST(amount AS REAL)) FROM g WHERE is_refund='false'"),
                       "event_names": [r[0] for r in db.execute("SELECT DISTINCT name FROM e ORDER BY name")],
                       "passed": all(r["passed"] for r in rows) and not any((
                            invalid_enrichment, orphan_rows, ledger_mismatches, duplicate_gifts, date_violations,
                            event_feed_violations, invalid_refund_references))}
        finally:
            db.close()
    (directory/"audit_report.json").write_text(json.dumps(summary, indent=2)+"\n")
    with (directory/"audit_report.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    report = measure_audit(args.directory)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
