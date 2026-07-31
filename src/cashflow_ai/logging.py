"""Centralized standard-library logging configuration."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Final, TextIO

from cashflow_ai.config import LogFormat, Settings

LOG_LEVEL_VALUES: Final = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


class JsonFormatter(logging.Formatter):
    """Format approved log record fields as one-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record without copying arbitrary extra fields."""
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = event

        context = getattr(record, "context", None)
        if context is not None:
            payload["context"] = context

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


class UtcConsoleFormatter(logging.Formatter):
    """Render readable console logs with UTC timestamps."""

    @staticmethod
    def converter(timestamp: float | None) -> time.struct_time:
        """Convert a logging timestamp to UTC."""
        return time.gmtime(timestamp)


def configure_logging(
    settings: Settings,
    *,
    stream: TextIO | None = None,
) -> None:
    """Configure the root logger exactly once per call."""
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)

    if settings.log_format is LogFormat.JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            UtcConsoleFormatter(
                fmt="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(LOG_LEVEL_VALUES[settings.log_level])
    logging.captureWarnings(True)
