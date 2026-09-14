"""Typed client tests for finalized-workspace result endpoints."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx2

from cashflow_ai.frontend.client import ApiClient
from cashflow_ai.schemas.api import Pagination
from cashflow_ai.schemas.statements import CoverageStatus
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspace_analytics import (
    WorkspaceAnalyticsRequest,
    WorkspaceTransactionSearchRequest,
)
from cashflow_ai.schemas.workspace_forecasts import WorkspaceForecastRequest
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


def _workspace() -> StatementWorkspace:
    return StatementWorkspace(
        workspace_id="workspace/with spaces",
        retention_mode=WorkspaceRetentionMode.TEMPORARY,
        status=WorkspaceStatus.FINALIZED,
        account_name="Fictional account",
        revision=3,
        rows=(
            WorkspaceTransactionRow(
                row_id="synthetic-row",
                transaction_date=TODAY - timedelta(days=5),
                description="SYNTHETIC FOOD",
                amount=Decimal("-20.00"),
                category_id="food",
                financial_role=FinancialRole.EXPENSE,
                review_state=WorkspaceRowReviewState.CONFIRMED,
            ),
        ),
        coverage=WorkspaceCoverageConfirmation(
            start_date=TODAY - timedelta(days=69),
            end_date=TODAY,
            status=CoverageStatus.COMPLETE,
            confirmed=True,
        ),
        balance=WorkspaceBalanceConfirmation(
            balance=Decimal("500.00"),
            as_of_date=TODAY,
            confirmed=True,
        ),
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(hours=1),
        finalized_at=NOW,
    )


def test_client_posts_revision_bound_result_requests_and_validates_union() -> None:
    workspace = _workspace()
    analytics_request = WorkspaceAnalyticsRequest(
        expected_workspace_revision=workspace.revision
    )
    search_request = WorkspaceTransactionSearchRequest(
        expected_workspace_revision=workspace.revision,
        pagination=Pagination(limit=8, offset=0),
    )
    forecast_request = WorkspaceForecastRequest(
        expected_workspace_revision=workspace.revision,
        horizon_days=15,
    )
    analytics = compute_workspace_analytics(workspace, analytics_request)
    transactions = search_workspace_transactions(workspace, search_request)
    forecast = forecast_statement_workspace(
        workspace,
        forecast_request,
        today=TODAY,
    )
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path.endswith("/analytics"):
            payload = analytics.model_dump(mode="json")
        elif request.url.path.endswith("/transactions/search"):
            payload = transactions.model_dump(mode="json")
        else:
            payload = forecast.model_dump(mode="json")
        return httpx2.Response(200, json=payload)

    with ApiClient(
        "http://127.0.0.1:8765",
        transport=httpx2.MockTransport(handler),
    ) as client:
        assert (
            client.workspace_analytics(
                workspace.workspace_id,
                analytics_request,
            )
            == analytics
        )
        assert (
            client.search_workspace_transactions(
                workspace.workspace_id,
                search_request,
            )
            == transactions
        )
        assert (
            client.workspace_forecast(
                workspace.workspace_id,
                forecast_request,
            )
            == forecast
        )

    assert len(requests) == 3
    assert all(request.method == "POST" for request in requests)
    assert all(
        "workspace%2Fwith%20spaces" in request.url.raw_path.decode()
        for request in requests
    )
    assert b'"expected_workspace_revision":3' in requests[0].content
    assert b'"horizon_days":15' in requests[2].content
