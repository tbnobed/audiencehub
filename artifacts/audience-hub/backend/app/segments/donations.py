"""Latest donation-event evidence, excluding explicitly linked returns.

Both event-only ingestion and mirrored gift ledgers are supported. A return
matches only within the same profile/source; unknown references never erase
unrelated gifts. Amounts, identity links and imported canonical fields are not
modified here.
"""
from sqlalchemy import and_, or_, select, literal, func
from app.models import Event, Gift


def unreturned_donation(event):
    returns = Event.__table__.alias("donation_returns")
    gifts = Gift.__table__.alias("donation_refunds")
    identifiers = (
        event.c.message_id, literal("gift:") + event.c.message_id,
        literal("donation:") + event.c.message_id,
        literal("donation:_event:") + event.c.message_id,
    )
    event_return = select(literal(1)).select_from(returns).where(
        returns.c.profile_id == event.c.profile_id,
        returns.c.source_id == event.c.source_id,
        returns.c.name == "donation_return",
        or_(*(returns.c.properties[key].astext.in_(identifiers)
              for key in ("original_donation_id", "original_gift_external_id"))),
    ).correlate(event).exists()
    gift_return = select(literal(1)).select_from(gifts).where(
        gifts.c.profile_id == event.c.profile_id,
        gifts.c.source_id == event.c.source_id,
        or_(gifts.c.amount < 0, gifts.c.attributes["is_refund"].astext == "true"),
        or_(*(gifts.c.attributes[key].astext.in_(identifiers)
              for key in ("original_donation_id", "original_gift_external_id"))),
    ).correlate(event).exists()
    return and_(func.coalesce(event.c.properties["is_refund"].astext, "false") != "true",
                ~event_return, ~gift_return)


# Frozen equivalent also lives in migration 0037 so future changes here cannot
# change the meaning of an already published migration.
UPDATE_LAST_DONATION = """
WITH returned AS MATERIALIZED (
  SELECT r.profile_id,r.source_id,ref.value
  FROM events r CROSS JOIN LATERAL (VALUES
    (r.properties->>'original_donation_id'),
    (r.properties->>'original_gift_external_id')) ref(value)
  WHERE r.profile_id=ANY(CAST(:ids AS bigint[]))
    AND r.name='donation_return' AND ref.value IS NOT NULL
  UNION ALL
  SELECT r.profile_id,r.source_id,ref.value
  FROM gifts r CROSS JOIN LATERAL (VALUES
    (r.attributes->>'original_donation_id'),
    (r.attributes->>'original_gift_external_id')) ref(value)
  WHERE r.profile_id=ANY(CAST(:ids AS bigint[]))
    AND (r.amount<0 OR r.attributes->>'is_refund'='true')
    AND ref.value IS NOT NULL
), donations AS (
  SELECT e.profile_id,max(e.occurred_at) AS latest
  FROM events e
  WHERE e.profile_id=ANY(CAST(:ids AS bigint[])) AND e.name='donation'
    AND COALESCE(e.properties->>'is_refund','false')<>'true'
    AND NOT EXISTS (
      SELECT 1 FROM returned r WHERE r.profile_id=e.profile_id
        AND r.source_id=e.source_id AND r.value=ANY(ARRAY[
          e.message_id,'gift:'||e.message_id,'donation:'||e.message_id,
          'donation:_event:'||e.message_id])
    )
  GROUP BY e.profile_id
)
UPDATE profiles p SET last_donation_at=d.latest
FROM (
  SELECT target.id,donations.latest
  FROM unnest(CAST(:ids AS bigint[])) target(id)
  LEFT JOIN donations ON donations.profile_id=target.id
) d WHERE p.id=d.id AND p.last_donation_at IS DISTINCT FROM d.latest
"""
