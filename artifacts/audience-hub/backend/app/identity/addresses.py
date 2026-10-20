"""US address parsing with conservative keys; household grouping is not identity."""
import hashlib
import json
import re

from sqlalchemy import text

STREETS = {"STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR",
           "BOULEVARD": "BLVD", "LANE": "LN", "COURT": "CT", "PLACE": "PL",
           "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
STATES = dict(line.split(":") for line in (
    "ALABAMA:AL|ALASKA:AK|ARIZONA:AZ|ARKANSAS:AR|CALIFORNIA:CA|COLORADO:CO|CONNECTICUT:CT|"
    "DELAWARE:DE|DISTRICT OF COLUMBIA:DC|FLORIDA:FL|GEORGIA:GA|HAWAII:HI|IDAHO:ID|ILLINOIS:IL|"
    "INDIANA:IN|IOWA:IA|KANSAS:KS|KENTUCKY:KY|LOUISIANA:LA|MAINE:ME|MARYLAND:MD|"
    "MASSACHUSETTS:MA|MICHIGAN:MI|MINNESOTA:MN|MISSISSIPPI:MS|MISSOURI:MO|MONTANA:MT|"
    "NEBRASKA:NE|NEVADA:NV|NEW HAMPSHIRE:NH|NEW JERSEY:NJ|NEW MEXICO:NM|NEW YORK:NY|"
    "NORTH CAROLINA:NC|NORTH DAKOTA:ND|OHIO:OH|OKLAHOMA:OK|OREGON:OR|PENNSYLVANIA:PA|"
    "RHODE ISLAND:RI|SOUTH CAROLINA:SC|SOUTH DAKOTA:SD|TENNESSEE:TN|TEXAS:TX|UTAH:UT|"
    "VERMONT:VT|VIRGINIA:VA|WASHINGTON:WA|WEST VIRGINIA:WV|WISCONSIN:WI|WYOMING:WY|"
    "PUERTO RICO:PR|GUAM:GU|VIRGIN ISLANDS:VI"
).split("|"))


def clean(value):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 -]", "", str(value or "").upper())).strip()


