from datetime import datetime, timezone


def _time(value):
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def consent_wins(old, new, old_authoritative=False, new_authoritative=False):
    """Same precedence as consent_candidate_wins in migration 0026.

    Preserve an authoritative opt-in's provenance against non-authoritative
    same-status updates, so replaying an old opt-out cannot undo that choice.
    Unknown is never evidence of permission and cannot clear a known decision.
    """
    if not old:
        return True
    ns, os = new["status"], old["status"]
    nt, ot = _time(new.get("captured_at")), _time(old.get("captured_at"))
    if ns == "unknown":
        return os == "unknown" and nt > ot
    if os == "unknown":
        return True
    if ns == "opted_out":
        return nt >= ot if os == "opted_in" and old_authoritative else os != "opted_out" or nt > ot
    if os == "opted_out":
        return bool(new_authoritative and nt > ot)
    if old_authoritative and not new_authoritative:
        return False
    return nt > ot or (nt == ot and new_authoritative and not old_authoritative)
