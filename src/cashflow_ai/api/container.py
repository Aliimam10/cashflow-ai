"""Explicit dependencies owned by one FastAPI application instance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.config import Settings, load_settings
from cashflow_ai.imports import OcrEngine, PytesseractOcrEngine
from cashflow_ai.persistence import create_session_factory, create_sqlite_engine


@dataclass(frozen=True, slots=True)
class AppContainer:
    """Settings and replaceable infrastructure used by API routes."""

    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    ocr_engine_factory: Callable[[], OcrEngine] = PytesseractOcrEngine


def build_container(settings: Settings | None = None) -> AppContainer:
    """Build default local SQLite dependencies without migrating the database."""
    resolved = settings or load_settings()
    engine = create_sqlite_engine(resolved.database_url, echo=False)
    return AppContainer(
        settings=resolved,
        engine=engine,
        session_factory=create_session_factory(engine),
    )


__all__ = ["AppContainer", "build_container"]
