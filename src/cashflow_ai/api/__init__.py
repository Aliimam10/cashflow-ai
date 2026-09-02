"""Public local FastAPI application boundary."""

from cashflow_ai.api.app import create_app
from cashflow_ai.api.container import AppContainer, build_container

__all__ = ["AppContainer", "build_container", "create_app"]
