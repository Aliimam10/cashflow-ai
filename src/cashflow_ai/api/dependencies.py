"""FastAPI dependency adapters for the application container."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.api.container import AppContainer
from cashflow_ai.imports import OcrEngine


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


EngineDependency = Annotated[Engine, Depends(get_engine)]
SessionFactoryDependency = Annotated[
    sessionmaker[Session], Depends(get_session_factory)
]
OcrEngineFactoryDependency = Annotated[
    Callable[[], OcrEngine], Depends(get_ocr_engine_factory)
]

__all__ = [
    "ContainerDependency",
    "EngineDependency",
    "OcrEngineFactoryDependency",
    "SessionFactoryDependency",
    "get_container",
    "get_engine",
    "get_ocr_engine_factory",
    "get_session_factory",
]
