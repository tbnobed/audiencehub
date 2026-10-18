"""Streaming synthetic audit population. No live identities or vendor data."""
import csv
import hashlib
import json
import os
import random
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path

from app.canonical import FIELDS
from app.seed_audit_people import display_person
from app.seed_dates import seed_window
from app.seed_audit_giving import AuditGiving

DENOMINATOR = 7807899
TARGETS = {
    "All TBN Reference": 7807899, "Has Active Email": 4475258,
    "Active (120 days)": 2902186, "TBN Plus Subscription": 2086797,
    "TBN Plus Free Users": 2050476, "TBN Plus Premium": 36321,
    "Premium Upgraders": 8770, "TBN App Non-Donors": 1920005,
    "TBN App Donors": 16855, "Basic Donors": 72100,
    "Basic Recurring Donors": 4658, "Basic One-Time Donors": 11177,
    "RFM TBN All Donor Reference": 1759625, "RFM Lapsed Donors": 1608292,
    "Partner Names Non-Donors": 597638, "Donors Past 30 Days": 18943,
    "Living Legacy Prospects V2": 788, "Giving Tuesday 2024": 704,
    "Mobile Subscribed": 89201, "Call Center SMS Opt-ins": 149575,
    "SMS Opt-Out Date Exists": 39305, "VOD Optouts": 1819797,
    "Customers with VOD Viewed": 1414204,
}
GT = date(2024, 12, 3)
GT_CODES = ("T4XWDGT", "T4XPCGT", "T4YWDGT", "T4YPCGT")
AUDIT_FILES = {
    "donor_direct.csv": ("donor_direct", "contact"),
    "donation_events_gifts.csv": ("donation_events", "gift"),
    "people_feed.csv": ("people_feed", "contact"),
    "hubspot.csv": ("hubspot", "contact"),
    "okta_tbn_plus.csv": ("okta_tbn_plus", "contact"),
    "donation_events.csv": ("donation_events", "event"),
    "people_feed_events.csv": ("people_feed", "event"),
    "vod_viewed_events.csv": ("vod_viewed_events", "event"),
    "model_scores.csv": ("model_scores", "enrichment"),
}
RESOURCES = {
    "source_codes": [{"key": code, "name": "Giving Tuesday 2024",
                      "attributes": {"synthetic": True, "campaign_date": str(GT)}}
                     for code in GT_CODES] + [
                         {"key": "SYN-YE", "name": "Synthetic Year End"},
                         {"key": "SYN-GEN", "name": "Synthetic General Giving"}],
    "products": [{"key": "SYN-BOOK", "name": "Synthetic devotional book"}],
    "vod_titles": [{"key": "SYN-VOD", "name": "Synthetic ministry update",
                    "series": "Synthetic weekly reflections"}],
}


