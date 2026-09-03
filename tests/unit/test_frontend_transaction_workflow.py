"""Tests for privacy-safe transaction and analytics display projections."""

from datetime import UTC, date, datetime
from decimal import Decimal

from cashflow_ai.frontend.transaction_workflow import (
    balance_chart,
    cadence_chart,
    category_chart,
    coverage_chart,
    coverage_rows,
    money_text,
    monthly_cash_flow_chart,
    transaction_rows,
)
from cashflow_ai.schemas.analytics import (
    AccountBalanceHistory,
    AccountCoverageIndicator,
    AnalyticsCoverageStatus,
    AnalyticsScope,
    AnalyticsValueBasis,
    AnalyticsView,
    BalanceHistoryPoint,
    BalanceHistorySegment,
    CashFlowAnalytics,
    CashFlowTotals,
    CategorySpending,
    DataCoverageIndicator,
    MonthlyCashFlow,
    SavingsRateResult,
    SpendingCadenceBreakdown,
)
from cashflow_ai.schemas.api import TransactionResponse
from cashflow_ai.schemas.statements import BalanceSnapshotSource, DateRange
from cashflow_ai.schemas.transactions import Currency, Direction, FinancialRole


def _coverage() -> DataCoverageIndicator:
    period = DateRange(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))
    covered = DateRange(start_date=date(2026, 8, 1), end_date=date(2026, 8, 15))
    partial = DateRange(start_date=date(2026, 8, 16), end_date=date(2026, 8, 20))
    missing = DateRange(start_date=date(2026, 8, 21), end_date=date(2026, 8, 31))
    return DataCoverageIndicator(
        requested_period=period,
        status=AnalyticsCoverageStatus.PARTIAL,
        fully_covered_periods=(covered,),
        partially_covered_periods=(partial,),
        missing_periods=(missing,),
        requested_days=31,
        fully_covered_days=15,
        partially_covered_days=5,
        missing_days=11,
        accounts=(
            AccountCoverageIndicator(
                account_id="account-1",
                status=AnalyticsCoverageStatus.PARTIAL,
                covered_periods=(covered, partial),
                missing_periods=(missing,),
                covered_days=20,
                missing_days=11,
            ),
        ),
    )


def _totals() -> CashFlowTotals:
    return CashFlowTotals(
        currency=Currency.GBP,
        basis=AnalyticsValueBasis.OBSERVED_ONLY,
        total_income=Decimal("1000.00"),
        total_expenses=Decimal("400.00"),
        total_refunds=Decimal("10.00"),
        total_reimbursements=Decimal("0.00"),
        total_cash_withdrawals=Decimal("0.00"),
        net_cash_flow=Decimal("610.00"),
        transfer_inflow=Decimal("0.00"),
        transfer_outflow=Decimal("0.00"),
        net_transfer_movement=Decimal("0.00"),
        unknown_inflow=Decimal("0.00"),
        unknown_outflow=Decimal("0.00"),
        excluded_inflow=Decimal("0.00"),
        excluded_outflow=Decimal("0.00"),
        transaction_count=3,
        unknown_transaction_count=0,
        excluded_transaction_count=0,
        matched_internal_transfer_count=0,
    )


