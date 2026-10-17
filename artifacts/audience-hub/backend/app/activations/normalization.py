"""Provider file normalization; hashes are SHA-256, not suppression HMACs."""
import hashlib
import re
import unicodedata
from app.identity.normalize import normalize_phone, validated_email

GOOGLE_COLUMNS = ["Email", "Phone", "First Name", "Last Name", "Country", "Zip"]
META_COLUMNS = ["email", "phone", "fn", "ln", "zip", "ct", "st", "country", "external_id"]
US_STATES = dict(pair.split(":") for pair in (
    "alabama:al|alaska:ak|arizona:az|arkansas:ar|california:ca|colorado:co|connecticut:ct|"
    "delaware:de|district of columbia:dc|florida:fl|georgia:ga|hawaii:hi|idaho:id|illinois:il|"
    "indiana:in|iowa:ia|kansas:ks|kentucky:ky|louisiana:la|maine:me|maryland:md|massachusetts:ma|"
    "michigan:mi|minnesota:mn|mississippi:ms|missouri:mo|montana:mt|nebraska:ne|nevada:nv|"
    "new hampshire:nh|new jersey:nj|new mexico:nm|new york:ny|north carolina:nc|north dakota:nd|"
    "ohio:oh|oklahoma:ok|oregon:or|pennsylvania:pa|rhode island:ri|south carolina:sc|"
    "south dakota:sd|tennessee:tn|texas:tx|utah:ut|vermont:vt|virginia:va|washington:wa|"
    "west virginia:wv|wisconsin:wi|wyoming:wy|puerto rico:pr|guam:gu|american samoa:as|"
    "northern mariana islands:mp|us virgin islands:vi"
).split("|"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def name(value):
    # Keep Unicode letters (including accents), strip punctuation and whitespace.
    return "".join(c for c in unicodedata.normalize("NFC", str(value or "")).lower() if c.isalpha())


def email(value):
    return (validated_email(str(value or "")) or "").strip().lower()


def phone(value):
    return normalize_phone(value) or ""


def google(row):
    country = str(row.get("country") or "").strip().upper()
    country = country if re.fullmatch("[A-Z]{2}", country) else ""
    zip_code = str(row.get("postal_code") or "").strip()
    if country == "US":
        match = re.fullmatch(r"(\d{5})(?:-\d{4})?", zip_code)
        zip_code = match[1] if match else ""
    address = email(row.get("email"))
    if "@" in address:
        local, domain = address.rsplit("@", 1)
        if domain in ("gmail.com", "googlemail.com"):
            address = local.replace(".", "") + "@" + domain
    return [digest(address), digest(phone(row.get("phone"))),
            digest(name(row.get("first_name"))), digest(name(row.get("last_name"))),
            country, zip_code]


def meta(row):
    state = str(row.get("region") or "").strip().lower()
    state = US_STATES.get(state, state)
    state = state if re.fullmatch("[a-z]{2}", state) else ""
    values = [email(row.get("email")), phone(row.get("phone")).lstrip("+"),
              name(row.get("first_name")), name(row.get("last_name")),
              str(row.get("postal_code") or "").strip().lower(),
              name(row.get("city")), state,
              str(row.get("country") or "").strip().lower(), str(row["id"])]
    return [digest(v) for v in values]


def csv_safe(value):
    value = "" if value is None else str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value