def generate_audit_seed(profiles, scale, random_seed, output_dir, as_of):
    if profiles < 1 or scale not in ("small", "medium", "large"):
        raise ValueError("Positive --profiles and small/medium/large --scale required")
    w = seed_window(as_of)
    dest = Path(output_dir or Path(os.environ.get("UPLOAD_DIR") or "/data/uploads") /
                "seed-data").expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    n = lambda count: round(profiles * count / DENOMINATOR)
    t = {key: n(value) for key, value in TARGETS.items()}
    g = t["RFM TBN All Donor Reference"]
    partner = t["Partner Names Non-Donors"]
    basic = t["Basic Donors"]
    premium = t["TBN Plus Premium"]
    nonapp = t["TBN App Non-Donors"]
    freepartner = t["TBN Plus Free Users"] - basic - nonapp
    recent = t["Donors Past 30 Days"]
    giving = AuditGiving(profiles, t, w, random_seed, GT, GT_CODES)
    rng = random.Random(random_seed)
    counts = dict.fromkeys(AUDIT_FILES, 0)
    headers = [
        ("external_id", "email", "phone", "first_name", "last_name", "zeta_cdp_id",
         "zeta_uid", "donor_account_number", "okta_customer_id", "hubspot_record_id",
         "email_sha256", *FIELDS),
        ("external_id", "contact_external_id", "amount", "currency", "gift_date",
         "fund", "campaign", "source_code", "channel", "is_recurring", "is_refund",
         "original_donation_id"),
        ("external_id", "contact_external_id", "email", "email_consent", "consent_captured_at",
         "zeta_cdp_id", "zeta_uid", "email_sha256", "hubspot_record_id"),
        ("external_id", "contact_external_id", "message_id", "type", "name",
         "occurred_at", "received_at", "properties", "context"),
        ("external_id", "contact_external_id", "hh_income_band", "age_band",
         "interests", "donor_propensity_score", "vendor", "license_id", "append_date", "sensitive"),
    ]
    # Reassign the accepted rows without adding/removing identity evidence.
    file_headers = {
        "donor_direct.csv": headers[0],
        "donation_events_gifts.csv": headers[1],
        "people_feed.csv": headers[2],
        "hubspot.csv": headers[2],
        "okta_tbn_plus.csv": headers[2],
        "donation_events.csv": headers[3],
        "people_feed_events.csv": headers[3],
        "vod_viewed_events.csv": headers[3],
        "model_scores.csv": headers[4],
    }
    with ExitStack() as stack:
        writers = {}
        for filename, cols in file_headers.items():
            # Identity fields can also enter the canonical catalog. Preserve
            # first occurrence/order and emit their single row value only once.
            cols = tuple(dict.fromkeys(cols))
            handle = stack.enter_context((dest / filename).open("w", newline="", encoding="utf-8"))
            writer = csv.DictWriter(handle, fieldnames=cols, lineterminator="\n")
            writer.writeheader()
            writers[filename] = writer

        def write(file, row):
            writers[file].writerow({k: str(v).lower() if isinstance(v, bool) else v
                                   for k, v in row.items()})
            counts[file] += 1

        def event(i, name, day, props=None, suffix=""):
            eid = f"{name}-{i}-{suffix}"
            stamp = f"{day}T12:00:00Z"
            filename = ("vod_viewed_events.csv" if name == "vod_viewed" else
                        "donation_events.csv" if name in ("donation", "donation_return") else
                        "people_feed_events.csv")
            write(filename, dict(external_id=eid, contact_external_id=f"crm-{i:08d}",
                         message_id=eid, type="track", name=name, occurred_at=stamp,
                         received_at=stamp, properties=json.dumps(props or {}),
                         context='{"synthetic":true}'))

        def money(cents):
            return f"{cents // 100}.{cents % 100:02d}"

        def donation(i, day, suffix, code="SYN-GEN", refund=False, original=None,
                     amount_cents=None, is_recurring=False):
            # CSV and event amounts are USD, not cents. Keep arithmetic in cents
            # internally; amounts and frequency come from the cohort-safe plan.
            if refund:
                if (original is None or original["owner"] != i or original["is_refund"] or
                        original["returned"] or day < original["day"]):
                    raise ValueError("Return requires this person's unreturned original gift")
                cents, code = original["cents"], original["code"]
                is_recurring = original["is_recurring"]
                original["returned"] = True
            else:
                if amount_cents is None or amount_cents <= 0:
                    raise ValueError("Donation requires a positive planned amount")
                cents = amount_cents
            name = "donation_return" if refund else "donation"
            eid = f"{name}-{i}-{suffix}"
            gift_id = f"gift:{eid}"
            props = {"amount": cents / 100, "source_code": code, "fund": "General",
                     "gift_id": gift_id}
            if refund:
                props.update(original_gift_id=original["gift_id"],
                             original_donation_id=original["gift_id"])
            # CSV event imports do not materialize gifts. Write exactly one ledger
            # row here; the source-pull gift adapter is deliberately not invoked.
            # The gift importer signs explicit refunds negative and preserves
            # original_donation_id in attributes; CSV carries the exact magnitude.
            write("donation_events_gifts.csv", dict(external_id=gift_id, contact_external_id=f"crm-{i:08d}",
                          amount=money(cents), currency="USD", gift_date=str(day),
                          fund="General", campaign="Giving Tuesday 2024" if code in GT_CODES else
                          "Year-End Giving" if code == "SYN-YE" else "General Giving",
                          source_code=code, channel="online",
                          is_recurring=is_recurring, is_refund=refund,
                          original_donation_id=original["gift_id"] if refund else ""))
            event(i, name, day, props, suffix)
            return {"gift_id": gift_id, "owner": i, "day": day, "cents": cents,
                    "code": code, "returned": False, "is_refund": refund, "is_recurring": is_recurring}

        for i in range(profiles):
            giver = i < g
            account = i < g + partner
            isbasic = i < basic or g <= i < g + freepartner or g + partner <= i < g + partner + nonapp
            ispremium = basic <= i < basic + premium
            app = i < t["TBN App Donors"] or g + partner <= i < g + partner + nonapp
            active = i < t["Active (120 days)"]
            person = display_person(i, random_seed)
            email = person["email"]
            # Explicit household overlap, hash-only and duplicate HubSpot evidence.
            if i % 1000 == 999:
                email = display_person(i-1, random_seed)["email"]
            known = i < round(profiles * .49)
            row = dict(external_id=f"crm-{i:08d}", first_name=person["first_name"],
                       last_name=person["last_name"], email="" if i % 200 == 199 else email,
                       email_sha256=hashlib.sha256(email.encode()).hexdigest(),
                       zeta_cdp_id=f"cdp-{i}" if i < round(profiles * .93) else "",
                       zeta_uid=f"uid-{i}" if i >= round(profiles * .93) else "",
                       donor_account_number=f"donor-{i}" if account else "",
                       okta_customer_id=f"okta-{i}" if app else "",
                       hubspot_record_id=f"hub-{i}",
                       phone=f"+1202{2000000+i:07d}" if i < round(profiles*.08) else "",
                       known_to_zeta=known, has_active_email=i < t["Has Active Email"],
                       email_validated=i < t["Has Active Email"],
                       last_seen=f"{w.as_of-timedelta(days=1 if active else 400)}T12:00:00Z",
                       tbn_video_on_demand_updates_opt_status=not (i < t["VOD Optouts"]),
                       mobile_mktg_optin_tbn=i < t["Mobile Subscribed"],
                       mobile_consent=i < t["Mobile Subscribed"],
                       tbn_call_center_sms_optin=i < t["Call Center SMS Opt-ins"])
            row.update({key: person[key] for key in
                        ("address1", "city", "region", "postal_code", "country")})
            # Disjoint opt-out and subscribed cohorts.
            if t["Call Center SMS Opt-ins"] <= i < t["Call Center SMS Opt-ins"] + t["SMS Opt-Out Date Exists"]:
                row["mobile_mktg_optout_date_tbn"] = f"{w.end}T12:00:00Z"
            if isbasic or ispremium:
                row.update(tbn_plus_subscription="basic" if isbasic else "premium_monthly",
                           tbn_plus_subscription_status="active")
            if basic <= i < basic + t["Premium Upgraders"]:
                row["tbn_plus_subscription_update"] = "Upgrade from basic"
            if account:
                row.update(donor_account_status="active", donor_status="donor" if giver else "non_donor",
                           donor_account_type="recurring" if i < t["Basic Recurring Donors"] else
                           "one_time" if i < t["Basic Recurring Donors"] + t["Basic One-Time Donors"] else "standard",
                           has_recurring_donation=i < t["Basic Recurring Donors"])
            if giver:
                row.update(rfm_date=str(w.end),
                           rfm_recency_score=0 if i >= g-t["RFM Lapsed Donors"] else 5,
                           rfm_frequency_score=2, rfm_monetary_score=2,
                           rfm_raw_frequency=1 if t["Basic Recurring Donors"] <= i <
                           t["Basic Recurring Donors"] + t["Basic One-Time Donors"] else 3,
                           rfm_bi_segment="lapsed" if i >= g-t["RFM Lapsed Donors"] else "active")
            if i < t["Living Legacy Prospects V2"]:
                row.update(donation_likelihood_prob=.85, wealth_score=5, amt_suggested=750,
                           donation_likelihood_model_date=str(w.end),
                           wealth_score_model_date=str(w.end), donation_ask_model_date=str(w.end))
            if i < t["Has Active Email"]:
                # Only partition the existing secondary contact rows. Keep
                # every row's keys, reference and consent evidence unchanged.
                secondary_file = ("okta_tbn_plus.csv" if app and row["okta_customer_id"] else
                                  "hubspot.csv" if i % 2 else "people_feed.csv")
                write(secondary_file, dict(external_id=f"esp-{i}", contact_external_id=f"crm-{i:08d}",
                              email=row["email"], email_consent="opted_in",
                              zeta_cdp_id=row["zeta_cdp_id"], zeta_uid=row["zeta_uid"],
                              email_sha256=row["email_sha256"],
                              hubspot_record_id=row["hubspot_record_id"] if i % 10000 == 9999 else "",
                              consent_captured_at=f"{w.end}T12:00:00Z"))
            if known:
                write("model_scores.csv", dict(external_id=f"zeta-{i}", contact_external_id=f"crm-{i:08d}",
                              hh_income_band="75k_149k", age_band="55_64", interests="media",
                              donor_propensity_score=f"{rng.random():.4f}",
                              vendor="kinship-synthetic-demo", license_id="synthetic-demo-only",
                              append_date=str(w.end), sensitive=False))
            if giver:
                lifetime, gifts, lastday, lastamount, lastcode = 0, 0, w.start, 0, ""
                for planned in giving.gifts(i):
                    original = donation(i, planned.day, planned.suffix, planned.code,
                                        amount_cents=planned.amount_cents,
                                        is_recurring=planned.is_recurring)
                    if planned.refund_day is not None:
                        donation(i, planned.refund_day, planned.suffix, refund=True, original=original)
                        continue
                    lifetime += original["cents"]
                    gifts += 1
                    if planned.day >= lastday:
                        lastday, lastamount, lastcode = planned.day, original["cents"], planned.code
                recency = (w.as_of-lastday).days
                status = "lapsed" if recency > 730 else "lapsing" if recency > 365 else "active"
                row.update(last_donation_date=str(lastday), last_donation_amount=money(lastamount),
                           last_donation_source=lastcode, lifetime_amount=money(lifetime),
                           lifetime_avg_donation=money((2*lifetime+gifts)//(2*gifts)),
                           number_of_donations=gifts, rfm_raw_frequency=gifts,
                           rfm_raw_recency=recency, rfm_raw_monetary=money(lifetime),
                           rfm_recency_score=0 if recency > 730 else 1 if recency > 365 else 5,
                           rfm_frequency_score=1 if gifts == 1 else 2 if gifts <= 3 else 4 if gifts < 20 else 5,
                           rfm_monetary_score=1 if lifetime < 10000 else 3 if lifetime < 100000 else 5,
                           rfm_bi_segment=status, donor_account_status=status, donor_status=status)
                row["rfm_rfm_score"] = (row["rfm_recency_score"] * 100 +
                                        row["rfm_frequency_score"] * 10 + row["rfm_monetary_score"])
            write("donor_direct.csv", row)
            if i < t["Customers with VOD Viewed"]:
                event(i, "vod_viewed", w.end, {"vod_title_id": "SYN-VOD"})
            if i < t["Mobile Subscribed"]:
                for name in ("text_signup", "text_web_signup", "text_reply_confirmation"):
                    event(i, name, w.end)
            if i < t["Call Center SMS Opt-ins"]:
                event(i, "text_call_center_signup", w.end)
            if "mobile_mktg_optout_date_tbn" in row:
                event(i, "text_reply_stop", w.end)
            if i < t["Has Active Email"]:
                event(i, "email_signup", w.start)
            if app:
                event(i, "user_web_signup", w.start)
            if i < t["VOD Optouts"]:
                event(i, "preference_update", w.end, {"vod_updates": False})
        # Exact total rates, distributed across all 30 days without daily rounding bias.
        for j in range(round(profiles * 171 * 30 / 7830000)):
            i = j % max(1, recent)
            day = w.end - timedelta(days=j % 30)
            event(i, "order_shipped", day, {"product_id": "SYN-BOOK", "amount": 25}, str(j))
        expected_financial_rows = giving.donations + giving.return_count
        if (giving.amount_ordinal != giving.donations or giving.return_ordinal != giving.return_count
                or counts["donation_events_gifts.csv"] != expected_financial_rows
                or counts["donation_events.csv"] != expected_financial_rows):
            raise ValueError("Gift plan quotas and emitted financial rows disagree")
    metadata = {"as_of": str(w.as_of), "start_date": str(w.start), "end_date": str(w.end),
                "mode": "audit", "requested_people": profiles, "random_seed": random_seed,
                "scale": scale, "denominator": DENOMINATOR,
                "scale_policy": "Audit ratios and daily rates are invariant across density labels.",
                "generation_memory": "O(gifts + donors) calendar plan; CSV rows/identity truth are streamed.",
                "giving_targets": {"donations": giving.donations, "returns": giving.return_count,
                                   "recent_donations": giving.recent_donations,
                                   "single_givers": len(giving.singles),
                                   "gifts_1000_plus": giving.tail_count, "gifts_10000_plus": giving.major_count},
                "timeline_checks": giving.timeline,
                "ground_truth_format": "Compact deterministic identity rule, not an in-memory event dictionary.",
                "giving_tuesday_in_window": w.start <= GT <= w.end,
                "source_file_mapping": {
                    name: {"source": source, "record_type": record_type}
                    for name, (source, record_type) in AUDIT_FILES.items()},
                "reference_source": "donor_direct",
                "source_record_count": sum(counts.values()),
                "files": {name: {"rows_generated": count, "messiness_injected": {}}
                          for name, count in counts.items()}}
    for name, data in (("generator_stats.json", metadata), ("resources.json", RESOURCES),
                       ("ground_truth.json", {"format_version": 2, "requested_people": profiles,
                        "identity_rule": "crm-{zero_based_index:08d}; synthetic only",
                        "household_rule": "Indices 1000*k+998 and 1000*k+999 share email; distinct Zeta IDs",
                        "hash_only_rule": "index % 200 == 199",
                        "hubspot_duplicate_rule": "index % 10000 == 9999 repeats donor_direct HubSpot ID on the partitioned secondary contact record of same person",
                        "source_file_mapping": metadata["source_file_mapping"],
                        "reference_source": "donor_direct",
                        "secondary_contact_rule": "Existing esp-{index} rows only: okta_tbn_plus when app and okta_customer_id; otherwise hubspot for odd index and people_feed for even index",
                        "as_of": str(w.as_of)})):
        (dest/name).write_text(json.dumps(data, indent=2)+"\n")
    return {**metadata, "output_dir": dest, "files": {name: dest/name for name in counts},
            "counts": counts, "ground_truth": dest/"ground_truth.json",
            "generator_stats": dest/"generator_stats.json", "record_count": sum(counts.values())}


def load_resources(directory):
    """Seed-only bootstrap via the same validated resource COPY importer."""
    path = Path(directory) / "resources.json"
    if not path.exists():
        return
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.resources import ResourceBatch, import_resources
    with Session(engine) as db:
        for kind, rows in json.loads(path.read_text()).items():
            if kind not in RESOURCES:
                raise ValueError(f"Unknown seed resource type {kind}")
            import_resources(kind, ResourceBatch(rows=rows), user=None, db=db)
