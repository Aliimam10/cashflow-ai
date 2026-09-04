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
        title="Overview",
        icon="⌂",
        summary="Start here and see what CashFlow AI can help you do.",
    ),
    NavigationItem(
        page_id=PageId.IMPORT,
        title="Add a statement",
        icon="+",
        summary="Upload and review a CSV, digital PDF, or scanned statement.",
    ),
    NavigationItem(
        page_id=PageId.TRANSACTIONS,
        title="Transactions",
        icon="↕",
        summary="Review activity and understand where your money goes.",
    ),
    NavigationItem(
        page_id=PageId.FORECAST_AND_PLANNING,
        title="Forecast & plans",
        icon="⌁",
        summary="Look ahead, set budgets, and explore financial scenarios.",
    ),
)


def navigation_item(page_id: PageId) -> NavigationItem:
    """Return the stable metadata for one validated page identity."""
    return next(item for item in NAVIGATION_ITEMS if item.page_id is page_id)


__all__ = ["NAVIGATION_ITEMS", "NavigationItem", "PageId", "navigation_item"]