def _analytics(*, detailed: bool = True) -> CashFlowAnalytics:
    coverage = _coverage()
    scope = AnalyticsScope(
        user_profile_id="profile-1",
        account_ids=("account-1",),
        period=coverage.requested_period,
        view=AnalyticsView.ACCOUNT,
    )
    totals = _totals()
    return CashFlowAnalytics(
        scope=scope,
        currency=Currency.GBP,
        coverage=coverage,
        totals=totals,
        savings_rate=SavingsRateResult(rate_percent=Decimal("61.00")),
        category_spending=(
            (
                CategorySpending(
                    category_id="food",
                    category_name="Food",
                    amount=Decimal("400.00"),
                    transaction_count=1,
                ),
                CategorySpending(
                    category_id=None,
                    category_name=None,
                    amount=Decimal("5.00"),
                    transaction_count=1,
                ),
            )
            if detailed
            else None
        ),
        spending_cadence=(
            SpendingCadenceBreakdown(
                recurring=Decimal("300.00"),
                discretionary=Decimal("100.00"),
                unclassified=Decimal("5.00"),
                recurring_count=1,
                discretionary_count=1,
                unclassified_count=1,
            )
            if detailed
            else None
        ),
        largest_transactions=(),
        balance_history=(
            AccountBalanceHistory(
                account_id="account-1",
                segments=(
                    BalanceHistorySegment(
                        coverage_period=coverage.fully_covered_periods[0],
                        points=(
                            BalanceHistoryPoint(
                                snapshot_id="snapshot-1",
                                account_id="account-1",
                                as_of_date=date(2026, 8, 1),
                                balance=Decimal("1000.00"),
                                currency=Currency.GBP,
                                source=BalanceSnapshotSource.RUNNING_BALANCE,
                            ),
                        ),
                    ),
                    BalanceHistorySegment(
                        coverage_period=None,
                        points=(
                            BalanceHistoryPoint(
                                snapshot_id="snapshot-2",
                                account_id="account-1",
                                as_of_date=date(2026, 8, 20),
                                balance=Decimal("610.00"),
                                currency=Currency.GBP,
                                source=BalanceSnapshotSource.STATEMENT_CLOSING,
                            ),
                        ),
                    ),
                ),
            ),
        ),
        monthly_cash_flow=(
            MonthlyCashFlow(
                month=date(2026, 8, 1),
                period=coverage.requested_period,
                full_calendar_month=True,
                coverage=coverage,
                totals=totals if detailed else None,
                savings_rate=SavingsRateResult(rate_percent=Decimal("61.00")),
                observed_transaction_count=3,
            ),
        ),
        monthly_comparisons=(),
        observed_transaction_count=3,
    )


def test_transaction_and_money_rows_use_controlled_fallback_labels() -> None:
    transaction = TransactionResponse(
        transaction_id="transaction-1",
        account_id="account-1",
        transaction_date=date(2026, 8, 1),
        posting_date=None,
        description="Synthetic shop",
        merchant=None,
        amount=Decimal("-12.50"),
        balance_after=None,
        currency=Currency.GBP,
        external_id=None,
        transaction_type=None,
        direction=Direction.OUTFLOW,
        category_id=None,
        financial_role=FinancialRole.EXPENSE,
        verified_at=datetime(2026, 8, 2, tzinfo=UTC),
    )

    rows = transaction_rows((transaction,), account_names={}, category_names={})

    assert money_text(Decimal("1234.5"), "GBP") == "GBP 1,234.50"
    assert rows[0]["Account"] == "Unknown account"
    assert rows[0]["Category"] == "Uncategorised"
    assert rows[0]["Financial role"] == "expense"
    assert rows[0]["Transaction ID"] == "transaction-1"


def test_chart_specs_retain_coverage_gaps_and_explicit_observed_values() -> None:
    analytics = _analytics()

    periods = coverage_rows(analytics.coverage)
    timeline = coverage_chart(analytics.coverage)
    balance = balance_chart(analytics)
    categories = category_chart(analytics)
    cadence = cadence_chart(analytics)
    monthly = monthly_cash_flow_chart(analytics)

    assert [item["status"] for item in periods] == [
        "Fully covered",
        "Partially covered",
        "Missing",
    ]
    assert timeline["data"]["values"] == periods
    assert [item["segment"] for item in balance["data"]["values"]] == [
        "account-1:0",
        "account-1:1",
    ]
    assert categories["data"]["values"][1]["category"] == "Uncategorised"
    assert cadence["data"]["values"][0] == {
        "cadence": "Recurring",
        "amount": 300.0,
    }
    assert [item["flow"] for item in monthly["data"]["values"]] == [
        "Income",
        "Expenses",
    ]


def test_optional_charts_remain_empty_when_analytics_withholds_values() -> None:
    analytics = _analytics(detailed=False)

    assert category_chart(analytics)["data"]["values"] == []
    assert cadence_chart(analytics)["data"]["values"] == []
    assert monthly_cash_flow_chart(analytics)["data"]["values"] == []
