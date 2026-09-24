import json
import logging
import re
from pathlib import Path
from datetime import datetime, timezone
from sqlalchemy.exc import DataError, IntegrityError, OperationalError, ProgrammingError

SENSITIVE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|\+?\d[\d\s().-]{8,}\d", re.I)

SQLSTATE_MESSAGES = {
    "23502": "A required database field was missing",
    "23503": "A referenced database record does not exist",
    "23505": "A database uniqueness constraint was violated",
    "23514": "A database check constraint was violated",
    "40001": "Transaction serialization failed; retry the job",
    "40P01": "Deadlock detected; transaction was rolled back; retry the job",
    "55P03": "Database lock could not be acquired; retry the job",
    "57014": "Database query was cancelled",
    "08000": "Database connection failed",
    "08003": "Database connection is closed",
    "08006": "Database connection was lost",
}


def _exceptions(error: BaseException):
    """Walk explicit/implicit causes and DBAPI wrappers without formatting them."""
    seen = set()

    def visit(current):
        if id(current) in seen or not isinstance(current, BaseException):
            return
        seen.add(id(current))
        if current.__cause__ is not None:
            yield from visit(current.__cause__)
        elif not current.__suppress_context__ and current.__context__ is not None:
            yield from visit(current.__context__)
        original = getattr(current, "orig", None)
        if original is not None and original is not current:
            yield from visit(original)
        yield current

    yield from visit(error)


def _safe_message(error: BaseException) -> str:
    # Never interpolate str(error), DBAPI diagnostics, args, SQL or parameter
    # values: even apparently harmless ValueError/KeyError text may be input.
    for item in reversed(list(_exceptions(error))):
        state = getattr(item, "sqlstate", None) or getattr(item, "pgcode", None)
        if isinstance(state, str) and state in SQLSTATE_MESSAGES:
            return SQLSTATE_MESSAGES[state]
    for item in _exceptions(error):
        if isinstance(item, IntegrityError):
            return "A database integrity constraint was violated"
        if isinstance(item, DataError):
            return "Database rejected an invalid data value"
        if isinstance(item, OperationalError):
            return "Database operation failed; check connectivity or retry"
        if isinstance(item, ProgrammingError):
            return "Database query failed; check application configuration"
    if isinstance(error, (TimeoutError,)):
        return "Operation timed out"
    if isinstance(error, (ConnectionError,)):
        return "Connection failed"
    if isinstance(error, FileNotFoundError):
        return "Required file was not found"
    if isinstance(error, PermissionError):
        return "Permission denied while processing the job"
    if isinstance(error, KeyError):
        return "A required job field was missing"
    if isinstance(error, (ValueError, TypeError)):
        return "Invalid job input or configuration"
    return "Job handler raised an unexpected error"


def _frames(error: BaseException) -> list[dict]:
    frames = []
    tb = error.__traceback__
    while tb:
        code = tb.tb_frame.f_code
        frames.append({"file": Path(code.co_filename).name,
                       "function": code.co_name, "line": tb.tb_lineno})
        tb = tb.tb_next
    return frames


def safe_traceback(error: BaseException) -> str:
    """Full exception chain and frame locations, without source or locals."""
    lines = ["Traceback (sanitized; source lines and locals omitted):"]
    for item in _exceptions(error):
        for frame in _frames(item):
            lines.append(f'  File "{frame["file"]}", line {frame["line"]}, in {frame["function"]}')
        lines.append(f"{type(item).__name__}: {_safe_message(item)}")
    return "\n".join(lines)


def safe_exception(error: BaseException) -> dict:
    """Safe structured exception and full sanitized traceback."""
    result = {"class": type(error).__name__, "message": _safe_message(error)}
    original = getattr(error, "orig", error)
    if original is not error:
        result["driver_class"] = type(original).__name__
    for item in reversed(list(_exceptions(error))):
        state = getattr(item, "sqlstate", None) or getattr(item, "pgcode", None)
        if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state):
            result["sqlstate"] = state
            break
    # DB identifiers can themselves contain user data. Only schema-owned names
    # are safe; do not emit arbitrary diag.constraint_name values.
    from app.models import Base
    known = {obj.name for table in Base.metadata.tables.values()
             for obj in (*table.constraints, *table.indexes) if obj.name}
    for item in reversed(list(_exceptions(error))):
        constraint = getattr(getattr(item, "diag", None), "constraint_name", None)
        if constraint in known:
            result["constraint"] = constraint
            break
    result["frames"] = _frames(error)
    result["traceback"] = safe_traceback(error)
    return result


def safe_job_error(error: BaseException) -> str:
    info = safe_exception(error)
    parts = [f"Job handler failed: {info['class']}: {info['message']}"]
    for key in ("driver_class", "sqlstate", "constraint"):
        if key in info:
            parts.append(f"{key}={info[key]}")
    if info["frames"]:
        frame = info["frames"][-1]
        parts.append(f"at {frame['file']}:{frame['line']} ({frame['function']})")
    return "; ".join(parts)[:500]


class Redact(logging.Filter):
    def filter(self, record):
        record.msg = SENSITIVE.sub("[REDACTED]", str(record.getMessage()))
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = {"at": datetime.now(timezone.utc).isoformat(),
                  "level": record.levelname, "logger": record.name,
                  "message": SENSITIVE.sub("[REDACTED]", record.getMessage())}
        if record.exc_info and record.exc_info[1]:
            result["exception"] = safe_exception(record.exc_info[1])
        return json.dumps(result)


def configure_logging(level: str):
    handler = logging.StreamHandler()
    handler.addFilter(Redact())
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)