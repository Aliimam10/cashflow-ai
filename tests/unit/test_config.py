"""Tests for typed application configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from cashflow_ai.config import Environment, LogFormat, load_settings

CONFIG_ENVIRONMENT_VARIABLES = (
    "CASHFLOW_ENVIRONMENT",
    "CASHFLOW_DEBUG",
    "CASHFLOW_LOG_LEVEL",
    "CASHFLOW_LOG_FORMAT",
    "CASHFLOW_TIMEZONE",
    "CASHFLOW_DATABASE_URL",
)


@pytest.fixture(autouse=True)
def clean_configuration_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in CONFIG_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def test_development_profile_is_the_default(tmp_path: Path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.app_name == "CashFlow AI"
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.debug is True
    assert settings.log_level == "DEBUG"
    assert settings.log_format is LogFormat.CONSOLE
    assert settings.timezone == "UTC"
    assert settings.database_url == "sqlite:///data/cashflow.db"


@pytest.mark.parametrize(
    ("environment", "debug", "log_level", "log_format"),
    [
        (Environment.TEST, False, "WARNING", LogFormat.CONSOLE),
        (Environment.PRODUCTION, False, "INFO", LogFormat.JSON),
    ],
)
def test_environment_profiles(
    environment: Environment,
    debug: bool,
    log_level: str,
    log_format: LogFormat,
    tmp_path: Path,
) -> None:
    settings = load_settings(
        environment,
        env_file=tmp_path / "missing.env",
    )

    assert settings.environment is environment
    assert settings.debug is debug
    assert settings.log_level == log_level
    assert settings.log_format is log_format


def test_environment_variables_override_profile_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CASHFLOW_ENVIRONMENT", "production")
    monkeypatch.setenv("CASHFLOW_DEBUG", "true")
    monkeypatch.setenv("CASHFLOW_LOG_LEVEL", "warning")
    monkeypatch.setenv("CASHFLOW_LOG_FORMAT", "console")
    monkeypatch.setenv("CASHFLOW_TIMEZONE", "Europe/London")
    monkeypatch.setenv("CASHFLOW_DATABASE_URL", "sqlite:///data/test.db")

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.environment is Environment.PRODUCTION
    assert settings.debug is True
    assert settings.log_level == "WARNING"
    assert settings.log_format is LogFormat.CONSOLE
    assert settings.timezone == "Europe/London"
    assert settings.database_url == "sqlite:///data/test.db"


def test_explicit_environment_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CASHFLOW_ENVIRONMENT", "production")

    settings = load_settings(
        Environment.TEST,
        env_file=tmp_path / "missing.env",
    )

    assert settings.environment is Environment.TEST
    assert settings.log_level == "WARNING"


def test_custom_dotenv_file_is_loaded(tmp_path: Path) -> None:
    env_file = tmp_path / "cashflow.env"
    env_file.write_text(
        "\n".join(
            [
                "CASHFLOW_ENVIRONMENT=test",
                "CASHFLOW_LOG_LEVEL=error",
                "CASHFLOW_TIMEZONE=Asia/Karachi",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)

    assert settings.environment is Environment.TEST
    assert settings.log_level == "ERROR"
    assert settings.timezone == "Asia/Karachi"


def test_default_environment_specific_file_overrides_base_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "CASHFLOW_ENVIRONMENT=production\nCASHFLOW_LOG_LEVEL=warning\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.production").write_text(
        "CASHFLOW_LOG_LEVEL=critical\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.environment is Environment.PRODUCTION
    assert settings.log_level == "CRITICAL"


def test_invalid_log_level_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CASHFLOW_LOG_LEVEL", "verbose")

    with pytest.raises(ValidationError, match="unsupported log level"):
        load_settings(env_file=tmp_path / "missing.env")


def test_invalid_timezone_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CASHFLOW_TIMEZONE", "Mars/Olympus")

    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        load_settings(env_file=tmp_path / "missing.env")


def test_non_sqlite_database_url_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CASHFLOW_DATABASE_URL", "postgresql://localhost/cashflow")

    with pytest.raises(ValidationError, match="must use local SQLite"):
        load_settings(env_file=tmp_path / "missing.env")
