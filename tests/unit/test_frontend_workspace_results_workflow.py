"""Tests for pure finalized-workspace dashboard display projections."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

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
    workspace_balance_summary,
    workspace_pulse_html,
)
from cashflow_ai.schemas.api import Pagination
from cashflow_ai.schemas.statements import CoverageStatus, DateRange
from cashflow_ai.schemas.transactions import Currency, FinancialRole
from cashflow_ai.schemas.workspace_analytics import (
    WorkspaceAnalytics,
    WorkspaceAnalyticsRequest,
    WorkspaceTransactionSearchRequest,
)
from cashflow_ai.schemas.workspace_forecasts import (
    WorkspaceForecastAvailable,
    WorkspaceForecastReasonCode,
    WorkspaceForecastRequest,
    WorkspaceForecastWarningCode,
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


def _row(
    row_id: str,
    row_date: date,
    amount: str,
    role: FinancialRole,
    *,
    category: str = "other",
    description: str | None = None,
    balance: str | None = None,
) -> WorkspaceTransactionRow:
    return WorkspaceTransactionRow(
        row_id=row_id,
        transaction_date=row_date,
        description=description or f"SYNTHETIC {row_id.upper()}",
        amount=Decimal(amount),
        balance_after=None if balance is None else Decimal(balance),
        category_id=category,
        financial_role=role,
        review_state=WorkspaceRowReviewState.CONFIRMED,
    )


def _workspace(
    rows: tuple[WorkspaceTransactionRow, ...],
    *,
    start: date = date(2026, 8, 1),
    end: date = TODAY,
    coverage_status: CoverageStatus = CoverageStatus.COMPLETE,
    gaps: tuple[DateRange, ...] = (),
    balance: WorkspaceBalanceConfirmation | None = None,
    account_name: str = "Fictional <account>",
) -> StatementWorkspace:
    return StatementWorkspace(
        workspace_id="synthetic-workspace",
        retention_mode=WorkspaceRetentionMode.TEMPORARY,
        status=WorkspaceStatus.FINALIZED,
        account_name=account_name,
        revision=3,
        rows=rows,
        coverage=WorkspaceCoverageConfirmation(
            start_date=start,
            end_date=end,
            status=coverage_status,
            missing_periods=gaps,
            confirmed=True,
        ),
        balance=balance,
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(hours=1),
        finalized_at=NOW,
    )


def _analytics(workspace: StatementWorkspace) -> WorkspaceAnalytics:
    return compute_workspace_analytics(
        workspace,
        WorkspaceAnalyticsRequest(expected_workspace_revision=workspace.revision),
    )


def test_money_and_balance_summaries_cover_empty_and_change_tones() -> None:
    empty = _analytics(_workspace(()))
    assert money_text(Decimal("12.30"), Currency.GBP) == "£12.30"
    assert money_text(Decimal("12.30"), Currency.GBP, signed=True) == "+£12.30"
    assert money_text(Decimal("-12.30"), Currency.GBP) == "-£12.30"
    assert workspace_balance_summary(empty).tone == "unavailable"
    assert "No verified" in workspace_balance_hero_html(empty)
    assert "No verified balance history" in workspace_pulse_html(empty)

    for final_balance, expected_tone in (
        ("120.00", "positive"),
        ("80.00", "negative"),
        ("100.00", "neutral"),
    ):
        analytics = _analytics(
            _workspace(
                (
                    _row(
                        "opening",
                        date(2026, 8, 1),
                        "10.00",
                        FinancialRole.INCOME,
                        balance="100.00",
                    ),
                    _row(
                        "closing",
                        date(2026, 8, 2),
                        "-1.00",
                        FinancialRole.EXPENSE,
                        balance=final_balance,
                    ),
                )
            )
        )
        assert workspace_balance_summary(analytics).tone == expected_tone


def test_balance_pulse_escapes_labels_and_never_joins_gaps() -> None:
    gap = DateRange(start_date=date(2026, 8, 3), end_date=date(2026, 8, 5))
    analytics = _analytics(
        _workspace(
            (
                _row(
                    "first",
                    date(2026, 8, 1),
                    "10.00",
                    FinancialRole.INCOME,
                    balance="100.00",
                ),
                _row(
                    "second",
                    date(2026, 8, 2),
                    "-1.00",
                    FinancialRole.EXPENSE,
                    balance="100.00",
                ),
                _row(
                    "third",
                    date(2026, 8, 6),
                    "2.00",
                    FinancialRole.INCOME,
                    balance="102.00",
                ),
            ),
            coverage_status=CoverageStatus.GAPPED,
            gaps=(gap,),
        )
    )

    pulse = workspace_pulse_html(analytics)
    hero = workspace_balance_hero_html(analytics)
    assert pulse.count("<path") == 1
    assert pulse.count("<circle") == 1
    assert "disconnected lines" in pulse
    assert workspace_balance_summary(analytics).has_gaps
    assert "Fictional &lt;account&gt;" in hero
    assert "gaps are not joined" in hero

    one_point = _analytics(
        _workspace(
            (
                _row(
                    "one",
                    date(2026, 8, 1),
                    "1.00",
                    FinancialRole.INCOME,
                    balance="50.00",
                ),
            )
        )
    )
    assert "<circle" in workspace_pulse_html(one_point)
    assert workspace_balance_summary(one_point).change is None


def test_chart_specs_and_read_only_rows_keep_role_semantics_visible() -> None:
    workspace = _workspace(
        (
            _row(
                "income",
                date(2026, 8, 1),
                "100.00",
                FinancialRole.INCOME,
                category="income",
            ),
            _row(
                "expense",
                date(2026, 8, 2),
                "-20.00",
                FinancialRole.EXPENSE,
                category="food",
                description="SYNTHETIC FOOD",
                balance="80.00",
            ),
            _row(
                "transfer",
                date(2026, 8, 3),
                "-10.00",
                FinancialRole.TRANSFER_OUT,
                category="transfers",
            ),
        ),
        balance=WorkspaceBalanceConfirmation(
            balance=Decimal("80.00"),
            as_of_date=date(2026, 8, 2),
            confirmed=True,
        ),
    )
    analytics = _analytics(workspace)
    search = search_workspace_transactions(
        workspace,
        WorkspaceTransactionSearchRequest(
            expected_workspace_revision=workspace.revision,
            pagination=Pagination(limit=100, offset=0),
        ),
    )

    donut = category_donut_chart(analytics)
    cash_flow = monthly_cash_flow_chart(analytics)
    coverage = coverage_chart(analytics.coverage)
    rows = transaction_rows(search)
    assert donut["data"]["values"] == [
        {"category": "Food", "amount": 20.0, "transactions": 1}
    ]
    assert len(cash_flow["data"]["values"]) == 6
    assert coverage["data"]["values"][0]["status"] == "Covered"
    assert coverage["data"]["values"][0]["start"] == "2026-08-01"
    assert coverage["data"]["values"][0]["through"] == "2026-09-13"
    assert coverage["data"]["values"][0]["end_exclusive"] == "2026-09-14"
    assert coverage["encoding"]["x2"] == {"field": "end_exclusive"}
    one_day = coverage_chart(
        _analytics(
            _workspace(
                (),
                start=date(2026, 8, 4),
                end=date(2026, 8, 4),
            )
        ).coverage
    )["data"]["values"][0]
    assert one_day["start"] == "2026-08-04"
    assert one_day["end_exclusive"] == "2026-08-05"
    assert rows[0]["Financial role"] == "Transfer Out"
    assert rows[0]["Balance"] == ""
    assert rows[1]["Balance"] == "£80.00"


def test_analytics_messages_cover_partial_missing_and_unknown_roles() -> None:
    unknown = _row(
        "unknown",
        date(2026, 8, 2),
        "-10.00",
        FinancialRole.UNKNOWN,
    )
    partial = _analytics(
        _workspace(
            (unknown,),
            coverage_status=CoverageStatus.GAPPED,
            gaps=(
                DateRange(
                    start_date=date(2026, 8, 10),
                    end_date=date(2026, 8, 12),
                ),
            ),
        )
    )
    missing = _analytics(_workspace((unknown,), coverage_status=CoverageStatus.UNKNOWN))
    complete = _analytics(
        _workspace(
            (
                _row(
                    "expense",
                    date(2026, 8, 2),
                    "-10.00",
                    FinancialRole.EXPENSE,
                ),
            )
        )
    )

    assert len(analytics_messages(partial)) == 2
    assert "partial" in analytics_messages(partial)[0]
    assert "unknown financial role" in analytics_messages(partial)[1]
    assert len(analytics_messages(missing)) == 2
    assert "withheld" in analytics_messages(missing)[0]
    assert analytics_messages(complete) == ()


def test_forecast_specs_and_all_controlled_messages_are_complete() -> None:
    workspace = _workspace(
        (),
        start=TODAY - timedelta(days=69),
        balance=WorkspaceBalanceConfirmation(
            balance=Decimal("500.00"),
            as_of_date=TODAY,
            confirmed=True,
        ),
    )
    result = forecast_statement_workspace(
        workspace,
        WorkspaceForecastRequest(
            expected_workspace_revision=workspace.revision,
            horizon_days=15,
        ),
        today=TODAY,
    )
    assert isinstance(result, WorkspaceForecastAvailable)
    chart = forecast_chart(result)
    assert len(chart["data"]["values"]) == 15
    assert chart["layer"][0]["encoding"]["y"]["title"] == "Balance (GBP)"
    reason_messages = forecast_reason_messages(tuple(WorkspaceForecastReasonCode))
    warning_messages = forecast_warning_messages(tuple(WorkspaceForecastWarningCode))
    assert len(reason_messages) == len(WorkspaceForecastReasonCode)
    assert len(warning_messages) == len(WorkspaceForecastWarningCode)
