"""Streamlit application shell for the local CashFlow AI interface."""

from __future__ import annotations

from typing import Protocol, cast

import streamlit as st

from cashflow_ai.config import load_settings
from cashflow_ai.frontend.client import ApiClient, ApiClientError, api_base_url
from cashflow_ai.frontend.components import (
    loading_state,
    render_empty_state,
    render_error,
    render_forecast_disclaimer,
    render_page_header,
    render_privacy_notice,
    render_service_status,
)
from cashflow_ai.frontend.forecast_page import render_forecast_page
from cashflow_ai.frontend.import_page import render_import_page
from cashflow_ai.frontend.navigation import (
    NAVIGATION_ITEMS,
    NavigationItem,
    PageId,
    navigation_item,
)
from cashflow_ai.frontend.session import (
    FrontendSessionState,
    load_session_state,
    save_session_state,
)
from cashflow_ai.frontend.styles import apply_app_styles
from cashflow_ai.frontend.transaction_page import (
    TransactionApi,
    _dashboard_boundary_transactions,
    _render_dashboard,
    render_transaction_page,
)
from cashflow_ai.schemas.api import (
    AccountResponse,
    HealthResponse,
    Page,
    ReadinessResponse,
    UserProfileResponse,
)

_NAVIGATION_WIDGET_KEY = "cashflow_main_navigation"


class StatusApi(Protocol):
    """Local API surface required by the functional home dashboard."""

    def health(self) -> HealthResponse:
        """Return local API liveness."""
        ...

    def readiness(self) -> ReadinessResponse:
        """Return local database readiness."""
        ...

    def current_profile(self) -> UserProfileResponse:
        """Return the single local profile."""
        ...

    def list_accounts(self, profile_id: str) -> Page[AccountResponse]:
        """Return account metadata for the local profile."""
        ...


def render_home(client: StatusApi) -> None:
    """Render the real personal-finance dashboard after a quiet readiness check."""
    render_page_header(
        "Dashboard",
        "Your money at a glance.",
        "Track verified cash balances, monthly movement, and spending without "
        "sending financial data off this device.",
    )

    try:
        with loading_state("Checking the local CashFlow AI service…"):
            client.health()
            readiness = client.readiness()
    except ApiClientError as error:
        render_error(error)
        st.caption("Start CashFlow AI with `make api`, then refresh this page.")
        return

    ready = readiness.status == "ready"
    render_service_status(ready=ready)
    if not ready:
        st.caption("Run `make db-upgrade` once, then refresh this page.")
        return

    try:
        profile = client.current_profile()
        accounts = client.list_accounts(profile.profile_id).items
    except ApiClientError as error:
        if error.problem_code != "profile_not_found":
            render_error(error)
        else:
            render_empty_state(
                "Start your private dashboard",
                "Create a local profile, add an account, and review a statement.",
            )
            st.button(
                "Add your first statement",
                type="primary",
                on_click=_navigate_to,
                args=(PageId.IMPORT,),
            )
        return

    if not accounts:
        render_empty_state(
            "No accounts yet",
            "Add an account and confirm a statement to populate this dashboard.",
        )
        st.button(
            "Add a statement",
            type="primary",
            on_click=_navigate_to,
            args=(PageId.IMPORT,),
        )
        return

    transaction_client = cast(TransactionApi, client)
    try:
        with loading_state("Loading your verified financial history…"):
            boundaries = _dashboard_boundary_transactions(
                transaction_client,
                profile_id=profile.profile_id,
                accounts=accounts,
            )
    except ApiClientError as error:
        render_error(error)
        st.caption(
            "The dashboard could not load transaction boundaries. Your imported "
            "source rows were not changed."
        )
        return
    _render_dashboard(
        transaction_client,
        profile_id=profile.profile_id,
        accounts=accounts,
        transactions=boundaries,
    )
    actions = st.columns(2)
    actions[0].button(
        "Review all transactions",
        on_click=_navigate_to,
        args=(PageId.TRANSACTIONS,),
        use_container_width=True,
    )
    actions[1].button(
        "Add another statement",
        on_click=_navigate_to,
        args=(PageId.IMPORT,),
        use_container_width=True,
    )
    render_privacy_notice()
    render_forecast_disclaimer()


def _navigate_to(page_id: PageId) -> None:
    """Select a destination from an explicit in-app action."""
    st.session_state[_NAVIGATION_WIDGET_KEY] = navigation_item(page_id).title


def render_placeholder(item: NavigationItem) -> None:
    """Render a truthful empty state without pulling future UI work forward."""
    render_page_header("CashFlow AI", item.title, item.summary)
    render_empty_state("This area is not implemented yet", item.summary)
    if item.page_id is PageId.FORECAST_AND_PLANNING:
        render_forecast_disclaimer()


def selected_navigation_item(title: str) -> NavigationItem:
    """Resolve a sidebar title to its stable data-only navigation entry."""
    return next(item for item in NAVIGATION_ITEMS if item.title == title)


def render_application_page(
    item: NavigationItem,
    *,
    base_url: str,
    session: FrontendSessionState,
) -> FrontendSessionState:
    """Render the selected shell page while opening the API only when required."""
    if item.page_id is PageId.HOME:
        with ApiClient(base_url) as client:
            render_home(client)
        return session
    if item.page_id is PageId.IMPORT:
        with ApiClient(base_url) as client:
            return render_import_page(client, session)
    if item.page_id is PageId.TRANSACTIONS:
        with ApiClient(base_url) as client:
            return render_transaction_page(client, session)
    with ApiClient(base_url) as client:
        return render_forecast_page(client, session)


def main() -> None:
    """Configure Streamlit, restore safe session state, and render one page."""
    settings = load_settings()
    st.set_page_config(
        page_title="CashFlow AI",
        page_icon="💷",
        layout="wide",
        initial_sidebar_state="expanded",
        menu_items={
            "Get Help": None,
            "Report a bug": None,
            "About": "CashFlow AI · private, local cash-flow planning",
        },
    )
    apply_app_styles()
    current = load_session_state(st.session_state)
    titles = tuple(item.title for item in NAVIGATION_ITEMS)
    selected_index = next(
        index
        for index, item in enumerate(NAVIGATION_ITEMS)
        if item.page_id is current.selected_page
    )
    st.sidebar.markdown(
        (
            '<div class="cf-brand"><span class="cf-brand-mark">£</span>'
            '<span class="cf-brand-name">CashFlow AI</span></div>'
            '<div class="cf-menu-label">MAIN MENU</div>'
        ),
        unsafe_allow_html=True,
    )
    selected_title = st.sidebar.radio(
        "Main menu",
        titles,
        index=selected_index,
        format_func=lambda title: f"{selected_navigation_item(title).icon}  {title}",
        label_visibility="collapsed",
        key=_NAVIGATION_WIDGET_KEY,
    )
    st.sidebar.markdown(
        (
            '<div class="cf-sidebar-footer"><strong>Private on this device</strong>'
            "<br>Your financial data stays under your control.</div>"
        ),
        unsafe_allow_html=True,
    )
    selected = selected_navigation_item(selected_title)
    selected_state = FrontendSessionState(
        selected_page=selected.page_id,
        user_profile_id=current.user_profile_id,
        account_id=current.account_id,
        privacy_notice_seen=True,
    )
    updated = render_application_page(
        selected,
        base_url=api_base_url(settings),
        session=selected_state,
    )
    save_session_state(st.session_state, updated)


if __name__ == "__main__":  # pragma: no cover - Streamlit script entry point
    main()
