"""SQLite engine, session factory, and transaction management."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def _enable_sqlite_foreign_keys(
    dbapi_connection: Any,
    connection_record: Any,
) -> None:
    """Enable SQLite foreign-key enforcement for every DB-API connection."""
    del connection_record
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_sqlite_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create a SQLite-only SQLAlchemy engine with enforced foreign keys."""
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        msg = "CashFlow AI Version 1 supports SQLite database URLs only"
        raise ValueError(msg)
    engine = create_engine(url, echo=echo)
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create explicit sessions that do not expire loaded rows after commit."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """Commit one unit of work, rolling it back fully on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
