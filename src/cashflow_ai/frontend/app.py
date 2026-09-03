"""Streamlit application shell for the local CashFlow AI interface."""

from __future__ import annotations

from typing import Protocol

import streamlit as st

from cashflow_ai.config import load_settings
from cashflow_ai.frontend.client import ApiClient, ApiClientError, api_base_url
from cashflow_ai.frontend.components import (
    loading_state,
    render_empty_state,
    render_error,
    render_forecast_disclaimer,
    render_privacy_notice,
)
from cashflow_ai.frontend.navigation import NAVIGATION_ITEMS, NavigationItem, PageId
from cashflow_ai.frontend.session import (
    FrontendSessionState,
    load_session_state,
    save_session_state,
)
from cashflow_ai.schemas.api import HealthResponse, ReadinessResponse


class StatusApi(Protocol):
    """Narrow API surface required by the foundation home page."""

    def health(self) -> HealthResponse:
        """Return local API liveness."""
        ...

    def readiness(self) -> ReadinessResponse:
        """Return local database readiness."""
        ...


def render_home(client: StatusApi) -> None:
    """Render product purpose, safeguards, and local backend status."""
    st.title("CashFlow AI")
    st.caption("Local-first cash-flow forecasting and financial decision support")
    render_privacy_notice()
    render_forecast_disclaimer()

    try:
        with loading_state("Checking the local CashFlow AI service…"):
            health = client.health()
            readiness = client.readiness()
    except ApiClientError as error:
        render_error(error)
        st.caption("Start the backend with `make api`, then refresh this page.")
        return

    first, second = st.columns(2)
    first.metric("API", health.status)
    second.metric("Database", readiness.status.replace("_", " "))
    if readiness.status == "ready":
        st.success("The local backend and database schema are ready.")
    else:
        st.warning("The backend is running, but the database needs `make db-upgrade`.")

    st.subheader("Foundation status")
    st.write(
        "Navigation and the typed API connection are ready. Statement onboarding, "
        "transaction dashboards, and forecasting controls are introduced in the "
        "next staged commits."
    )


def render_placeholder(item: NavigationItem) -> None:
    """Render a truthful empty state without pulling future UI work forward."""
    st.title(f"{item.icon} {item.title}")
    render_empty_state("This area is not implemented yet", item.summary)
    if item.page_id is PageId.FORECAST_AND_PLANNING:
        render_forecast_disclaimer()


def selected_navigation_item(title: str) -> NavigationItem:
    """Resolve a sidebar title to its stable data-only navigation entry."""
    return next(item for item in NAVIGATION_ITEMS if item.title == title)


def render_application_page(item: NavigationItem, *, base_url: str) -> None:
    """Render the selected shell page while opening the API only when required."""
    if item.page_id is PageId.HOME:
        with ApiClient(base_url) as client:
            render_home(client)
        return
    render_placeholder(item)


def main() -> None:
    """Configure Streamlit, restore safe session state, and render one page."""
    settings = load_settings()
    st.set_page_config(
        page_title="CashFlow AI",
        page_icon="💷",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    current = load_session_state(st.session_state)
    titles = tuple(item.title for item in NAVIGATION_ITEMS)
    selected_index = next(
        index
        for index, item in enumerate(NAVIGATION_ITEMS)
        if item.page_id is current.selected_page
    )
    st.sidebar.title("CashFlow AI")
    selected_title = st.sidebar.radio(
        "Navigation",
        titles,
        index=selected_index,
    )
    st.sidebar.caption("Private, local and single-user by design.")
    selected = selected_navigation_item(selected_title)
    save_session_state(
        st.session_state,
        FrontendSessionState(
            selected_page=selected.page_id,
            user_profile_id=current.user_profile_id,
            account_id=current.account_id,
            privacy_notice_seen=True,
        ),
    )
    render_application_page(selected, base_url=api_base_url(settings))


if __name__ == "__main__":  # pragma: no cover - Streamlit script entry point
    main()
