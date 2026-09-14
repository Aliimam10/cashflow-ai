"""Tests for pure analytics over finalized synthetic statement workspaces."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from cashflow_ai.schemas.analytics import (
    AnalyticsCoverageStatus,
    AnalyticsValueBasis,
    SavingsRateUnavailableReason,
)
from cashflow_ai.schemas.api import Pagination
from cashflow_ai.schemas.statements import (
    BalanceSnapshotSource,
    CoverageStatus,
    DateRange,
)
from cashflow_ai.schemas.transactions import Currency, Direction, FinancialRole
from cashflow_ai.schemas.workspace_analytics import (
    WorkspaceAnalyticsRequest,
    WorkspaceTransactionSearchRequest,
    WorkspaceTransactionSearchResult,
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
    WorkspaceAnalyticsError,
    WorkspaceAnalyticsErrorCode,
    compute_workspace_analytics,
    search_workspace_transactions,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _row(
    row_id: str,
    value_date: date,
    amount: str,
    role: FinancialRole,
    *,
    category: str = "other",
    description: str | None = None,
    merchant: str | None = None,
    balance: str | None = None,
    posting_date: date | None = None,
    external_id: str | None = None,
    transaction_type: str | None = None,
) -> WorkspaceTransactionRow:
    return WorkspaceTransactionRow(
        row_id=row_id,
        transaction_date=value_date,
        posting_date=posting_date,
        description=description or f"FICTIONAL {row_id.upper()}",
        merchant=merchant,
        amount=Decimal(amount),
        balance_after=None if balance is None else Decimal(balance),
        currency=Currency.GBP,
        category_id=category,
        financial_role=role,
        external_id=external_id,
        transaction_type=transaction_type,
        review_state=WorkspaceRowReviewState.CONFIRMED,
    )


def _workspace(
    rows: tuple[WorkspaceTransactionRow, ...],
    *,
    status: CoverageStatus = CoverageStatus.COMPLETE,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 1, 31),
    gaps: tuple[DateRange, ...] = (),
    balance: WorkspaceBalanceConfirmation | None = None,
    revision: int = 4,
) -> StatementWorkspace:
    return StatementWorkspace(
        workspace_id="workspace-synthetic",
        retention_mode=WorkspaceRetentionMode.SAVED,
        status=WorkspaceStatus.FINALIZED,
        account_name="Fictional current account",
        currency=Currency.GBP,
        revision=revision,
        rows=rows,
        coverage=WorkspaceCoverageConfirmation(
            start_date=start,
            end_date=end,
            status=status,
            missing_periods=gaps,
            confirmed=True,
        ),
        balance=balance,
        created_at=NOW,
        updated_at=NOW,
        finalized_at=NOW,
    )


def _complete_role_workspace() -> StatementWorkspace:
    rows = (
        _row(
            "income",
            date(2026, 1, 1),
            "1000.00",
            FinancialRole.INCOME,
            category="income",
            balance="1000.00",
        ),
        _row(
            "groceries-one",
            date(2026, 1, 2),
            "-60.00",
            FinancialRole.EXPENSE,
            category="groceries",
            balance="940.00",
        ),
        _row(
            "groceries-two",
            date(2026, 1, 3),
            "-40.00",
            FinancialRole.EXPENSE,
            category="groceries",
        ),
        _row(
            "pet-care",
            date(2026, 1, 4),
            "-50.00",
            FinancialRole.EXPENSE,
            category="pet_care",
        ),
        _row("refund", date(2026, 1, 5), "25.00", FinancialRole.REFUND),
        _row(
            "reimbursement",
            date(2026, 1, 6),
            "15.00",
            FinancialRole.REIMBURSEMENT,
        ),
        _row(
            "cash",
            date(2026, 1, 7),
            "-30.00",
            FinancialRole.CASH_WITHDRAWAL,
        ),
        _row(
            "transfer-in",
            date(2026, 1, 8),
            "200.00",
            FinancialRole.TRANSFER_IN,
            category="transfers",
        ),
        _row(
            "transfer-out",
            date(2026, 1, 9),
            "-200.00",
            FinancialRole.TRANSFER_OUT,
            category="transfers",
        ),
        _row("unknown-in", date(2026, 1, 10), "5.00", FinancialRole.UNKNOWN),
        _row("unknown-out", date(2026, 1, 11), "-6.00", FinancialRole.UNKNOWN),
        _row("excluded-in", date(2026, 1, 12), "7.00", FinancialRole.EXCLUDED),
        _row(
            "excluded-out",
            date(2026, 1, 31),
            "-8.00",
            FinancialRole.EXCLUDED,
            balance="850.00",
        ),
    )
    return _workspace(
        rows,
        balance=WorkspaceBalanceConfirmation(
            balance=Decimal("900.00"),
            as_of_date=date(2026, 1, 31),
            currency=Currency.GBP,
            confirmed=True,
        ),
    )


def test_complete_workspace_analytics_keeps_roles_categories_and_balance_distinct() -> (
    None
):
    workspace = _complete_role_workspace()

    result = compute_workspace_analytics(
        workspace,
        WorkspaceAnalyticsRequest(
            expected_workspace_revision=workspace.revision,
            largest_transaction_limit=100,
        ),
    )

    assert result.period == DateRange(
        start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
    )
    assert result.coverage.status is AnalyticsCoverageStatus.COMPLETE
    assert result.coverage.fully_covered_days == 31
    totals = result.totals
    assert totals is not None
    assert totals.basis is AnalyticsValueBasis.COMPLETE_PERIOD
    assert totals.total_income == Decimal("1000.00")
    assert totals.total_expenses == Decimal("150.00")
    assert totals.total_refunds == Decimal("25.00")
    assert totals.total_reimbursements == Decimal("15.00")
    assert totals.total_cash_withdrawals == Decimal("30.00")
    assert totals.net_cash_flow == Decimal("860.00")
    assert totals.transfer_inflow == Decimal("200.00")
    assert totals.transfer_outflow == Decimal("200.00")
    assert totals.net_transfer_movement == Decimal("0.00")
    assert totals.unknown_inflow == Decimal("5.00")
    assert totals.unknown_outflow == Decimal("6.00")
    assert totals.excluded_inflow == Decimal("7.00")
    assert totals.excluded_outflow == Decimal("8.00")
    assert totals.transaction_count == 13
    assert totals.unknown_transaction_count == 2
    assert totals.excluded_transaction_count == 2
    assert totals.matched_internal_transfer_count == 0
    assert result.unresolved_financial_role_count == 2
    assert result.savings_rate.unavailable_reason is (
        SavingsRateUnavailableReason.UNRESOLVED_FINANCIAL_ROLES
    )
    assert [
        (item.category_name, item.amount) for item in result.category_spending or ()
    ] == [
        ("Groceries", Decimal("100.00")),
        ("Pet Care", Decimal("50.00")),
    ]
    cadence = result.spending_cadence
    assert cadence is not None
    assert cadence.recurring == Decimal("0.00")
    assert cadence.discretionary == Decimal("0.00")
    assert cadence.unclassified == Decimal("150.00")
    assert cadence.unclassified_count == 3
    assert len(result.largest_transactions) == 11
    assert all(
        item.financial_role is not FinancialRole.EXCLUDED
        for item in result.largest_transactions
    )
    history = result.balance_history[0]
    assert len(history.segments) == 1
    assert [point.balance for point in history.segments[0].points] == [
        Decimal("1000.00"),
        Decimal("940.00"),
        Decimal("850.00"),
        Decimal("900.00"),
    ]
    assert history.segments[0].points[-1].source is BalanceSnapshotSource.MANUAL
    assert len(result.monthly_cash_flow) == 1
    assert result.monthly_cash_flow[0].full_calendar_month


def test_complete_workspace_savings_rate_and_no_income_reason() -> None:
    rows = (
        _row("pay", date(2026, 1, 1), "100.00", FinancialRole.INCOME),
        _row("shop", date(2026, 1, 2), "-20.00", FinancialRole.EXPENSE),
    )
    complete = _workspace(rows)
    result = compute_workspace_analytics(
        complete,
        WorkspaceAnalyticsRequest(expected_workspace_revision=complete.revision),
    )
    assert result.savings_rate.rate_percent == Decimal("80.00")
    assert result.savings_rate.unavailable_reason is None

    no_income = _workspace((rows[1],), status=CoverageStatus.OVERLAPPING)
    no_income_result = compute_workspace_analytics(
        no_income,
        WorkspaceAnalyticsRequest(expected_workspace_revision=no_income.revision),
    )
    assert no_income_result.coverage.status is AnalyticsCoverageStatus.COMPLETE
    assert no_income_result.savings_rate.unavailable_reason is (
        SavingsRateUnavailableReason.NO_INCOME
    )


def test_balance_history_preserves_same_day_evidence_in_canonical_order() -> None:
    workspace = _workspace(
        (
            _row(
                "morning",
                date(2026, 1, 10),
                "100.00",
                FinancialRole.INCOME,
                balance="500.00",
            ),
            _row(
                "afternoon",
                date(2026, 1, 10),
                "-40.00",
                FinancialRole.EXPENSE,
                balance="460.00",
            ),
        ),
        balance=WorkspaceBalanceConfirmation(
            balance=Decimal("460.00"),
            as_of_date=date(2026, 1, 10),
            confirmed=True,
        ),
    )

    result = compute_workspace_analytics(
        workspace,
        WorkspaceAnalyticsRequest(expected_workspace_revision=workspace.revision),
    )

    points = result.balance_history[0].segments[0].points
    assert [point.snapshot_id for point in points] == [
        "morning",
        "afternoon",
        "confirmed-workspace-balance",
    ]
    assert [point.balance for point in points] == [
        Decimal("500.00"),
        Decimal("460.00"),
        Decimal("460.00"),
    ]


def test_gapped_workspace_uses_observed_values_and_disconnects_balance_history() -> (
    None
):
    gap = DateRange(start_date=date(2026, 1, 11), end_date=date(2026, 1, 20))
    workspace = _workspace(
        (
            _row(
                "before-gap",
                date(2026, 1, 5),
                "-10.00",
                FinancialRole.EXPENSE,
                balance="90.00",
            ),
            _row(
                "after-gap",
                date(2026, 1, 25),
                "100.00",
                FinancialRole.INCOME,
                balance="190.00",
            ),
        ),
        status=CoverageStatus.GAPPED,
        gaps=(gap,),
        balance=WorkspaceBalanceConfirmation(
            balance=Decimal("120.00"),
            as_of_date=date(2026, 1, 15),
            confirmed=True,
        ),
    )

    result = compute_workspace_analytics(
        workspace,
        WorkspaceAnalyticsRequest(expected_workspace_revision=workspace.revision),
    )

    assert result.coverage.status is AnalyticsCoverageStatus.PARTIAL
    assert result.coverage.fully_covered_periods == (
        DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 10)),
        DateRange(start_date=date(2026, 1, 21), end_date=date(2026, 1, 31)),
    )
    assert result.coverage.missing_periods == (gap,)
    assert result.coverage.fully_covered_days == 21
    assert result.coverage.missing_days == 10
    assert result.totals is not None
    assert result.totals.basis is AnalyticsValueBasis.OBSERVED_ONLY
    assert result.savings_rate.unavailable_reason is (
        SavingsRateUnavailableReason.INCOMPLETE_COVERAGE
    )
    segments = result.balance_history[0].segments
    assert [item.coverage_period for item in segments] == [
        DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 10)),
        None,
        DateRange(start_date=date(2026, 1, 21), end_date=date(2026, 1, 31)),
    ]


def test_gaps_may_touch_both_confirmed_coverage_boundaries() -> None:
    workspace = _workspace(
        (_row("known", date(2026, 1, 10), "10.00", FinancialRole.INCOME),),
        status=CoverageStatus.GAPPED,
        gaps=(
            DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 5)),
            DateRange(start_date=date(2026, 1, 25), end_date=date(2026, 1, 31)),
        ),
    )

    result = compute_workspace_analytics(
        workspace,
        WorkspaceAnalyticsRequest(expected_workspace_revision=workspace.revision),
    )

    assert result.coverage.fully_covered_periods == (
        DateRange(start_date=date(2026, 1, 6), end_date=date(2026, 1, 24)),
    )
    assert result.coverage.missing_days == 12


@pytest.mark.parametrize(
    "coverage_status", [CoverageStatus.PARTIAL, CoverageStatus.UNKNOWN]
)
def test_unproven_workspace_coverage_withholds_totals(
    coverage_status: CoverageStatus,
) -> None:
    workspace = _workspace(
        (
            _row(
                "unknown",
                date(2026, 1, 2),
                "-9.00",
                FinancialRole.UNKNOWN,
            ),
        ),
        status=coverage_status,
    )

    result = compute_workspace_analytics(
        workspace,
        WorkspaceAnalyticsRequest(expected_workspace_revision=workspace.revision),
    )

    assert result.coverage.status is AnalyticsCoverageStatus.MISSING
    assert result.coverage.fully_covered_periods == ()
    assert result.coverage.missing_periods == (
        DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)),
    )
    assert result.totals is None
    assert result.category_spending is None
    assert result.spending_cadence is None
    assert result.largest_transactions == ()
    assert result.balance_history == ()
    assert result.monthly_cash_flow[0].totals is None
    assert result.observed_transaction_count == 1
    assert result.unresolved_financial_role_count == 1


def test_requested_subperiod_is_clipped_and_december_advances_to_next_year() -> None:
    workspace = _workspace(
        (
            _row("nov", date(2025, 11, 30), "10.00", FinancialRole.INCOME),
            _row("dec", date(2025, 12, 2), "-2.00", FinancialRole.EXPENSE),
            _row("jan", date(2026, 1, 2), "-3.00", FinancialRole.EXPENSE),
        ),
        start=date(2025, 11, 1),
        end=date(2026, 1, 31),
    )
    period = DateRange(start_date=date(2025, 11, 30), end_date=date(2026, 1, 2))

    result = compute_workspace_analytics(
        workspace,
        WorkspaceAnalyticsRequest(
            expected_workspace_revision=workspace.revision,
            period=period,
            largest_transaction_limit=1,
        ),
    )

    assert result.observed_transaction_count == 3
    assert len(result.largest_transactions) == 1
    assert [month.month for month in result.monthly_cash_flow] == [
        date(2025, 11, 1),
        date(2025, 12, 1),
        date(2026, 1, 1),
    ]
    assert not result.monthly_cash_flow[0].full_calendar_month
    assert result.monthly_cash_flow[1].full_calendar_month
    assert not result.monthly_cash_flow[2].full_calendar_month


def test_workspace_analytics_rejects_draft_stale_and_outside_requests() -> None:
    draft = StatementWorkspace(
        workspace_id="draft-workspace",
        retention_mode=WorkspaceRetentionMode.TEMPORARY,
        status=WorkspaceStatus.DRAFT,
        account_name="Fictional draft",
        revision=1,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(WorkspaceAnalyticsError) as draft_error:
        compute_workspace_analytics(
            draft,
            WorkspaceAnalyticsRequest(expected_workspace_revision=1),
        )
    assert draft_error.value.code is WorkspaceAnalyticsErrorCode.NOT_FINALIZED
    assert "transaction" not in str(draft_error.value).casefold()

    workspace = _workspace(
        (_row("safe", date(2026, 1, 2), "1.00", FinancialRole.INCOME),)
    )
    with pytest.raises(WorkspaceAnalyticsError) as revision_error:
        compute_workspace_analytics(
            workspace,
            WorkspaceAnalyticsRequest(expected_workspace_revision=3),
        )
    assert revision_error.value.code is WorkspaceAnalyticsErrorCode.REVISION_CONFLICT

    for period in (
        DateRange(start_date=date(2025, 12, 31), end_date=date(2026, 1, 2)),
        DateRange(start_date=date(2026, 1, 30), end_date=date(2026, 2, 1)),
    ):
        with pytest.raises(WorkspaceAnalyticsError) as period_error:
            compute_workspace_analytics(
                workspace,
                WorkspaceAnalyticsRequest(
                    expected_workspace_revision=workspace.revision,
                    period=period,
                ),
            )
        assert period_error.value.code is (
            WorkspaceAnalyticsErrorCode.PERIOD_OUTSIDE_COVERAGE
        )


def test_transaction_search_is_newest_first_filtered_and_revision_bound() -> None:
    workspace = _workspace(
        (
            _row(
                "first",
                date(2026, 1, 1),
                "-1.00",
                FinancialRole.EXPENSE,
                category="food",
                description="FICTIONAL CAFE",
            ),
            _row(
                "same-day-first",
                date(2026, 1, 3),
                "-2.00",
                FinancialRole.EXPENSE,
                category="groceries",
                description="FICTIONAL SHOP",
                merchant="FICTIONAL MARKET",
                posting_date=date(2026, 1, 4),
                external_id="fictional-external",
                transaction_type="card",
            ),
            _row(
                "same-day-second",
                date(2026, 1, 3),
                "5.00",
                FinancialRole.INCOME,
                category="income",
                description="FICTIONAL PAY",
            ),
            _row(
                "middle",
                date(2026, 1, 2),
                "-3.00",
                FinancialRole.EXPENSE,
                category="pet_care",
                description="FICTIONAL VET",
            ),
        )
    )

    first_page = search_workspace_transactions(
        workspace,
        WorkspaceTransactionSearchRequest(
            expected_workspace_revision=workspace.revision,
            pagination=Pagination(limit=2, offset=0),
        ),
    )
    assert first_page.total == 4
    assert [item.row_id for item in first_page.items] == [
        "same-day-second",
        "same-day-first",
    ]
    assert first_page.items[0].direction is Direction.INFLOW
    assert first_page.items[1].direction is Direction.OUTFLOW
    assert first_page.items[1].posting_date == date(2026, 1, 4)
    assert first_page.items[1].external_id == "fictional-external"
    assert first_page.items[1].transaction_type == "card"

    merchant_match = search_workspace_transactions(
        workspace,
        WorkspaceTransactionSearchRequest(
            expected_workspace_revision=workspace.revision,
            period=DateRange(start_date=date(2026, 1, 2), end_date=date(2026, 1, 3)),
            search_text="market",
            category_ids=("groceries",),
            financial_roles=(FinancialRole.EXPENSE,),
        ),
    )
    assert [item.row_id for item in merchant_match.items] == ["same-day-first"]
    assert merchant_match.items[0].category_name == "Groceries"

    empty_page = search_workspace_transactions(
        workspace,
        WorkspaceTransactionSearchRequest(
            expected_workspace_revision=workspace.revision,
            search_text="not present",
            pagination=Pagination(limit=10, offset=10),
        ),
    )
    assert empty_page.items == ()
    assert empty_page.total == 0

    custom_category = search_workspace_transactions(
        workspace,
        WorkspaceTransactionSearchRequest(
            expected_workspace_revision=workspace.revision,
            category_ids=("pet_care",),
        ),
    )
    assert custom_category.items[0].category_name == "Pet Care"

    with pytest.raises(WorkspaceAnalyticsError) as stale_error:
        search_workspace_transactions(
            workspace,
            WorkspaceTransactionSearchRequest(expected_workspace_revision=3),
        )
    assert stale_error.value.code is WorkspaceAnalyticsErrorCode.REVISION_CONFLICT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category_ids", ("food", "food")),
        ("financial_roles", (FinancialRole.EXPENSE, FinancialRole.EXPENSE)),
    ],
)
def test_transaction_search_contract_rejects_duplicate_filters(
    field: str,
    value: tuple[Any, ...],
) -> None:
    with pytest.raises(ValidationError, match="filters must be unique"):
        WorkspaceTransactionSearchRequest.model_validate(
            {"expected_workspace_revision": 1, field: value}
        )


def test_transaction_search_result_rejects_inconsistent_windows() -> None:
    workspace = _workspace(
        (_row("one", date(2026, 1, 1), "1.00", FinancialRole.INCOME),)
    )
    valid = search_workspace_transactions(
        workspace,
        WorkspaceTransactionSearchRequest(expected_workspace_revision=4),
    )
    payload = valid.model_dump()

    with pytest.raises(ValidationError, match="exceed the result window"):
        WorkspaceTransactionSearchResult.model_validate(
            {**payload, "limit": 1, "items": (*valid.items, *valid.items)}
        )
    with pytest.raises(ValidationError, match="exceed the result window"):
        WorkspaceTransactionSearchResult.model_validate(
            {**payload, "offset": 1, "total": 1}
        )
