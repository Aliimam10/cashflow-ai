"""Stable navigation metadata for the staged Streamlit interface."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PageId(StrEnum):
    """Frontend areas introduced incrementally by later commits."""

    HOME = "home"
    IMPORT = "import"
    TRANSACTIONS = "transactions"
    FORECAST_AND_PLANNING = "forecast_and_planning"


class NavigationItem(BaseModel):
    """Data-only navigation entry with no business behaviour."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_id: PageId
    title: str
    icon: str
    summary: str


NAVIGATION_ITEMS = (
    NavigationItem(
        page_id=PageId.HOME,
        title="Home",
        icon="🏠",
        summary="Local service status and product boundaries.",
    ),
    NavigationItem(
        page_id=PageId.IMPORT,
        title="Import statements",
        icon="📄",
        summary="Set up local accounts and review CSV, digital-PDF, or OCR extraction.",
    ),
    NavigationItem(
        page_id=PageId.TRANSACTIONS,
        title="Transactions & analytics",
        icon="📊",
        summary="Transaction review and dashboards arrive in Commit 34.",
    ),
    NavigationItem(
        page_id=PageId.FORECAST_AND_PLANNING,
        title="Forecast & planning",
        icon="📈",
        summary=(
            "Forecasts, budgets, goals, scenarios, and anomalies arrive in Commit 35."
        ),
    ),
)


def navigation_item(page_id: PageId) -> NavigationItem:
    """Return the stable metadata for one validated page identity."""
    return next(item for item in NAVIGATION_ITEMS if item.page_id is page_id)


__all__ = ["NAVIGATION_ITEMS", "NavigationItem", "PageId", "navigation_item"]
