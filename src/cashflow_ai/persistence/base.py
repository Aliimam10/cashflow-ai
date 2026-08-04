"""Shared SQLAlchemy base, identifiers, and timezone-safe column types."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


def new_id() -> str:
    """Return a database-safe UUID string."""
    return str(uuid4())


def utc_now() -> datetime:
    """Return the current aware UTC time."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Store aware datetimes as naive UTC and restore UTC on retrieval."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """Convert an aware value to SQLite's timezone-neutral UTC form."""
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "database timestamps must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """Restore explicit UTC awareness to a stored value."""
        del dialect
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    """Declarative base for all CashFlow AI persistence models."""
