"""Audited first-party properties. Imported models never read computed traits."""
from datetime import date, datetime, timezone
from decimal import Decimal
import re

from sqlalchemy import text

FIELDS = {}


def _fields(group, data_type, names):
    for name in names.split():
        FIELDS[name] = {"data_type": data_type, "group": group, "sensitive": name == "inmate"}

_fields("Login identity", "text", "okta_customer_id fusionauth_user_id")


_fields("Donor", "text", """donor_account_status donor_account_type donor_status
inception_source_code last_donation_source donation_trend recptpref stopmail offerpref
phonepref newspref receives_nl suprcore""")
_fields("Donor", "boolean", "ambassador has_recurring_donation do_not_call inmate")
_fields("Donor", "number", "last_donation_amount lifetime_amount lifetime_avg_donation number_of_donations")
_fields("Donor", "date", "last_donation_date")
_fields("Imported models", "number", """rfm_raw_recency rfm_raw_frequency rfm_raw_monetary
rfm_recency_score rfm_frequency_score rfm_monetary_score rfm_rfm_score
donation_likelihood_prob donation_likelihood_decile donation_likelihood_percentile
donation_prob_20_40 donation_prob_50_75 donation_prob_80_120 donation_prob_200_300
donation_prob_1000_1500 est_best_amt amt_suggested donation_ask_2_2_multinomial
donation_ask_2_2_knn wealth_score""")
_fields("Imported models", "text", "rfm_bi_segment")
_fields("Imported models", "date", "rfm_date donation_likelihood_model_date donation_ask_model_date wealth_score_model_date")
_fields("SMS evidence", "boolean", "mobile_mktg_optin_tbn mobile_consent tbn_call_center_sms_optin")
_fields("SMS evidence", "timestamp", "mobile_mktg_optin_date_tbn mobile_mktg_optout_date_tbn tlv_optin_date")
_fields("SMS evidence", "text", """initial_mobile_mktg_source last_mobile_mktg_source keyword_tbn
carrier aggregator_code last_mobile_bounce_reason""")
_fields("TBN+", "text", "tbn_plus_subscription tbn_plus_subscription_status tbn_plus_subscription_update")
_fields("TBN+", "boolean", "tbn_video_on_demand_updates_opt_status historical_vimeo")
_fields("TBN+", "timestamp", "tbn_plus_subscription_end_date signup_date")
_fields("Email hygiene", "boolean", "has_active_email email_validated historical_email_engaged")
_fields("Email hygiene", "text", "historical_email_hard_bounce_reason")
_fields("Address", "text", "address1 address2 city region postal_code country ncoa_return_code")
_fields("Address", "date", "ncoa_last_processed_date address_update_date")
_fields("Activity", "timestamp", "last_seen last_contact")
_fields("Identity", "boolean", "known_to_zeta")
_fields("Identity", "text", "donor_account_number")
for _key, _meta in FIELDS.items():
    if _key.startswith("rfm_") and _key != "rfm_date":
        _meta["model_date"] = "rfm_date"
    elif _key.startswith("donation_likelihood_") and _key != "donation_likelihood_model_date":
        _meta["model_date"] = "donation_likelihood_model_date"
    elif _key.startswith("donation_prob_") or _key in (
        "est_best_amt", "amt_suggested", "donation_ask_2_2_multinomial", "donation_ask_2_2_knn"
    ):
        _meta["model_date"] = "donation_ask_model_date"
    elif _key == "wealth_score":
        _meta["model_date"] = "wealth_score_model_date"

EVENT_NAMES = ("email_sent", "email_opened", "email_clicked", "email_bounced",
               "donation", "donation_return", "order_shipped", "vod_viewed", "email_signup",
               "user_web_signup", "preference_update", "text_signup", "text_web_signup",
               "text_call_center_signup", "text_reply_confirmation", "text_reply_stop")
ALIASES = {re.sub(r"[^a-z0-9]", "", key): key for key in FIELDS}


def canonical_name(key):
    return ALIASES.get(re.sub(r"[^a-z0-9]", "", key.lower()), key)


def convert(key, value):
    if value is None:
        return None
    typ = FIELDS[key]["data_type"]
    if typ == "boolean":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "yes", "y", "1"):
            return True
        if str(value).lower() in ("false", "no", "n", "0"):
            return False
        raise ValueError(f"{key}: expected boolean")
    if typ == "number":
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError(f"{key}: expected finite number")
        # The importer's JSON encoder stores non-integral Decimals exactly;
        # never round analyst scores or lifetime amounts through binary floats.
        return int(number) if number == number.to_integral_value() else number
    if typ == "date":
        return date.fromisoformat(str(value)).isoformat()
    if typ == "timestamp":
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None
                else parsed.astimezone(timezone.utc)).isoformat()
    value = str(value)
    if key == "tbn_plus_subscription" and value not in ("basic", "premium_monthly", "premium_annual"):
        raise ValueError("tbn_plus_subscription: unsupported plan")
    return value


def normalize_attributes(attributes):
    result = {}
    for key, value in attributes.items():
        name = canonical_name(key)
        value = convert(name, value) if name in FIELDS else value
        if name in result and result[name] != value:
            raise ValueError(f"Conflicting aliases for {name}; no value was discarded")
        result[name] = value
    return result


def canonical_attributes_many(db, ids):
    """One query per batch; per-field priority, newest record breaks ties."""
    result = {int(pid): {} for pid in ids}
    if not result:
        return result
    rows = db.execute(text("""
        SELECT sr.profile_id, sr.attributes, sr.address1, sr.address2, sr.city,
               sr.region, sr.postal_code, sr.country
        FROM source_records sr JOIN sources s ON s.id=sr.source_id
        WHERE sr.profile_id = ANY(:ids)
        ORDER BY s.priority, sr.updated_at DESC, sr.id
    """), {"ids": list(result)}).mappings()
    for row in rows:
        attrs = normalize_attributes(row["attributes"] or {})
        for key in ("address1", "address2", "city", "region", "postal_code", "country"):
            if row[key] is not None:
                attrs[key] = row[key]
        for key, value in attrs.items():
            if key in FIELDS and key not in result[row["profile_id"]] and value is not None:
                result[row["profile_id"]][key] = value
    return result


def canonical_attributes(db, profile_id):
    return canonical_attributes_many(db, [profile_id])[profile_id]
