"""Streamlit pages backed only by a finalized statement workspace."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

import streamlit as st

from cashflow_ai.frontend.client import ApiClientError
from cashflow_ai.frontend.components import (
    loading_state,
    render_empty_state,
    render_error,
    render_forecast_disclaimer,
    render_page_header,
    render_privacy_notice,
)
from cashflow_ai.frontend.navigation import PageId
from cashflow_ai.frontend.session import FrontendSessionState
from cashflow_ai.frontend.workspace_results_workflow import (
    analytics_messages,
    category_donut_chart,
    coverage_chart,
    forecast_chart,
    forecast_reason_messages,
    forecast_warning_messages,
    money_text,
    monthly_cash_flow_chart,
    transaction_rows,
    workspace_balance_hero_html,
)
from cashflow_ai.schemas.api import Pagination
from cashflow_ai.schemas.workspace_analytics import (
    WorkspaceAnalytics,
    WorkspaceAnalyticsRequest,
    WorkspaceTransactionSearchRequest,
    WorkspaceTransactionSearchResult,
)
from cashflow_ai.schemas.workspace_forecasts import (
    WorkspaceForecastAvailable,
    WorkspaceForecastHorizon,
    WorkspaceForecastRequest,
    WorkspaceForecastResult,
)


class WorkspaceResultsApi(Protocol):
    """Narrow server-owned result surface used by the normal dashboard."""

    def workspace_analytics(
        self,
        workspace_id: str,
        request: WorkspaceAnalyticsRequest,
    ) -> WorkspaceAnalytics:
        """Return coverage-aware finalized-workspace analytics."""
        ...

    def search_workspace_transactions(
        self,
        workspace_id: str,
        request: WorkspaceTransactionSearchRequest,
    ) -> WorkspaceTransactionSearchResult:
        """Return approved rows without consulting the legacy demo database."""
        ...

    def workspace_forecast(
        self,
        workspace_id: str,
        request: WorkspaceForecastRequest,
    ) -> WorkspaceForecastResult:
        """Return an available forecast or controlled withholding reasons."""
        ...


type NavigationCallback = Callable[[PageId], None]


def _workspace_identity(session: FrontendSessionState) -> tuple[str, int] | None:
    if session.workspace_id is None or session.workspace_revision is None:
        return None
    return session.workspace_id, session.workspace_revision


def _load_analytics(
    client: WorkspaceResultsApi,
    session: FrontendSessionState,
) -> WorkspaceAnalytics | None:
    identity = _workspace_identity(session)
    if identity is None:
        return None
    workspace_id, revision = identity
    with loading_state("Calculating from your approved statement table…"):
        return client.workspace_analytics(
            workspace_id,
            WorkspaceAnalyticsRequest(expected_workspace_revision=revision),
        )


def _render_messages(analytics: WorkspaceAnalytics) -> None:
    for message in analytics_messages(analytics):
        st.warning(message)


def _render_totals(analytics: WorkspaceAnalytics) -> None:
    totals = analytics.totals
    if totals is None:
        render_empty_state(
            "Totals are withheld",
            "Confirm statement coverage before using income and spending totals.",
        )
        return
    columns = st.columns(4)
    columns[0].metric(
        "Income",
        money_text(totals.total_income, analytics.currency),
    )
    columns[1].metric(
        "Spending",
        money_text(totals.total_expenses, analytics.currency),
    )
    columns[2].metric(
        "Net cash flow",
        money_text(totals.net_cash_flow, analytics.currency, signed=True),
    )
    columns[3].metric(
        "Transfers",
        money_text(totals.net_transfer_movement, analytics.currency, signed=True),
        help="Transfers are shown separately and are not counted as spending.",
    )
    st.caption(
        f"{totals.basis.value.replace('_', ' ').title()} · "
        f"{totals.transaction_count} approved transaction(s)"
    )


def _render_charts(analytics: WorkspaceAnalytics) -> None:
    chart_columns = st.columns(2)
    with chart_columns[0]:
        st.subheader("Spending by category")
        if analytics.category_spending:
            st.vega_lite_chart(
                category_donut_chart(analytics),
                use_container_width=True,
            )
            st.dataframe(
                [
                    {
                        "Category": item.category_name or "Other",
                        "Spending": money_text(item.amount, analytics.currency),
                        "Transactions": item.transaction_count,
                    }
                    for item in analytics.category_spending
                ],
                hide_index=True,
                use_container_width=True,
            )
        else:
            render_empty_state(
                "No confirmed expense rows",
                "Income, transfers, refunds, and unknown roles are not expenses.",
            )
    with chart_columns[1]:
        st.subheader("Cash flow over time")
        if any(month.totals is not None for month in analytics.monthly_cash_flow):
            st.vega_lite_chart(
                monthly_cash_flow_chart(analytics),
                use_container_width=True,
            )
        else:
            render_empty_state(
                "No covered cash-flow period",
                "Confirm coverage before comparing income and spending over time.",
            )
    st.subheader("Statement coverage")
    st.vega_lite_chart(
        coverage_chart(analytics.coverage),
        use_container_width=True,
    )
    st.caption(
        "Only confirmed covered dates are treated as known. Missing dates are never "
        "filled with zero spending."
    )


def _recent_transactions(
    client: WorkspaceResultsApi,
    session: FrontendSessionState,
) -> WorkspaceTransactionSearchResult | None:
    identity = _workspace_identity(session)
    if identity is None:
        return None
    workspace_id, revision = identity
    return client.search_workspace_transactions(
        workspace_id,
        WorkspaceTransactionSearchRequest(
            expected_workspace_revision=revision,
            pagination=Pagination(limit=8, offset=0),
        ),
    )


def render_workspace_overview(
    client: WorkspaceResultsApi,
    session: FrontendSessionState,
    *,
    navigate: NavigationCallback,
) -> FrontendSessionState:
    """Render the finalized account overview without legacy database reads."""
    render_page_header(
        "Overview",
        "Your cash flow at a glance.",
        "See verified balances, role-aware spending, and the exact dates covered "
        "by your approved statements.",
    )
    try:
        analytics = _load_analytics(client, session)
        if analytics is None:
            raise RuntimeError("workspace identity unavailable")
        recent = _recent_transactions(client, session)
    except ApiClientError as error:
        render_error(error)
        return session
    except RuntimeError:
        render_empty_state(
            "No finalized statement workspace",
            "Upload and approve your statements before opening the overview.",
        )
        st.button(
            "Upload bank statements",
            type="primary",
            on_click=navigate,
            args=(PageId.IMPORT,),
        )
        return session

    _render_messages(analytics)
    st.markdown(workspace_balance_hero_html(analytics), unsafe_allow_html=True)
    _render_totals(analytics)
    _render_charts(analytics)
    st.subheader("Recent transactions")
    if recent is not None and recent.items:
        st.dataframe(
            transaction_rows(recent),
            hide_index=True,
            use_container_width=True,
        )
        st.button(
            "View all transactions",
            on_click=navigate,
            args=(PageId.TRANSACTIONS,),
        )
    else:
        render_empty_state(
            "No approved transactions",
            "The finalized table does not contain rows in this period.",
        )
    render_privacy_notice()
    return session


def render_workspace_transactions(
    client: WorkspaceResultsApi,
    session: FrontendSessionState,
    *,
    navigate: NavigationCallback,
) -> FrontendSessionState:
    """Render the approved canonical table as a read-only result view."""
    render_page_header(
        "Transactions",
        "Understand where your money goes.",
        "Browse the table you approved. Transfers and refunds keep their own "
        "financial roles instead of being reported as spending.",
    )
    identity = _workspace_identity(session)
    if identity is None:
        render_empty_state(
            "No finalized statement workspace",
            "Upload and approve your statements before viewing transactions.",
        )
        st.button(
            "Upload bank statements",
            type="primary",
            on_click=navigate,
            args=(PageId.IMPORT,),
        )
        return session
    workspace_id, revision = identity
    try:
        with loading_state("Loading your approved transaction table…"):
            result = client.search_workspace_transactions(
                workspace_id,
                WorkspaceTransactionSearchRequest(
                    expected_workspace_revision=revision,
                    pagination=Pagination(limit=100, offset=0),
                ),
            )
    except ApiClientError as error:
        render_error(error)
        return session

    st.info(
        "This is the finalized table. To change a row, start a new statement "
        "workspace and approve the corrected table."
    )
    if result.items:
        st.dataframe(
            transaction_rows(result),
            hide_index=True,
            use_container_width=True,
        )
        shown = len(result.items)
        st.caption(f"Showing {shown} of {result.total} approved transaction(s).")
        if result.total > shown:
            st.warning(
                "This screen shows the newest 100 rows. Download the final CSV from "
                "Bank statements to inspect the complete approved table."
            )
    else:
        render_empty_state(
            "No approved transactions",
            "The finalized workspace contains no included transaction rows.",
        )
    st.button(
        "Open bank statements",
        on_click=navigate,
        args=(PageId.IMPORT,),
    )
    return session


def render_workspace_forecast(
    client: WorkspaceResultsApi,
    session: FrontendSessionState,
) -> FrontendSessionState:
    """Render one conservative exact-horizon balance forecast or refusal."""
    render_page_header(
        "Forecast",
        "Look ahead without hiding uncertainty.",
        "Project your approved account balance only when recent history, coverage, "
        "financial roles, and balance evidence are trustworthy.",
    )
    render_forecast_disclaimer()
    identity = _workspace_identity(session)
    if identity is None:
        render_empty_state(
            "No finalized statement workspace",
            "Upload and approve statements before asking for a balance forecast.",
        )
        return session
    horizon = st.radio(
        "Forecast horizon",
        (15, 30, 60, 90),
        horizontal=True,
        format_func=lambda days: f"{days} days",
    )
    workspace_id, revision = identity
    try:
        with loading_state("Checking whether the evidence supports a forecast…"):
            result = client.workspace_forecast(
                workspace_id,
                WorkspaceForecastRequest(
                    expected_workspace_revision=revision,
                    horizon_days=cast(WorkspaceForecastHorizon, horizon),
                ),
            )
    except ApiClientError as error:
        render_error(error)
        return session

    if not isinstance(result, WorkspaceForecastAvailable):
        st.warning("A balance forecast is not responsible with the current evidence.")
        for message in forecast_reason_messages(result.reasons):
            st.markdown(f"- {message}")
        return session

    final_point = result.daily_balances[-1]
    columns = st.columns(3)
    columns[0].metric(
        "Estimated balance today",
        money_text(result.expected_balance_as_of_today, result.currency),
    )
    columns[1].metric(
        f"Estimated in {result.horizon_days} days",
        money_text(final_point.expected_balance, result.currency),
    )
    columns[2].metric(
        "Estimated range",
        (
            f"{money_text(final_point.lower_balance, result.currency)} to "
            f"{money_text(final_point.upper_balance, result.currency)}"
        ),
    )
    st.vega_lite_chart(forecast_chart(result), use_container_width=True)
    for message in forecast_warning_messages(result.warnings):
        st.caption(f"• {message}")
    with st.expander("How this forecast was produced"):
        st.write(result.model.selection_reason)
        st.write(
            "Training window: "
            f"{result.model.training_window_start.isoformat()} to "
            f"{result.model.training_window_end.isoformat()}"
        )
        st.write(
            f"Reproducibility: seed {result.model.random_seed}, "
            f"{result.model.simulation_count} interval simulations."
        )
    return session


__all__ = [
    "WorkspaceResultsApi",
    "render_workspace_forecast",
    "render_workspace_overview",
    "render_workspace_transactions",
]
