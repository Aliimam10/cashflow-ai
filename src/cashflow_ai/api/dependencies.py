"""FastAPI dependency adapters for the application container."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, cast

from fastapi import Depends, Query, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.api.container import AppContainer
from cashflow_ai.imports import OcrEngine
from cashflow_ai.schemas.api import Pagination


def get_container(request: Request) -> AppContainer:
    """Return the container attached by the app factory."""
    return cast(AppContainer, request.app.state.container)


ContainerDependency = Annotated[AppContainer, Depends(get_container)]


def get_engine(container: ContainerDependency) -> Engine:
    """Provide the app-owned database engine."""
    return container.engine


def get_session_factory(
    container: ContainerDependency,
) -> sessionmaker[Session]:
    """Provide the app-owned transaction factory."""
    return container.session_factory


def get_ocr_engine_factory(
    container: ContainerDependency,
) -> Callable[[], OcrEngine]:
    """Provide a replaceable local OCR engine constructor."""
    return container.ocr_engine_factory


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Pagination:
    """Validate the shared bounded collection window."""
    return Pagination(limit=limit, offset=offset)


EngineDependency = Annotated[Engine, Depends(get_engine)]
SessionFactoryDependency = Annotated[
    sessionmaker[Session], Depends(get_session_factory)
]
OcrEngineFactoryDependency = Annotated[
    Callable[[], OcrEngine], Depends(get_ocr_engine_factory)
]
PaginationDependency = Annotated[Pagination, Depends(get_pagination)]

__all__ = [
    "ContainerDependency",
    "EngineDependency",
    "OcrEngineFactoryDependency",
    "PaginationDependency",
    "SessionFactoryDependency",
    "get_container",
    "get_engine",
    "get_ocr_engine_factory",
    "get_pagination",
    "get_session_factory",
]
