"""Read-only pull adapters. Never log transport exceptions or returned records."""
import fnmatch
import json
import re
import stat
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from app.identity.filedrop import connect as sftp_connect

MAX_LINES = 500_000
MAX_LINE_BYTES = 1024 * 1024
FEEDS = {"people", "models", "events", "resources", "enrichment", "lookups"}
DEFAULT_SCHEDULES = {
    "people": {"frequency": "hourly", "timezone": "America/New_York"},
    "events": {"frequency": "daily", "hour": 21, "timezone": "America/New_York"},
    "resources": {"frequency": "daily", "hour": 5, "timezone": "America/New_York"},
    "models": {"frequency": "monthly", "day": 1, "hour": 5, "timezone": "America/New_York"},
    "enrichment": {"frequency": "monthly", "day": 1, "hour": 5, "timezone": "America/New_York"},
    "lookups": {"frequency": "hourly", "timezone": "America/New_York"},
}
RESOURCES = {"source_codes", "products", "vod_titles"}


class SourceError(ValueError):
    """Only constant safe messages cross the worker/API boundary."""


def jsonl_rows(stream):
    for number in range(MAX_LINES + 1):
        line = stream.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        if number == MAX_LINES:
            raise SourceError("JSONL files must not exceed 500,000 lines.")
        if len(line) > MAX_LINE_BYTES:
            raise SourceError("JSONL row exceeds the 1 MiB limit.")
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            raise SourceError("JSONL contains invalid JSON.") from None
        if not isinstance(row, dict):
            raise SourceError("Each JSONL row must be an object.")
        yield row


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}", value):
        raise SourceError("Invalid Snowflake identifier.")
    return value


def readonly_query(value):
    """Deliberately restricted SELECT grammar, not an arbitrary SQL console.

    No calls/UDFs, comments, literals, CTEs or statement separators. Configured
    query supports projection, joins and ordinary comparisons on identifiers.
    Incremental predicates/limits are added by us with bound parameters.
    """
    if not isinstance(value, str) or len(value) > 10000:
        raise SourceError("Snowflake query must be a read-only SELECT.")
    if not re.fullmatch(r"[A-Za-z0-9_.*,\s=<>!]+", value) or not re.match(r"^\s*SELECT\s+", value, re.I):
        raise SourceError("Snowflake query must be a single restricted SELECT without functions or literals.")
    words = set(re.findall(r"[A-Za-z_]+", value.upper()))
    forbidden = {"INSERT", "UPDATE", "DELETE", "MERGE", "COPY", "PUT", "GET", "CALL",
                 "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE", "EXECUTE",
                 "USE", "SET", "UNSET", "REMOVE", "INTO", "UNION", "INTERSECT", "EXCEPT"}
    if words & forbidden or "FROM" not in words:
        raise SourceError("Snowflake query contains a forbidden operation.")
    return value.strip()


def validate_config(kind, config):
    if kind not in {"sftp_jsonl", "snowflake", "zeta_exit"}:
        raise SourceError("Source kind does not support pulling.")
    if not isinstance(config, dict) or set(config) - {"mappings", "schedules"}:
        raise SourceError("Unknown source configuration field.")
    mappings = config.get("mappings", [])
    if not isinstance(mappings, list) or not 1 <= len(mappings) <= 50:
        raise SourceError("Configure between 1 and 50 feed mappings.")
    seen = set()
    for mapping in mappings:
        if not isinstance(mapping, dict) or set(mapping) - {"id", "feed", "folder", "pattern", "table", "query", "cursor_column", "resource_type"}:
            raise SourceError("Invalid feed mapping.")
        key = mapping.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) or key in seen:
            raise SourceError("Mapping IDs must be unique lowercase identifiers.")
        seen.add(key)
        if mapping.get("feed") not in FEEDS:
            raise SourceError("Unsupported feed.")
        if mapping.get("feed") == "resources" and mapping.get("resource_type") not in RESOURCES:
            raise SourceError("Resource mappings require a supported resource_type.")
        if kind == "snowflake":
            if bool(mapping.get("table")) == bool(mapping.get("query")):
                raise SourceError("Choose exactly one Snowflake table or query.")
            if mapping.get("table"):
                identifier(mapping["table"])
            else:
                readonly_query(mapping["query"])
            if mapping.get("cursor_column"):
                identifier(mapping["cursor_column"])
                if "." in mapping["cursor_column"]:
                    raise SourceError("Cursor must be an unqualified column.")
        else:
            if mapping.get("folder") not in {"/users/", "/resources/", "/events_non_real_time/"}:
                raise SourceError("SFTP mapping folder must use the Zeta folder layout.")
            pattern = mapping.get("pattern", "*.jsonl")
            if not isinstance(pattern, str) or len(pattern) > 180 or "/" in pattern or "\\" in pattern:
                raise SourceError("Invalid SFTP filename pattern.")
    schedules = config.get("schedules", {})
    if not isinstance(schedules, dict) or set(schedules) - FEEDS:
        raise SourceError("Invalid feed schedules.")
    for feed, schedule in schedules.items():
        if not isinstance(schedule, dict) or set(schedule) - {"frequency", "hour", "day", "timezone"}:
            raise SourceError("Invalid schedule fields.")
        if schedule.get("frequency") not in {"hourly", "daily", "monthly", "manual"}:
            raise SourceError("Unsupported schedule frequency.")
        if schedule.get("timezone", "America/New_York") != "America/New_York":
            raise SourceError("Schedules use America/New_York.")
        if type(schedule.get("hour", 0)) is not int or not 0 <= schedule.get("hour", 0) <= 23:
            raise SourceError("Invalid schedule hour.")
        if type(schedule.get("day", 1)) is not int or not 1 <= schedule.get("day", 1) <= 28:
            raise SourceError("Monthly schedule day must be between 1 and 28.")
    return config


