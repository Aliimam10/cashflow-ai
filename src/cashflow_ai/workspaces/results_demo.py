"""Readable synthetic walkthrough of workspace analytics and forecasting."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from cashflow_ai.frontend.workspace_results_workflow import money_text
from cashflow_ai.schemas.statements import CoverageStatus
from cashflow_ai.schemas.transactions import Currency, FinancialRole
from cashflow_ai.schemas.workspace_analytics import WorkspaceAnalyticsRequest
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
from cashflow_ai.workspaces.analytics import compute_workspace_analytics
from cashflow_ai.workspaces.forecasting import forecast_statement_workspace

_TODAY = date(2026, 9, 13)
_NOW = datetime(2026, 9, 13, 10, tzinfo=UTC)
_STARTING_BALANCE = Decimal("1000.00")


def _synthetic_workspace() -> StatementWorkspace:
    start = _TODAY - timedelta(days=89)
    activities: list[tuple[date, str, Decimal, str, FinancialRole]] = [
        (
            date(2026, 6, 30),
            "SYNTHETIC MONTHLY PAY",
            Decimal("1500.00"),
            "income",
            FinancialRole.INCOME,
        ),
        (
            date(2026, 7, 1),
            "SYNTHETIC RENT",
            Decimal("-600.00"),
            "housing",
            FinancialRole.EXPENSE,
        ),
        (
            date(2026, 7, 15),
            "SYNTHETIC SAVINGS TRANSFER",
            Decimal("-200.00"),
            "transfers",
            FinancialRole.TRANSFER_OUT,
        ),
        (
            date(2026, 7, 31),
            "SYNTHETIC MONTHLY PAY",
            Decimal("1500.00"),
            "income",
            FinancialRole.INCOME,
        ),
        (
            date(2026, 8, 1),
            "SYNTHETIC RENT",
            Decimal("-600.00"),
            "housing",
            FinancialRole.EXPENSE,
        ),
        (
            date(2026, 8, 5),
            "SYNTHETIC REFUND",
            Decimal("20.00"),
            "shopping",
            FinancialRole.REFUND,
        ),
        (
            date(2026, 8, 31),
            "SYNTHETIC MONTHLY PAY",
            Decimal("1500.00"),
            "income",
            FinancialRole.INCOME,
        ),
        (
            date(2026, 9, 1),
            "SYNTHETIC RENT",
            Decimal("-600.00"),
            "housing",
            FinancialRole.EXPENSE,
        ),
    ]
    cursor = start
    while cursor <= _TODAY:
        if cursor.weekday() == 0:
            activities.append(
                (
                    cursor,
                    "SYNTHETIC WEEKLY GROCERIES",
                    Decimal("-80.00"),
                    "groceries",
                    FinancialRole.EXPENSE,
                )
            )
        cursor += timedelta(days=1)

    balance = _STARTING_BALANCE
    rows = []
    for index, (row_date, description, amount, category, role) in enumerate(
        sorted(activities, key=lambda item: (item[0], item[1]))
    ):
        balance += amount
        rows.append(
            WorkspaceTransactionRow(
                row_id=f"synthetic-{index + 1}",
                transaction_date=row_date,
                description=description,
                amount=amount,
                balance_after=balance,
                currency=Currency.GBP,
                category_id=category,
                financial_role=role,
                review_state=WorkspaceRowReviewState.CONFIRMED,
            )
        )
    return StatementWorkspace(
        workspace_id="synthetic-results-demo",
        retention_mode=WorkspaceRetentionMode.TEMPORARY,
        status=WorkspaceStatus.FINALIZED,
        account_name="Fictional current account",
        currency=Currency.GBP,
        revision=3,
        rows=tuple(rows),
        coverage=WorkspaceCoverageConfirmation(
            start_date=start,
            end_date=_TODAY,
            status=CoverageStatus.COMPLETE,
            confirmed=True,
        ),
        balance=WorkspaceBalanceConfirmation(
            balance=balance,
            as_of_date=_TODAY,
            currency=Currency.GBP,
            confirmed=True,
        ),
        created_at=_NOW - timedelta(hours=2),
        updated_at=_NOW - timedelta(hours=1),
        finalized_at=_NOW,
    )


def main() -> None:
    """Print results and one guard path without reading or writing local data."""
    workspace = _synthetic_workspace()
    analytics = compute_workspace_analytics(
        workspace,
        WorkspaceAnalyticsRequest(expected_workspace_revision=workspace.revision),
    )
    totals = analytics.totals
    if totals is None:
        raise RuntimeError("synthetic complete coverage unexpectedly withheld totals")

    print("CashFlow AI synthetic workspace-results check")
    print(
        "coverage: "
        f"{analytics.coverage.status.value} "
        f"({analytics.coverage.fully_covered_days}/"
        f"{analytics.coverage.requested_days} days)"
    )
    print(f"income: {money_text(totals.total_income, analytics.currency)}")
    print(f"spending: {money_text(totals.total_expenses, analytics.currency)}")
    print(
        "net transfers: "
        f"{money_text(totals.net_transfer_movement, analytics.currency, signed=True)}"
    )
    if workspace.balance is None:
        raise RuntimeError("synthetic balance unexpectedly unavailable")
    print(
        "latest verified balance: "
        f"{money_text(workspace.balance.balance, analytics.currency)}"
    )
    print(
        "expense categories: "
        + ", ".join(
            f"{item.category_name}={money_text(item.amount, analytics.currency)}"
            for item in analytics.category_spending or ()
        )
    )

    for horizon in (15, 30):
        forecast = forecast_statement_workspace(
            workspace,
            WorkspaceForecastRequest(
                expected_workspace_revision=workspace.revision,
                horizon_days=horizon,
            ),
            today=_TODAY,
        )
        if not isinstance(forecast, WorkspaceForecastAvailable):
            raise RuntimeError("synthetic trustworthy history unexpectedly withheld")
        final_point = forecast.daily_balances[-1]
        print(
            f"{horizon}-day forecast: available; expected "
            f"{money_text(final_point.expected_balance, forecast.currency)}; range "
            f"{money_text(final_point.lower_balance, forecast.currency)} to "
            f"{money_text(final_point.upper_balance, forecast.currency)}"
        )

    guarded = workspace.model_copy(
        update={
            "rows": (
                workspace.rows[0].model_copy(
                    update={"financial_role": FinancialRole.UNKNOWN}
                ),
                *workspace.rows[1:],
            )
        }
    )
    withheld = forecast_statement_workspace(
        guarded,
        WorkspaceForecastRequest(
            expected_workspace_revision=guarded.revision,
            horizon_days=15,
        ),
        today=_TODAY,
    )
    if not isinstance(withheld, WorkspaceForecastWithheld):
        raise RuntimeError("synthetic unresolved role unexpectedly produced a path")
    print(
        "unresolved-role guard: withheld ("
        + ", ".join(reason.value for reason in withheld.reasons)
        + ")"
    )
    print("files, databases, and real financial data created: no")


__all__ = ["main"]
