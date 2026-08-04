"""Typed application configuration and environment profiles."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX: Final = "CASHFLOW_"
VALID_LOG_LEVELS: Final = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


class Environment(StrEnum):
    """Supported application environments."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    """Supported logging output formats."""

    CONSOLE = "console"
    JSON = "json"


class _Profile(NamedTuple):
    debug: bool
    log_level: str
    log_format: LogFormat


PROFILES: Final = {
    Environment.DEVELOPMENT: _Profile(
        debug=True,
        log_level="DEBUG",
        log_format=LogFormat.CONSOLE,
    ),
    Environment.TEST: _Profile(
        debug=False,
        log_level="WARNING",
        log_format=LogFormat.CONSOLE,
    ),
    Environment.PRODUCTION: _Profile(
        debug=False,
        log_level="INFO",
        log_format=LogFormat.JSON,
    ),
}


class _EnvironmentInput(BaseSettings):
    """Minimal first-pass input used to select a profile."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.DEVELOPMENT


class _SettingsInput(BaseSettings):
    """Environment-backed values before profile defaults are resolved."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.DEVELOPMENT
    debug: bool | None = None
    log_level: str | None = None
    log_format: LogFormat | None = None
    timezone: str = "UTC"
    database_url: str = "sqlite:///data/cashflow.db"


class Settings(BaseModel):
    """Resolved, immutable application settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    app_name: str = "CashFlow AI"
    environment: Environment
    debug: bool
    log_level: str
    log_format: LogFormat
    timezone: str
    database_url: str = "sqlite:///data/cashflow.db"

    @field_validator("log_level")
    @classmethod
    def normalise_log_level(cls, value: str) -> str:
        """Return a validated standard-library log level."""
        normalised = value.upper()
        if normalised not in VALID_LOG_LEVELS:
            supported = ", ".join(sorted(VALID_LOG_LEVELS))
            msg = f"unsupported log level {value!r}; choose one of: {supported}"
            raise ValueError(msg)
        return normalised

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Require a valid IANA timezone name."""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            msg = f"unknown IANA timezone: {value!r}"
            raise ValueError(msg) from exc
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        """Restrict Version 1 persistence to local SQLite URLs."""
        if not value.startswith(("sqlite:///", "sqlite+pysqlite:///")):
            msg = "Version 1 database URL must use local SQLite"
            raise ValueError(msg)
        return value


def _env_files(
    environment: Environment,
    env_file: Path | None,
) -> tuple[Path, ...]:
    if env_file is not None:
        return (env_file,)
    return (Path(".env"), Path(f".env.{environment.value}"))


def load_settings(
    environment: Environment | None = None,
    *,
    env_file: Path | None = None,
) -> Settings:
    """Load settings from profile defaults, dotenv files, and the environment.

    Explicitly supplied environment selection takes precedence. Otherwise,
    `CASHFLOW_ENVIRONMENT` or the base `.env` file selects the profile. Process
    environment variables override dotenv values.
    """
    selection_files = (env_file,) if env_file is not None else (Path(".env"),)
    # `_env_file` is a documented BaseSettings runtime argument, but it is not
    # represented in the generated model constructor signature.
    selection = _EnvironmentInput(_env_file=selection_files)  # type: ignore[call-arg]
    selected_environment = environment or selection.environment

    raw = _SettingsInput(  # type: ignore[call-arg]
        _env_file=_env_files(selected_environment, env_file),
        environment=selected_environment,
    )
    profile = PROFILES[selected_environment]

    return Settings(
        environment=selected_environment,
        debug=profile.debug if raw.debug is None else raw.debug,
        log_level=profile.log_level if raw.log_level is None else raw.log_level,
        log_format=profile.log_format if raw.log_format is None else raw.log_format,
        timezone=raw.timezone,
        database_url=raw.database_url,
    )