def parse_address(row):
    import usaddress
    raw = ", ".join(str(row[k]) for k in ("address1", "address2", "city", "region", "postal_code")
                    if row.get(k))
    try:
        tags, kind = usaddress.tag(raw)
    except usaddress.RepeatedLabelError:
        tags, kind = {}, "Ambiguous"
    number = clean(tags.get("AddressNumber"))
    street = " ".join(STREETS.get(word, word) for word in clean(" ".join(
        tags.get(k, "") for k in ("StreetNamePreDirectional", "StreetNamePreType",
                                  "StreetName", "StreetNamePostType", "StreetNamePostDirectional")
    )).split())
    unit = clean(" ".join(tags.get(k, "") for k in ("OccupancyIdentifier", "SubaddressIdentifier")))
    city = clean(row.get("city") or tags.get("PlaceName"))
    state = clean(row.get("region") or tags.get("StateName"))
    state = STATES.get(state, state)
    postal = re.sub(r"\D", "", str(row.get("postal_code") or tags.get("ZipCode") or ""))
    zip5, zip4 = postal[:5], postal[5:9]
    # Never group incomplete/ambiguous, PO box, or foreign addresses.
    valid = (kind == "Street Address" and number and street and len(state) == 2
             and len(zip5) == 5 and str(row.get("country") or "US").upper()
             in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"))
    normalized = "|".join((number, street, unit, city, state, zip5))
    key = "addr_" + hashlib.sha256(normalized.encode()).hexdigest() if valid else None
    return dict(number=number or None, street=street or None, unit=unit or None,
                city=city or None, state=state or None, zip5=zip5 or None, zip4=zip4 or None,
                normalized_key=key, parser="usaddress")


def refresh_addresses(db, profile_ids=None):
    """Keyset batches of changed canonical addresses; leave vendor evidence intact."""
    after, changed = 0, 0
    while True:
        rows = db.execute(text("""
          SELECT p.id,p.address1,coalesce(s.address2,'') AS address2,p.city,p.region,p.postal_code,p.country,
            a.input_hash FROM active_profiles p
          LEFT JOIN LATERAL (SELECT address2 FROM source_records WHERE profile_id=p.id
            AND address1=p.address1 ORDER BY updated_at DESC,id DESC LIMIT 1) s ON true
          LEFT JOIN identity_addresses a ON a.profile_id=p.id
          WHERE p.id>:after AND (CAST(:ids AS bigint[]) IS NULL OR p.id=ANY(CAST(:ids AS bigint[])))
          ORDER BY p.id LIMIT 1000
        """), {"after": after, "ids": profile_ids}).mappings().all()
        if not rows:
            break
        for row in rows:
            fingerprint = hashlib.sha256(json.dumps(
                [row[k] for k in ("address1", "address2", "city", "region", "postal_code", "country")]
            ).encode()).hexdigest()
            if fingerprint == row["input_hash"]:
                continue
            values = parse_address(dict(row))
            db.execute(text("""
              INSERT INTO identity_addresses(profile_id,input_hash,number,street,unit,city,state,
                zip5,zip4,normalized_key,parser) VALUES(:id,:hash,:number,:street,:unit,:city,
                :state,:zip5,:zip4,:normalized_key,:parser)
              ON CONFLICT(profile_id) DO UPDATE SET input_hash=EXCLUDED.input_hash,
                number=EXCLUDED.number,street=EXCLUDED.street,unit=EXCLUDED.unit,city=EXCLUDED.city,
                state=EXCLUDED.state,zip5=EXCLUDED.zip5,zip4=EXCLUDED.zip4,
                normalized_key=EXCLUDED.normalized_key,parser=EXCLUDED.parser,
                vendor=NULL,vendor_result=NULL,standardized_at=now()
            """), {**values, "id": row["id"], "hash": fingerprint})
            changed += 1
        after = rows[-1]["id"]
    return changed


def vendor_result(db, profile_id, input_hash, vendor, result):
    """CASS/NCOA adapter hook; stale vendor files cannot overwrite a newer address.

    File adapters provide parsed components in result and preserve the original
    vendor payload. No schema migration is required for an additional vendor.
    """
    required = {"number", "street", "city", "state", "zip5"}
    if not required.issubset(result):
        raise ValueError("Vendor result must include standardized address components")
    parsed = parse_address({"address1": f"{result['number']} {result['street']}",
                            "address2": "UNIT " + result["unit"] if result.get("unit") else None,
                            "city": result["city"],
                            "region": result["state"],
                            "postal_code": result["zip5"] + result.get("zip4", ""), "country": "US"})
    if not parsed["normalized_key"]:
        raise ValueError("Vendor result is not a complete US street address")
    return db.execute(text("""
      UPDATE identity_addresses SET number=:number,street=:street,unit=:unit,city=:city,state=:state,
        zip5=:zip5,zip4=:zip4,normalized_key=:normalized_key,parser='vendor',
        vendor=:vendor,vendor_result=CAST(:result AS jsonb),standardized_at=now()
      WHERE profile_id=:id AND input_hash=:hash
    """), {**parsed, "id": profile_id, "hash": input_hash, "vendor": vendor,
           "result": json.dumps(result)}).rowcount


def refresh_households(db):
    """Stable address/surname IDs, separate from Master ID. Null addresses are singletons."""
    surname = db.scalar(text("SELECT household_surname FROM identity_graph_policy WHERE id=1"))
    db.execute(text("""
      WITH groups AS (
        SELECT p.id,CASE WHEN a.normalized_key IS NOT NULL
            AND (NOT :surname OR nullif(lower(btrim(p.last_name)),'') IS NOT NULL)
          THEN 'hh_'||encode(digest(a.normalized_key ||
            CASE WHEN :surname THEN '|'||lower(btrim(p.last_name)) ELSE '' END,'sha256'),'hex')
          END AS household FROM active_profiles p
        LEFT JOIN identity_addresses a ON a.profile_id=p.id
      ), sized AS (
        SELECT id,household,CASE WHEN household IS NULL THEN 1
          ELSE count(*) OVER(PARTITION BY household)::integer END AS size FROM groups
      ) UPDATE profiles p SET household_id=s.household,household_size=s.size FROM sized s
        WHERE p.id=s.id AND (p.household_id IS DISTINCT FROM s.household OR p.household_size<>s.size)
    """), {"surname": bool(surname)})
    db.execute(text("""UPDATE profiles SET household_id=NULL,household_size=1
      WHERE (merged_into_id IS NOT NULL OR is_deleted) AND household_id IS NOT NULL"""))
