"""Bounded five-field numeric cron with IANA zones, without extra dependencies."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse(expression):
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Schedule must have five cron fields.")
    result = []
    for expression_field, low, high in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)):
        values = set()
        for part in expression_field.split(","):
            try:
                raw, *step_text = part.split("/")
                if len(step_text) > 1:
                    raise ValueError()
                step = int(step_text[0]) if step_text else 1
                if step < 1 or step > high + 1:
                    raise ValueError()
                if raw == "*":
                    start, end = low, high
                elif "-" in raw:
                    pieces = raw.split("-")
                    if len(pieces) != 2:
                        raise ValueError()
                    start, end = map(int, pieces)
                else:
                    start = end = int(raw)
                    if step_text:
                        end = high
                if start < low or end > high or start > end:
                    raise ValueError()
                values.update(range(start, end + 1, step))
            except (ValueError, TypeError):
                raise ValueError("Invalid cron field; use numbers, *, ranges, lists or steps.") from None
        result.append(values)
    result[4] = {v % 7 for v in result[4]}
    return result, fields


def validate(expression, zone):
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, TypeError):
        raise ValueError("Choose a valid IANA timezone.") from None
    if expression:
        parse(expression)


def matches(expression, zone, instant):
    values, raw = parse(expression)
    local = instant.astimezone(ZoneInfo(zone))
    minute, hour, day, month, weekday = values
    dom = local.day in day
    dow = (local.weekday() + 1) % 7 in weekday
    date_matches = (dom and dow) if raw[2] == "*" or raw[4] == "*" else (dom or dow)
    return local.minute in minute and local.hour in hour and local.month in month and date_matches


def next_run(expression, zone, now=None):
    if not expression:
        return None
    # Bounded calendar search; UTC traversal handles missing/repeated DST times.
    now = now or datetime.now(timezone.utc)
    values, raw = parse(expression)
    tz = ZoneInfo(zone)
    first_day = now.astimezone(tz).date()
    for offset in range(366 * 8):
        day = first_day + timedelta(days=offset)
        if day.month not in values[3]:
            continue
        dom = day.day in values[2]
        dow = (day.weekday() + 1) % 7 in values[4]
        if not ((dom and dow) if raw[2] == "*" or raw[4] == "*" else (dom or dow)):
            continue
        for hour in sorted(values[1]):
            for minute in sorted(values[0]):
                local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                instant = local.astimezone(timezone.utc)
                # A nonexistent spring-forward wall time must never be shifted.
                if instant > now and instant.astimezone(tz).replace(tzinfo=None) == local.replace(tzinfo=None):
                    return instant
    return None


def tick_activations(db, now):
    from sqlalchemy import select, text
    from app.models import Activation, ActivationRun, ScheduledRun
    from app.activations.service import enqueue_run, audit
    for activation in db.scalars(select(Activation).where(Activation.is_active, Activation.schedule.is_not(None))):
        if not matches(activation.schedule, activation.timezone, now):
            continue
        task = f"activation.cron:{activation.id}"
        # Same local wall-clock minute during a DST fold is one durable window.
        window = now.astimezone(ZoneInfo(activation.timezone)).strftime("%Y-%m-%dT%H:%M")
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"{task}:{window}"})
        if db.get(ScheduledRun, (task, window)):
            continue
        if db.scalar(text("""SELECT EXISTS(SELECT 1 FROM audit_log
          WHERE action='activation.schedule_failed' AND entity_type='activation'
          AND entity_id=:id AND details->>'window'=:window)"""),
          {"id": str(activation.id), "window": window}):
            continue
        try:
            run, job = enqueue_run(db, activation.id, triggered_by="schedule",
                                  dedupe_key=f"schedule:{task}:{window}")
        except ValueError:
            # Preserve a durable alert even if validation failed before enqueue.
            failed = ActivationRun(activation_id=activation.id, status="failed",
                                   triggered_by="schedule", finished_at=now,
                                   error="Scheduled activation could not be queued; verify destination credentials, audience and export approvals.")
            db.add(failed)
            db.flush()
            audit(db, "activation.schedule_failed", "activation", activation.id,
                  run_id=failed.id, window=window)
            continue
        db.add(ScheduledRun(task=task, window_key=window, job_id=job.id))
    # Queue recovery terminates a crashed single-attempt delivery job. Keep the
    # corresponding history honest rather than leaving "running" indefinitely.
    db.execute(text("""UPDATE activation_runs r SET status='failed',finished_at=now(),
      error='Worker interrupted. Review destination receipts before re-running.'
      FROM jobs j WHERE j.type='activation.run' AND j.status IN ('failed','cancelled')
      AND j.payload->>'run_id'=r.id::text AND r.status IN ('queued','running')"""))