def window(schedule, now: datetime):
    local = now.astimezone(ZoneInfo("America/New_York"))
    frequency = schedule["frequency"]
    if frequency == "manual":
        return None
    if frequency == "hourly":
        return local.strftime("%Y-%m-%dT%H%z")
    if local.hour < schedule.get("hour", 0):
        return None
    if frequency == "monthly":
        return local.strftime("%Y-%m") if local.day >= schedule.get("day", 1) else None
    return local.strftime("%Y-%m-%d")


def validate_credentials(kind, credentials):
    if not isinstance(credentials, dict):
        raise SourceError("Credentials must be an object.")
    if kind in {"sftp_jsonl", "zeta_exit"}:
        if set(credentials) - {"host", "port", "username", "private_key", "private_key_passphrase", "host_key_sha256"}:
            raise SourceError("Unsupported SFTP credentials field.")
        from app.identity.filedrop import validate_config as check
        check({**credentials, "input_dir": "/users", "output_dir": "/lookup-output"})
    else:
        if set(credentials) - {"account", "user", "private_key", "private_key_passphrase", "warehouse", "database", "schema", "role"}:
            raise SourceError("Unsupported Snowflake credentials field.")
        if not all(isinstance(credentials.get(k), str) and credentials[k] for k in ("account", "user", "private_key", "role")):
            raise SourceError("Snowflake account, user, private key and read-only role are required.")
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){0,3}", credentials["account"]):
            raise SourceError("Invalid Snowflake account.")
        for key in ("role", "warehouse", "database", "schema"):
            if credentials.get(key):
                identifier(credentials[key])


@contextmanager
def snowflake_connection(credentials, factory=None):
    validate_credentials("snowflake", credentials)
    connection = None
    try:
        if factory is None:
            try:
                import snowflake.connector
            except ImportError:
                raise SourceError("Install snowflake-connector-python to enable Snowflake sources.") from None
            factory = snowflake.connector.connect
        from cryptography.hazmat.primitives.serialization import load_pem_private_key, Encoding, PrivateFormat, NoEncryption
        password = credentials.get("private_key_passphrase")
        key = load_pem_private_key(credentials["private_key"].encode(), password=password.encode() if password else None)
        options = {k: v for k, v in credentials.items() if k not in {"private_key", "private_key_passphrase"}}
        connection = factory(**options, private_key=key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption()),
                             login_timeout=15, network_timeout=30,
                             session_parameters={"QUERY_TAG": "kinship_readonly_source", "STATEMENT_TIMEOUT_IN_SECONDS": 600})
        yield connection
    except SourceError:
        raise
    except Exception:
        raise SourceError("Snowflake read failed; verify the key-pair credentials and SELECT-only role.") from None
    finally:
        if connection is not None:
            connection.close()


def snowflake_rows(connection, mapping, cursor_value=None, limit=None):
    parameters = []
    if mapping.get("table"):
        query = "SELECT * FROM IDENTIFIER(%s)"
        parameters.append(identifier(mapping["table"]))
    else:
        query = "SELECT * FROM (" + readonly_query(mapping["query"]) + ") AS source_rows"
    cursor = mapping.get("cursor_column")
    if cursor and cursor_value is not None:
        # Inclusive boundary intentionally rereads ties; import/event receipts
        # make retries safe and prevent loss of rows sharing a timestamp.
        query += " WHERE IDENTIFIER(%s) >= %s"
        parameters.extend([identifier(cursor), cursor_value])
    if cursor:
        query += " ORDER BY IDENTIFIER(%s)"
        parameters.append(identifier(cursor))
    if limit is not None:
        query += " LIMIT %s"
        parameters.append(limit)
    with connection.cursor() as result:
        result.execute(query, tuple(parameters))
        names = [col[0].lower() for col in result.description]
        emitted = 0
        while True:
            rows = result.fetchmany(1000 if limit is None else min(limit, 100))
            if not rows:
                return
            for row in rows:
                if limit is not None and emitted >= limit:
                    return
                emitted += 1
                yield dict(zip(names, row))


@contextmanager
def sftp_connection(credentials):
    validate_credentials("sftp_jsonl", credentials)
    with sftp_connect({**credentials, "input_dir": "/users", "output_dir": "/lookup-output"}) as remote:
        yield remote


def remote_files(remote, mapping):
    folder = mapping["folder"]
    for entry in sorted(remote.listdir_attr(folder), key=lambda x: x.filename):
        name = entry.filename
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}", name):
            continue
        if not fnmatch.fnmatchcase(name, mapping.get("pattern", "*.jsonl")):
            continue
        path = folder + name
        info = remote.lstat(path)
        if stat.S_ISREG(info.st_mode):
            yield path, info
