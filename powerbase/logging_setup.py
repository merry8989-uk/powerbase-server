"""Structured logging.

Two formats from one formatter: a readable key=value line for a human watching
a terminal, and one JSON object per line for anything that ships logs somewhere.
The switch is `POWERBASE_LOG_JSON`.

Every record can carry a `fields` mapping via `extra={"fields": {...}}`, which is
how the access log attaches the request's app and key without string-formatting
them into the message.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

LOGGER_NAME = "powerbase"
ACCESS_LOGGER_NAME = "powerbase.http"


class StructuredFormatter(logging.Formatter):
    def __init__(self, json_format: bool = False):
        super().__init__()
        self.json_format = json_format

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None) or {}
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        message = record.getMessage()

        if self.json_format:
            payload: dict = {
                "ts": timestamp,
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
            payload.update(fields)
            if record.exc_info:
                payload["error"] = self.formatException(record.exc_info)
            return json.dumps(payload, default=str, ensure_ascii=False)

        line = f"{timestamp} {record.levelname:<7} {record.name}: {message}"
        if fields:
            line += " " + " ".join(f"{key}={value}" for key, value in fields.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(
    level: str = "INFO",
    json_format: bool = False,
    stream=None,
) -> logging.Logger:
    """Attach one structured handler to the `powerbase` logger."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(StructuredFormatter(json_format))
    logger.handlers = [handler]
    # uvicorn configures the root logger; keeping our own handler means the
    # format is ours and a duplicate line never appears.
    logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(name or LOGGER_NAME)
