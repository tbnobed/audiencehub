import json
import logging
import re
from datetime import datetime, timezone

SENSITIVE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|\+?\d[\d\s().-]{8,}\d", re.I)


class Redact(logging.Filter):
    def filter(self, record):
        record.msg = SENSITIVE.sub("[REDACTED]", str(record.getMessage()))
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                           "level": record.levelname, "logger": record.name,
                           "message": SENSITIVE.sub("[REDACTED]", record.getMessage())})


def configure_logging(level: str):
    handler = logging.StreamHandler()
    handler.addFilter(Redact())
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)