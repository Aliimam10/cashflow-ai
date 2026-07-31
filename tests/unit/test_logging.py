"""Tests for centralized structured logging."""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Iterator

import pytest

from cashflow_ai.config import Environment, LogFormat, Settings
from cashflow_ai.logging import configure_logging


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root_logger = logging.getLogger()
    handlers = root_logger.handlers.copy()
    level = root_logger.level

    yield

    root_logger.handlers.clear()
    root_logger.handlers.extend(handlers)
    root_logger.setLevel(level)


def make_settings(
    *,
    log_format: LogFormat,
    log_level: str = "INFO",
) -> Settings:
    return Settings(
        environment=Environment.TEST,
        debug=False,
        log_level=log_level,
        log_format=log_format,
        timezone="UTC",
    )


def test_console_logging_is_human_readable(
    restore_root_logger: None,
) -> None:
    stream = io.StringIO()
    configure_logging(
        make_settings(log_format=LogFormat.CONSOLE),
        stream=stream,
    )

    logging.getLogger("cashflow_ai.test").info("service ready")

    output = stream.getvalue()
    assert "INFO cashflow_ai.test: service ready" in output
    assert output.startswith("20")


def test_json_logging_contains_only_approved_structured_fields(
    restore_root_logger: None,
) -> None:
    stream = io.StringIO()
    configure_logging(
        make_settings(log_format=LogFormat.JSON, log_level="DEBUG"),
        stream=stream,
    )

    logging.getLogger("cashflow_ai.test").debug(
        "import ready",
        extra={
            "event": "import_ready",
            "context": {"batch_id": 7},
            "private_description": "must not be copied",
        },
    )

    payload = json.loads(stream.getvalue())
    assert payload["level"] == "DEBUG"
    assert payload["logger"] == "cashflow_ai.test"
    assert payload["message"] == "import ready"
    assert payload["event"] == "import_ready"
    assert payload["context"] == {"batch_id": 7}
    assert "timestamp" in payload
    assert "private_description" not in payload


def test_json_logging_serializes_exceptions(
    restore_root_logger: None,
) -> None:
    stream = io.StringIO()
    configure_logging(
        make_settings(log_format=LogFormat.JSON),
        stream=stream,
    )

    try:
        raise ValueError("invalid example")
    except ValueError:
        logging.getLogger("cashflow_ai.test").exception("operation failed")

    payload = json.loads(stream.getvalue())
    assert payload["message"] == "operation failed"
    assert "ValueError: invalid example" in payload["exception"]
    assert "event" not in payload
    assert "context" not in payload


def test_reconfiguration_replaces_the_existing_root_handler(
    restore_root_logger: None,
) -> None:
    first_stream = io.StringIO()
    second_stream = io.StringIO()
    settings = make_settings(log_format=LogFormat.CONSOLE)

    configure_logging(settings, stream=first_stream)
    configure_logging(settings, stream=second_stream)
    logging.getLogger("cashflow_ai.test").info("once")

    assert first_stream.getvalue() == ""
    assert second_stream.getvalue().count("once") == 1
    assert len(logging.getLogger().handlers) == 1


def test_default_stream_is_standard_output(
    restore_root_logger: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(make_settings(log_format=LogFormat.CONSOLE))

    logging.getLogger("cashflow_ai.test").warning("visible")

    assert "visible" in capsys.readouterr().out
    assert logging.getLogger().level == logging.INFO
    assert logging.captureWarnings is not None
    assert sys.stdout is not None
