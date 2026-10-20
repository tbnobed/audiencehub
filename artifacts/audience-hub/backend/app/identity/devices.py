"""Device observations are NOT resolver identifiers and never merge people."""
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

DEVICE_KEY_TYPES = ("roku_rida", "samsung_tifa", "apple_idfa", "google_aaid",
                    "app_install_id", "web_cookie_id")


def rehome_devices(db):
    """Follow authoritative Okta ownership, including after merge reversal."""
    # Resolve collisions before moving. The same authenticated device may have
    # appeared on both sides of a merge.
    db.execute(text("""
      INSERT INTO identity_devices(key_type,device_id,profile_id,source_id,okta_customer_id,
        first_seen_at,last_seen_at,expires_at)
      SELECT DISTINCT ON(d.key_type,d.device_id,p.id) d.key_type,d.device_id,p.id,d.source_id,
        d.okta_customer_id,d.first_seen_at,d.last_seen_at,d.expires_at
      FROM identity_devices d JOIN identity_xref x ON x.key_type='okta_customer_id'
        AND x.key_value=d.okta_customer_id JOIN active_profiles p ON p.master_id=x.master_id
      WHERE p.id<>d.profile_id
        AND (SELECT count(DISTINCT master_id) FROM identity_xref
          WHERE key_type='okta_customer_id' AND key_value=d.okta_customer_id)=1
      ORDER BY d.key_type,d.device_id,p.id,d.last_seen_at DESC
      ON CONFLICT(key_type,device_id,profile_id) DO UPDATE SET
        first_seen_at=least(identity_devices.first_seen_at,EXCLUDED.first_seen_at),
        last_seen_at=greatest(identity_devices.last_seen_at,EXCLUDED.last_seen_at),
        expires_at=greatest(identity_devices.expires_at,EXCLUDED.expires_at);
      DELETE FROM identity_devices d WHERE NOT EXISTS(
        SELECT 1 FROM identity_xref x JOIN active_profiles p ON p.master_id=x.master_id
        WHERE x.key_type='okta_customer_id' AND x.key_value=d.okta_customer_id AND p.id=d.profile_id)
    """))


def observe(db, source_id, event, *, trusted_authenticated=False):
    """Only server-validated authenticated observations are eligible.

    Callers MUST establish authentication independently of event JSON. A browser
    boolean or possession of the public ingestion write key is not sufficient.
    """
    if not trusted_authenticated:
        return 0
    properties = event.get("properties") or {}
    okta = str(properties.get("okta_customer_id") or "").strip()
    if not okta:
        return 0
    owners = db.scalars(text("""SELECT DISTINCT p.id FROM identity_xref x
      JOIN active_profiles p ON p.master_id=x.master_id
      WHERE x.key_type='okta_customer_id' AND x.key_value=:key"""), {"key": okta}).all()
    if len(owners) != 1:
        return 0
    when = event["occurred_at"]
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if when.tzinfo is None or when > now + timedelta(minutes=5):
        raise ValueError("Device observations need a valid UTC event time")
    days = db.scalar(text("SELECT device_expiry_days FROM identity_graph_policy WHERE id=1"))
    if when <= now - timedelta(days=days):
        return 0
    count = 0
    for kind in DEVICE_KEY_TYPES:
        value = str(properties.get(kind) or "").strip()
        if not value:
            continue
        if kind in DEVICE_KEY_TYPES[:4]:
            try:
                value = str(UUID(value))
            except ValueError:
                raise ValueError(f"Invalid {kind}") from None
            if UUID(value).int == 0:
                continue
        elif not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
            raise ValueError(f"Invalid {kind}")
        db.execute(text("""
          INSERT INTO identity_devices(key_type,device_id,profile_id,source_id,okta_customer_id,
            first_seen_at,last_seen_at,expires_at)
          VALUES(:kind,:value,:profile,:source,:okta,:when,:when,:when+make_interval(days=>:days))
          ON CONFLICT(key_type,device_id,profile_id) DO UPDATE SET
            first_seen_at=least(identity_devices.first_seen_at,EXCLUDED.first_seen_at),
            last_seen_at=greatest(identity_devices.last_seen_at,EXCLUDED.last_seen_at),
            expires_at=greatest(identity_devices.expires_at,EXCLUDED.expires_at),
            source_id=CASE WHEN EXCLUDED.last_seen_at>=identity_devices.last_seen_at
              THEN EXCLUDED.source_id ELSE identity_devices.source_id END
        """), {"kind": kind, "value": value, "profile": owners[0], "source": source_id,
               "okta": okta, "when": when, "days": days})
        count += 1
    return count
