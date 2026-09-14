"""Interaction tests for finalized-workspace Streamlit result pages."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

import cashflow_ai.frontend.workspace_results_page as page
from cashflow_ai.frontend.client import ApiClientError, ApiClientErrorCode
from cashflow_ai.frontend.session import FrontendSessionState
from cashflow_ai.schemas.api import Pagination
from cashflow_ai.schemas.statements import CoverageStatus
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspace_analytics import (
    WorkspaceAnalytics,
    WorkspaceAnalyticsRequest,
    WorkspaceTransactionSearchRequest,
    WorkspaceTransactionSearchResult,
)
from cashflow_ai.schemas.workspace_forecasts import (
    WorkspaceForecastAvailable,
    WorkspaceForecastRequest,
    WorkspaceForecastWithheld,
)
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceBalanceConfirmation,
    WorkspaceCoverageConfirmation,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceStatus,
    WorkspaceTransactionRow,
)
from cashflow_ai.workspaces.analytics import (
    compute_workspace_analytics,
    search_workspace_transactions,
)
from cashflow_ai.workspaces.forecasting import forecast_statement_workspace

TODAY = date(2026, 9, 13)
NOW = datetime(2026, 9, 13, 10, tzinfo=UTC)


def _workspace(*, with_expense: bool = True) -> StatementWorkspace:
    rows = (
        (
            WorkspaceTransactionRow(
                row_id="synthetic-income",
                transaction_date=TODAY - timedelta(days=30),
                description="SYNTHETIC PAY",
                amount=Decimal("1000.00"),
                balance_after=Decimal("1000.00"),
                category_id="income",
                financial_role=FinancialRole.INCOME,
                review_state=WorkspaceRowReviewState.CONFIRMED,
            ),
            WorkspaceTransactionRow(
                row_id="synthetic-expense",
                transaction_date=TODAY - timedelta(days=5),
                description="SYNTHETIC FOOD",
                amount=Decimal("-20.00"),
                balance_after=Decimal("980.00"),
                category_id="food",
                financial_role=FinancialRole.EXPENSE,
                review_state=WorkspaceRowReviewState.CONFIRMED,
            ),
        )
        if with_expense
        else ()
    )
    return StatementWorkspace(
        workspace_id="synthetic-workspace",
        retention_mode=WorkspaceRetentionMode.TEMPORARY,
        status=WorkspaceStatus.FINALIZED,
        account_name="Fictional account",
        revision=3,
        rows=rows,
        coverage=WorkspaceCoverageConfirmation(
            start_date=TODAY - timedelta(days=69),
            end_date=TODAY,
            status=CoverageStatus.COMPLETE,
            confirmed=True,
        ),
        balance=WorkspaceBalanceConfirmation(
            balance=Decimal("980.00"),
            as_of_date=TODAY,
            confirmed=True,
        ),
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(hours=1),
        finalized_at=NOW,
    )


def _analytics(workspace: StatementWorkspace) -> WorkspaceAnalytics:
    return compute_workspace_analytics(
        workspace,
        WorkspaceAnalyticsRequest(expected_workspace_revision=workspace.revision),
    )


def _transactions(
    workspace: StatementWorkspace,
) -> WorkspaceTransactionSearchResult:
    return search_workspace_transactions(
        workspace,
        WorkspaceTransactionSearchRequest(
            expected_workspace_revision=workspace.revision,
            pagination=Pagination(limit=100, offset=0),
        ),
    )


@pytest.fixture
def ui(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    streamlit = MagicMock()
    streamlit.columns.side_effect = lambda count: tuple(
        MagicMock() for _index in range(count)
    )
    streamlit.radio.return_value = 15
    streamlit.expander.return_value = nullcontext()
    monkeypatch.setattr(page, "st", streamlit)
    monkeypatch.setattr(page, "loading_state", lambda message: nullcontext())
    monkeypatch.setattr(page, "render_page_header", MagicMock())
    streamlit.empty_state = MagicMock()
    streamlit.display_error = MagicMock()
    streamlit.privacy_notice = MagicMock()
    monkeypatch.setattr(page, "render_empty_state", streamlit.empty_state)
    monkeypatch.setattr(page, "render_error", streamlit.display_error)
    monkeypatch.setattr(page, "render_privacy_notice", streamlit.privacy_notice)
    monkeypatch.setattr(page, "render_forecast_disclaimer", MagicMock())
    return streamlit


def test_identity_and_analytics_loader_are_revision_bound() -> None:
    assert page._workspace_identity(FrontendSessionState()) is None
    assert (
        page._workspace_identity(FrontendSessionState(workspace_id="workspace-only"))
        is None
    )
    session = FrontendSessionState(
        workspace_id="synthetic-workspace",
        workspace_revision=3,
        workspace_status=WorkspaceStatus.FINALIZED,
    )
    client = MagicMock()
    expected = _analytics(_workspace())
    client.workspace_analytics.return_value = expected

    assert page._load_analytics(client, FrontendSessionState()) is None
    assert page._load_analytics(client, session) == expected
    request = client.workspace_analytics.call_args.args[1]
    assert request.expected_workspace_revision == 3
    assert page._recent_transactions(client, FrontendSessionState()) is None
    client.search_workspace_transactions.return_value = _transactions(_workspace())
    recent = page._recent_transactions(client, session)
    assert recent is not None
    assert recent.total == 2
    search_request = client.search_workspace_transactions.call_args.args[1]
    assert search_request.pagination.limit == 8


def test_totals_and_charts_cover_available_and_withheld_states(
    ui: MagicMock,
) -> None:
    analytics = _analytics(_workspace())
    page._render_messages(analytics)
    page._render_messages(
        analytics.model_copy(update={"unresolved_financial_role_count": 1})
    )
    page._render_totals(analytics)
    page._render_charts(analytics)
    assert ui.warning.called
    assert ui.columns.call_count >= 2
    assert ui.vega_lite_chart.call_count == 3
    assert ui.dataframe.called

    missing = analytics.model_copy(
        update={
            "totals": None,
            "category_spending": None,
            "monthly_cash_flow": (),
        }
    )
    page._render_totals(missing)
    page._render_charts(missing)
    assert ui.empty_state.call_count >= 3


def test_overview_handles_missing_identity_api_failure_and_recent_rows(
    ui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    navigate = MagicMock()
    blank = FrontendSessionState()
    assert (
        page.render_workspace_overview(MagicMock(), blank, navigate=navigate) == blank
    )
    assert ui.empty_state.called
    assert ui.button.called

    session = FrontendSessionState(
        workspace_id="synthetic-workspace",
        workspace_revision=3,
        workspace_status=WorkspaceStatus.FINALIZED,
    )
    failure = ApiClientError(
        ApiClientErrorCode.CONNECTION_FAILED,
        "safe failure",
    )
    monkeypatch.setattr(page, "_load_analytics", MagicMock(side_effect=failure))
    assert (
        page.render_workspace_overview(MagicMock(), session, navigate=navigate)
        == session
    )
    ui.display_error.assert_called_with(failure)

    workspace = _workspace()
    analytics = _analytics(workspace)
    transactions = _transactions(workspace)
    monkeypatch.setattr(page, "_load_analytics", MagicMock(return_value=analytics))
    recent_mock = MagicMock(return_value=transactions)
    monkeypatch.setattr(
        page,
        "_recent_transactions",
        recent_mock,
    )
    monkeypatch.setattr(page, "_render_messages", MagicMock())
    monkeypatch.setattr(page, "_render_totals", MagicMock())
    monkeypatch.setattr(page, "_render_charts", MagicMock())
    assert (
        page.render_workspace_overview(MagicMock(), session, navigate=navigate)
        == session
    )
    assert ui.markdown.call_args.kwargs["unsafe_allow_html"] is True
    assert ui.dataframe.called
    ui.privacy_notice.assert_called_once()

    recent_mock.return_value = None
    ui.empty_state.reset_mock()
    page.render_workspace_overview(MagicMock(), session, navigate=navigate)
    ui.empty_state.assert_called_with(
        "No approved transactions",
        "The finalized table does not contain rows in this period.",
    )


def test_transactions_page_covers_gate_error_empty_and_truncated_results(
    ui: MagicMock,
) -> None:
    navigate = MagicMock()
    blank = FrontendSessionState()
    assert (
        page.render_workspace_transactions(MagicMock(), blank, navigate=navigate)
        == blank
    )

    session = FrontendSessionState(
        workspace_id="synthetic-workspace",
        workspace_revision=3,
        workspace_status=WorkspaceStatus.FINALIZED,
    )
    failure_client = MagicMock()
    failure = ApiClientError(
        ApiClientErrorCode.API_REJECTED_REQUEST,
        "safe failure",
    )
    failure_client.search_workspace_transactions.side_effect = failure
    assert (
        page.render_workspace_transactions(
            failure_client,
            session,
            navigate=navigate,
        )
        == session
    )
    ui.display_error.assert_called_with(failure)

    empty_client = MagicMock()
    empty_client.search_workspace_transactions.return_value = _transactions(
        _workspace(with_expense=False)
    )
    ui.empty_state.reset_mock()
    page.render_workspace_transactions(empty_client, session, navigate=navigate)
    assert ui.empty_state.called

    complete_result = _transactions(_workspace())
    complete_client = MagicMock()
    complete_client.search_workspace_transactions.return_value = complete_result
    page.render_workspace_transactions(complete_client, session, navigate=navigate)

    result = complete_result.model_copy(update={"total": 101})
    populated_client = MagicMock()
    populated_client.search_workspace_transactions.return_value = result
    page.render_workspace_transactions(populated_client, session, navigate=navigate)
    assert ui.dataframe.called
    assert ui.warning.called
    request = populated_client.search_workspace_transactions.call_args.args[1]
    assert request.pagination.limit == 100


def test_forecast_page_gates_errors_withholds_and_renders_available_path(
    ui: MagicMock,
) -> None:
    blank = FrontendSessionState()
    assert page.render_workspace_forecast(MagicMock(), blank) == blank
    assert ui.empty_state.called

    session = FrontendSessionState(
        workspace_id="synthetic-workspace",
        workspace_revision=3,
        workspace_status=WorkspaceStatus.FINALIZED,
    )
    failure_client = MagicMock()
    failure = ApiClientError(
        ApiClientErrorCode.CONNECTION_FAILED,
        "safe failure",
    )
    failure_client.workspace_forecast.side_effect = failure
    assert page.render_workspace_forecast(failure_client, session) == session
    ui.display_error.assert_called_with(failure)

    workspace = _workspace()
    withheld = forecast_statement_workspace(
        workspace,
        WorkspaceForecastRequest(
            expected_workspace_revision=2,
            horizon_days=15,
        ),
        today=TODAY,
    )
    assert isinstance(withheld, WorkspaceForecastWithheld)
    withheld_client = MagicMock()
    withheld_client.workspace_forecast.return_value = withheld
    page.render_workspace_forecast(withheld_client, session)
    assert ui.warning.called
    assert ui.markdown.called

    available = forecast_statement_workspace(
        workspace,
        WorkspaceForecastRequest(
            expected_workspace_revision=3,
            horizon_days=15,
        ),
        today=TODAY,
    )
    assert isinstance(available, WorkspaceForecastAvailable)
    available_client = MagicMock()
    available_client.workspace_forecast.return_value = available
    page.render_workspace_forecast(available_client, session)
    assert ui.vega_lite_chart.called
    assert ui.write.call_count == 3
    request = available_client.workspace_forecast.call_args.args[1]
    assert request.horizon_days == 15
