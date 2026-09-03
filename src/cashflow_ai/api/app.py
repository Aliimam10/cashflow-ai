"""FastAPI application factory for the local CashFlow AI backend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cashflow_ai import __version__
from cashflow_ai.api.container import AppContainer, build_container
from cashflow_ai.api.decision_routes import router as decision_router
from cashflow_ai.api.errors import register_exception_handlers
from cashflow_ai.api.routes import router

OPENAPI_TAGS = [
    {
        "name": "operations",
        "description": "Process liveness and local database readiness.",
    },
    {
        "name": "profiles",
        "description": "Single-user local profile setup.",
    },
    {
        "name": "accounts",
        "description": "Current/checking and savings account metadata.",
    },
    {
        "name": "ingestion",
        "description": (
            "Stateless CSV/PDF preview, explicit review, and confirmed CSV import."
        ),
    },
    {
        "name": "transactions",
        "description": "Verified transaction reads without raw source payloads.",
    },
    {
        "name": "analytics",
        "description": "Coverage, freshness, and role-aware cash-flow results.",
    },
    {
        "name": "recurring",
        "description": "Point-in-time recurring detection and explicit review.",
    },
    {
        "name": "categorisation",
        "description": "Category metadata, review queues, and explicit corrections.",
    },
    {
        "name": "financial roles",
        "description": "Advisory transfer/refund matching and explicit role decisions.",
    },
    {
        "name": "forecasts",
        "description": "Leakage-safe model evaluation and balance paths.",
    },
    {
        "name": "planning",
        "description": "Budgets, goals, and conservative safe-spending calculations.",
    },
    {
        "name": "scenarios",
        "description": "Read-only baseline-versus-hypothetical comparisons.",
    },
    {
        "name": "anomalies",
        "description": "Carefully worded unusual-transaction review signals.",
    },
    {
        "name": "models",
        "description": "Data-minimised evaluation and active-model information.",
    },
]


def create_app(container: AppContainer | None = None) -> FastAPI:
    """Create an app with explicit replaceable infrastructure dependencies."""
    resolved = container or build_container()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        yield
        resolved.engine.dispose()

    app = FastAPI(
        title=resolved.settings.app_name,
        version=__version__,
        description=(
            "Local-first API for profile/account setup and review-gated statement "
            "ingestion, analytics, forecasting, anomaly review, and planning."
        ),
        # Never return traceback text from a process handling financial inputs.
        debug=False,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        license_info={"name": "MIT"},
    )
    app.state.container = resolved
    register_exception_handlers(app)
    app.include_router(router)
    app.include_router(decision_router)
    return app


__all__ = ["OPENAPI_TAGS", "create_app"]
