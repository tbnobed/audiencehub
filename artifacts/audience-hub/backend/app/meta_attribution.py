"""Click attribution is never an identifier or a resolver edge."""
import json
import re
from sqlalchemy import text
from app.ingestion.service import anonymous_key

KEYS = ("fbclid", "_fbp", "_fbc")


def extract(message):
    result = {}
    for container in (message, message.get("context", {}), message.get("properties", {})):
        for key in KEYS:
            if key in container:
                value = container[key]
                maximum = 600 if key == "_fbc" else 512
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1," + str(maximum) + "}", value):
                    raise ValueError("Invalid Meta click attribution value.")
                result[key] = value
    return result


def capture(db, source_id, message):
    values = message.get("attribution", {})
    if not values or not message.get("anonymousId"):
        return
    db.execute(text("""INSERT INTO meta_attribution_sessions(source_id,session_key,attribution,
      first_seen_at,last_seen_at) VALUES(:s,:key,CAST(:a AS jsonb),:at,:at)
      ON CONFLICT(source_id,session_key) DO UPDATE SET
      attribution=CASE WHEN EXCLUDED.last_seen_at>=meta_attribution_sessions.last_seen_at
        THEN meta_attribution_sessions.attribution || EXCLUDED.attribution
        ELSE EXCLUDED.attribution || meta_attribution_sessions.attribution END,
      first_seen_at=least(meta_attribution_sessions.first_seen_at,EXCLUDED.first_seen_at),
      last_seen_at=greatest(meta_attribution_sessions.last_seen_at,EXCLUDED.last_seen_at),
      attached_at=CASE WHEN meta_attribution_sessions.attribution=(
        meta_attribution_sessions.attribution || EXCLUDED.attribution)
        THEN meta_attribution_sessions.attached_at ELSE NULL END"""),
      {"s": source_id, "key": anonymous_key(source_id, message["anonymousId"]),
       "a": json.dumps(values), "at": message["occurred_at"]})


def attach(db, source_id):
    # Unique existing owner only; the click IDs never participate in this join.
    db.execute(text("""WITH owners AS (
      SELECT a.source_id,a.session_key,min(p.id) AS profile_id FROM meta_attribution_sessions a
      JOIN identifiers i ON i.type='anonymous_id' AND i.value=a.session_key
      JOIN active_profiles p ON p.id=i.profile_id WHERE a.source_id=:s
        AND NOT EXISTS(SELECT 1 FROM identifier_blocklist b WHERE b.type=i.type AND b.value=i.value)
      GROUP BY a.source_id,a.session_key HAVING count(DISTINCT p.id)=1
    ), linked AS (
      UPDATE meta_attribution_sessions a SET profile_id=o.profile_id,attached_at=clock_timestamp()
      FROM owners o WHERE a.source_id=o.source_id AND a.session_key=o.session_key
        AND (a.attached_at IS NULL OR a.profile_id IS DISTINCT FROM o.profile_id)
      RETURNING a.*
    ) INSERT INTO events(source_id,profile_id,type,name,occurred_at,properties,message_id)
      SELECT source_id,profile_id,'track','Meta Attribution',last_seen_at,
        attribution || jsonb_build_object('attribution_only',true),
        'meta-attribution:'||encode(sha256(convert_to(session_key||attribution::text,'UTF8')),'hex')
      FROM linked ON CONFLICT(source_id,message_id,occurred_at) DO NOTHING"""), {"s": source_id})
