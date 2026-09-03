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
        summary=(
            "Correct transactions, review suggestions, and inspect known-data "
            "analytics."
        ),
    ),
    NavigationItem(
        page_id=PageId.FORECAST_AND_PLANNING,
        title="Forecast & planning",
        icon="📈",
        summary=("Review recurring payments and inspect uncertainty-aware forecasts."),
    ),
)


def navigation_item(page_id: PageId) -> NavigationItem:
    """Return the stable metadata for one validated page identity."""
    return next(item for item in NAVIGATION_ITEMS if item.page_id is page_id)


__all__ = ["NAVIGATION_ITEMS", "NavigationItem", "PageId", "navigation_item"]
