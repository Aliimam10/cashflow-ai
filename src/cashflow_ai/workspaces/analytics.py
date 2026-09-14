"""Pure analytics and transaction search over finalized workspace values."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import cast

from cashflow_ai.schemas.analytics import (
    AccountBalanceHistory,
    AccountCoverageIndicator,
    AnalyticsCoverageStatus,
    AnalyticsValueBasis,
    BalanceHistoryPoint,
    BalanceHistorySegment,
    CashFlowTotals,
    CategorySpending,
    DataCoverageIndicator,
    LargestTransaction,
    MonthlyCashFlow,
    SavingsRateResult,
    SavingsRateUnavailableReason,
    SpendingCadenceBreakdown,
)
from cashflow_ai.schemas.statements import (
    BalanceSnapshotSource,
    CoverageStatus,
    DateRange,
)
from cashflow_ai.schemas.transactions import Currency, Direction, FinancialRole
from cashflow_ai.schemas.workspace_analytics import (
    WorkspaceAnalytics,
    WorkspaceAnalyticsRequest,
    WorkspaceTransactionSearchRequest,
    WorkspaceTransactionSearchResult,
    WorkspaceTransactionView,
)
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceCoverageConfirmation,
    WorkspaceStatus,
    WorkspaceTransactionRow,
)

_ONE_DAY = timedelta(days=1)
_ZERO = Decimal("0.00")
_PERCENT_QUANTUM = Decimal("0.01")
_CATEGORY_NAMES = {
    "food": "Food",
    "groceries": "Groceries",
    "restaurants": "Restaurants",
    "eating_out": "Restaurants",
    "transport": "Transport",
    "housing": "Rent/Housing",
    "utilities": "Bills",
    "entertainment": "Entertainment",
    "shopping": "Shopping",
    "health": "Health",
    "income": "Income",
    "transfers": "Transfers",
    "other": "Other",
    "needs_review": "Needs Review",
}


class WorkspaceAnalyticsErrorCode(StrEnum):
    """Stable privacy-safe failures from finalized-workspace calculations."""

    NOT_FINALIZED = "workspace_not_finalized"
    REVISION_CONFLICT = "workspace_revision_conflict"
    PERIOD_OUTSIDE_COVERAGE = "workspace_period_outside_coverage"


class WorkspaceAnalyticsError(ValueError):
    """Controlled workspace-insight failure without private transaction values."""

    def __init__(self, code: WorkspaceAnalyticsErrorCode, message: str) -> None:
        """Store a stable public code beside a data-minimised message."""
        super().__init__(message)
        self.code = code


def _require_finalized(
    workspace: StatementWorkspace,
    expected_revision: int,
) -> None:
    if workspace.status is not WorkspaceStatus.FINALIZED:
        raise WorkspaceAnalyticsError(
            WorkspaceAnalyticsErrorCode.NOT_FINALIZED,
            "finalize the statement workspace before requesting results",
        )
    if workspace.revision != expected_revision:
        raise WorkspaceAnalyticsError(
            WorkspaceAnalyticsErrorCode.REVISION_CONFLICT,
            "the statement workspace changed; refresh it before requesting results",
        )


def _request_period(
    workspace: StatementWorkspace,
    requested: DateRange | None,
) -> DateRange:
    coverage = cast(WorkspaceCoverageConfirmation, workspace.coverage)
    whole = DateRange(start_date=coverage.start_date, end_date=coverage.end_date)
    if requested is None:
        return whole
    if requested.start_date < whole.start_date or requested.end_date > whole.end_date:
        raise WorkspaceAnalyticsError(
            WorkspaceAnalyticsErrorCode.PERIOD_OUTSIDE_COVERAGE,
            "the requested period must remain inside confirmed workspace coverage",
        )
    return requested


def _range_days(item: DateRange) -> int:
    return (item.end_date - item.start_date).days + 1


def _known_periods(workspace: StatementWorkspace) -> tuple[DateRange, ...]:
    coverage = cast(WorkspaceCoverageConfirmation, workspace.coverage)
    whole = DateRange(start_date=coverage.start_date, end_date=coverage.end_date)
    if coverage.status in {CoverageStatus.COMPLETE, CoverageStatus.OVERLAPPING}:
        return (whole,)
    if coverage.status is not CoverageStatus.GAPPED:
        return ()

    known: list[DateRange] = []
    cursor = whole.start_date
    for gap in coverage.missing_periods:
        if cursor < gap.start_date:
            known.append(
                DateRange(start_date=cursor, end_date=gap.start_date - _ONE_DAY)
            )
        cursor = gap.end_date + _ONE_DAY
    if cursor <= whole.end_date:
        known.append(DateRange(start_date=cursor, end_date=whole.end_date))
    return tuple(known)


def _clip_periods(
    periods: tuple[DateRange, ...],
    requested: DateRange,
) -> tuple[DateRange, ...]:
    return tuple(
        DateRange(
            start_date=max(item.start_date, requested.start_date),
            end_date=min(item.end_date, requested.end_date),
        )
        for item in periods
        if item.end_date >= requested.start_date
        and item.start_date <= requested.end_date
    )


def _missing_periods(
    requested: DateRange,
    covered: tuple[DateRange, ...],
) -> tuple[DateRange, ...]:
    missing: list[DateRange] = []
    cursor = requested.start_date
    for item in covered:
        if cursor < item.start_date:
            missing.append(
                DateRange(start_date=cursor, end_date=item.start_date - _ONE_DAY)
            )
        cursor = item.end_date + _ONE_DAY
    if cursor <= requested.end_date:
        missing.append(DateRange(start_date=cursor, end_date=requested.end_date))
    return tuple(missing)


def _coverage_indicator(
    workspace: StatementWorkspace,
    period: DateRange,
) -> DataCoverageIndicator:
    covered = _clip_periods(_known_periods(workspace), period)
    requested_days = _range_days(period)
    covered_days = sum((_range_days(item) for item in covered), start=0)
    if covered_days == requested_days:
        status = AnalyticsCoverageStatus.COMPLETE
    elif covered_days:
        status = AnalyticsCoverageStatus.PARTIAL
    else:
        status = AnalyticsCoverageStatus.MISSING
    missing = _missing_periods(period, covered)
    account = AccountCoverageIndicator(
        account_id=workspace.workspace_id,
        status=status,
        covered_periods=covered,
        missing_periods=missing,
        covered_days=covered_days,
        missing_days=requested_days - covered_days,
    )
    return DataCoverageIndicator(
        requested_period=period,
        status=status,
        fully_covered_periods=covered,
        partially_covered_periods=(),
        missing_periods=missing,
        requested_days=requested_days,
        fully_covered_days=covered_days,
        partially_covered_days=0,
        missing_days=requested_days - covered_days,
        accounts=(account,),
    )


def _rows_in_period(
    workspace: StatementWorkspace,
    period: DateRange,
) -> tuple[WorkspaceTransactionRow, ...]:
    return tuple(
        row
        for row in workspace.rows
        if period.start_date <= cast(date, row.transaction_date) <= period.end_date
    )


def _totals(
    rows: tuple[WorkspaceTransactionRow, ...],
    coverage_status: AnalyticsCoverageStatus,
    currency: Currency,
) -> CashFlowTotals | None:
    if coverage_status is AnalyticsCoverageStatus.MISSING:
        return None

    income = expenses = refunds = reimbursements = withdrawals = _ZERO
    transfer_in = transfer_out = _ZERO
    unknown_in = unknown_out = excluded_in = excluded_out = _ZERO
    unknown_count = excluded_count = 0
    for row in rows:
        amount = cast(Decimal, row.amount)
        role = row.financial_role
        if role is FinancialRole.INCOME:
            income += amount
        elif role is FinancialRole.EXPENSE:
            expenses -= amount
        elif role is FinancialRole.REFUND:
            refunds += amount
        elif role is FinancialRole.REIMBURSEMENT:
            reimbursements += amount
        elif role is FinancialRole.CASH_WITHDRAWAL:
            withdrawals -= amount
        elif role is FinancialRole.TRANSFER_IN:
            transfer_in += amount
        elif role is FinancialRole.TRANSFER_OUT:
            transfer_out -= amount
        elif role is FinancialRole.UNKNOWN:
            unknown_count += 1
            if amount > 0:
                unknown_in += amount
            else:
                unknown_out -= amount
        else:
            excluded_count += 1
            if amount > 0:
                excluded_in += amount
            else:
                excluded_out -= amount

    return CashFlowTotals(
        currency=currency,
        basis=(
            AnalyticsValueBasis.COMPLETE_PERIOD
            if coverage_status is AnalyticsCoverageStatus.COMPLETE
            else AnalyticsValueBasis.OBSERVED_ONLY
        ),
        total_income=income,
        total_expenses=expenses,
        total_refunds=refunds,
        total_reimbursements=reimbursements,
        total_cash_withdrawals=withdrawals,
        net_cash_flow=income + refunds + reimbursements - expenses - withdrawals,
        transfer_inflow=transfer_in,
        transfer_outflow=transfer_out,
        net_transfer_movement=transfer_in - transfer_out,
        unknown_inflow=unknown_in,
        unknown_outflow=unknown_out,
        excluded_inflow=excluded_in,
        excluded_outflow=excluded_out,
        transaction_count=len(rows),
        unknown_transaction_count=unknown_count,
        excluded_transaction_count=excluded_count,
        matched_internal_transfer_count=0,
    )


def _savings_rate(
    totals: CashFlowTotals | None,
    coverage_status: AnalyticsCoverageStatus,
) -> SavingsRateResult:
    if coverage_status is not AnalyticsCoverageStatus.COMPLETE or totals is None:
        return SavingsRateResult(
            unavailable_reason=SavingsRateUnavailableReason.INCOMPLETE_COVERAGE
        )
    if totals.unknown_transaction_count:
        return SavingsRateResult(
            unavailable_reason=SavingsRateUnavailableReason.UNRESOLVED_FINANCIAL_ROLES
        )
    if totals.total_income <= 0:
        return SavingsRateResult(
            unavailable_reason=SavingsRateUnavailableReason.NO_INCOME
        )
    return SavingsRateResult(
        rate_percent=(totals.net_cash_flow / totals.total_income * 100).quantize(
            _PERCENT_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    )


def _category_name(category_id: str) -> str:
    return _CATEGORY_NAMES.get(category_id, category_id.replace("_", " ").title())


def _category_spending(
    rows: tuple[WorkspaceTransactionRow, ...],
) -> tuple[CategorySpending, ...]:
    grouped: dict[str, tuple[Decimal, int]] = {}
    for row in rows:
        if row.financial_role is not FinancialRole.EXPENSE:
            continue
        amount, count = grouped.get(row.category_id, (_ZERO, 0))
        grouped[row.category_id] = (amount - cast(Decimal, row.amount), count + 1)
    return tuple(
        sorted(
            (
                CategorySpending(
                    category_id=category_id,
                    category_name=_category_name(category_id),
                    amount=amount,
                    transaction_count=count,
                )
                for category_id, (amount, count) in grouped.items()
            ),
            key=lambda item: (-item.amount, item.category_id or ""),
        )
    )


def _spending_cadence(
    rows: tuple[WorkspaceTransactionRow, ...],
) -> SpendingCadenceBreakdown:
    expenses = tuple(row for row in rows if row.financial_role is FinancialRole.EXPENSE)
    return SpendingCadenceBreakdown(
        recurring=_ZERO,
        discretionary=_ZERO,
        unclassified=sum(
            (-cast(Decimal, row.amount) for row in expenses),
            start=_ZERO,
        ),
        recurring_count=0,
        discretionary_count=0,
        unclassified_count=len(expenses),
    )


def _largest_transactions(
    workspace: StatementWorkspace,
    rows: tuple[WorkspaceTransactionRow, ...],
    limit: int,
) -> tuple[LargestTransaction, ...]:
    included = tuple(
        row for row in rows if row.financial_role is not FinancialRole.EXCLUDED
    )
    ordered = sorted(
        included,
        key=lambda row: (
            -abs(cast(Decimal, row.amount)),
            cast(date, row.transaction_date),
            row.row_id,
        ),
    )
    return tuple(
        LargestTransaction(
            transaction_id=row.row_id,
            account_id=workspace.workspace_id,
            transaction_date=cast(date, row.transaction_date),
            description=cast(str, row.description),
            amount=cast(Decimal, row.amount),
            currency=row.currency,
            financial_role=row.financial_role,
            category_id=row.category_id,
        )
        for row in ordered[:limit]
    )


def _balance_history(
    workspace: StatementWorkspace,
    rows: tuple[WorkspaceTransactionRow, ...],
    period: DateRange,
) -> tuple[AccountBalanceHistory, ...]:
    point_records: list[tuple[int, BalanceHistoryPoint]] = []
    for position, row in enumerate(rows):
        if row.balance_after is None:
            continue
        row_date = cast(date, row.transaction_date)
        point_records.append(
            (
                position,
                BalanceHistoryPoint(
                    snapshot_id=row.row_id,
                    account_id=workspace.workspace_id,
                    as_of_date=row_date,
                    balance=row.balance_after,
                    currency=row.currency,
                    source=BalanceSnapshotSource.RUNNING_BALANCE,
                ),
            )
        )

    confirmed = workspace.balance
    if confirmed is not None and (
        period.start_date <= confirmed.as_of_date <= period.end_date
    ):
        # Keep every same-day running-balance observation.  The separately
        # confirmed balance is appended last for its date so the headline uses
        # the user's explicit closing evidence without discarding source points.
        point_records.append(
            (
                len(rows),
                BalanceHistoryPoint(
                    snapshot_id="confirmed-workspace-balance",
                    account_id=workspace.workspace_id,
                    as_of_date=confirmed.as_of_date,
                    balance=confirmed.balance,
                    currency=confirmed.currency,
                    source=BalanceSnapshotSource.MANUAL,
                ),
            )
        )
    if not point_records:
        return ()

    covered = _clip_periods(_known_periods(workspace), period)
    covered_groups: dict[int, list[tuple[int, BalanceHistoryPoint]]] = defaultdict(list)
    standalone: list[tuple[int, BalanceHistorySegment]] = []
    for position, point in sorted(
        point_records,
        key=lambda item: (item[1].as_of_date, item[0]),
    ):
        range_index = next(
            (
                index
                for index, item in enumerate(covered)
                if item.start_date <= point.as_of_date <= item.end_date
            ),
            None,
        )
        if range_index is None:
            standalone.append(
                (
                    position,
                    BalanceHistorySegment(coverage_period=None, points=(point,)),
                )
            )
        else:
            covered_groups[range_index].append((position, point))

    segments: list[tuple[int, BalanceHistorySegment]] = [
        (
            points[0][0],
            BalanceHistorySegment(
                coverage_period=covered[index],
                points=tuple(point for _position, point in points),
            ),
        )
        for index, points in covered_groups.items()
    ]
    segments.extend(standalone)
    segments.sort(key=lambda item: (item[1].points[0].as_of_date, item[0]))
    return (
        AccountBalanceHistory(
            account_id=workspace.workspace_id,
            segments=tuple(segment for _position, segment in segments),
        ),
    )


def _calendar_months(period: DateRange) -> tuple[tuple[date, DateRange, bool], ...]:
    month = date(period.start_date.year, period.start_date.month, 1)
    result: list[tuple[date, DateRange, bool]] = []
    while month <= period.end_date:
        next_month = (
            date(month.year + 1, 1, 1)
            if month.month == 12
            else date(month.year, month.month + 1, 1)
        )
        calendar_end = next_month - _ONE_DAY
        clipped = DateRange(
            start_date=max(month, period.start_date),
            end_date=min(calendar_end, period.end_date),
        )
        result.append(
            (
                month,
                clipped,
                clipped.start_date == month and clipped.end_date == calendar_end,
            )
        )
        month = next_month
    return tuple(result)


def _monthly_cash_flow(
    workspace: StatementWorkspace,
    rows: tuple[WorkspaceTransactionRow, ...],
    period: DateRange,
) -> tuple[MonthlyCashFlow, ...]:
    months: list[MonthlyCashFlow] = []
    for month, month_period, full_month in _calendar_months(period):
        coverage = _coverage_indicator(workspace, month_period)
        month_rows = tuple(
            row
            for row in rows
            if month_period.start_date
            <= cast(date, row.transaction_date)
            <= month_period.end_date
        )
        totals = _totals(month_rows, coverage.status, workspace.currency)
        months.append(
            MonthlyCashFlow(
                month=month,
                period=month_period,
                full_calendar_month=full_month,
                coverage=coverage,
                totals=totals,
                savings_rate=_savings_rate(totals, coverage.status),
                observed_transaction_count=len(month_rows),
            )
        )
    return tuple(months)


def compute_workspace_analytics(
    workspace: StatementWorkspace,
    request: WorkspaceAnalyticsRequest,
) -> WorkspaceAnalytics:
    """Calculate deterministic insights without persistence or legacy demo data."""
    _require_finalized(workspace, request.expected_workspace_revision)
    period = _request_period(workspace, request.period)
    rows = _rows_in_period(workspace, period)
    coverage = _coverage_indicator(workspace, period)
    totals = _totals(rows, coverage.status, workspace.currency)
    data_available = totals is not None
    return WorkspaceAnalytics(
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        account_name=workspace.account_name,
        period=period,
        currency=workspace.currency,
        coverage=coverage,
        totals=totals,
        savings_rate=_savings_rate(totals, coverage.status),
        category_spending=_category_spending(rows) if data_available else None,
        spending_cadence=_spending_cadence(rows) if data_available else None,
        largest_transactions=(
            _largest_transactions(workspace, rows, request.largest_transaction_limit)
            if data_available
            else ()
        ),
        balance_history=_balance_history(workspace, rows, period),
        monthly_cash_flow=_monthly_cash_flow(workspace, rows, period),
        observed_transaction_count=len(rows),
        unresolved_financial_role_count=sum(
            row.financial_role is FinancialRole.UNKNOWN for row in rows
        ),
    )


def _transaction_view(
    workspace: StatementWorkspace,
    row: WorkspaceTransactionRow,
) -> WorkspaceTransactionView:
    amount = cast(Decimal, row.amount)
    return WorkspaceTransactionView(
        row_id=row.row_id,
        transaction_date=cast(date, row.transaction_date),
        posting_date=row.posting_date,
        description=cast(str, row.description),
        merchant=row.merchant,
        amount=amount,
        balance_after=row.balance_after,
        currency=row.currency,
        direction=Direction.INFLOW if amount > 0 else Direction.OUTFLOW,
        category_id=row.category_id,
        category_name=_category_name(row.category_id),
        financial_role=row.financial_role,
        external_id=row.external_id,
        transaction_type=row.transaction_type,
    )


def search_workspace_transactions(
    workspace: StatementWorkspace,
    request: WorkspaceTransactionSearchRequest,
) -> WorkspaceTransactionSearchResult:
    """Filter approved rows, then return a stable newest-first bounded page."""
    _require_finalized(workspace, request.expected_workspace_revision)
    period = _request_period(workspace, request.period)
    search = request.search_text.casefold() if request.search_text is not None else None
    indexed_rows = tuple(enumerate(_rows_in_period(workspace, period)))
    filtered = tuple(
        (position, row)
        for position, row in indexed_rows
        if (
            search is None
            or search in cast(str, row.description).casefold()
            or (row.merchant is not None and search in row.merchant.casefold())
        )
        and (request.category_ids is None or row.category_id in request.category_ids)
        and (
            request.financial_roles is None
            or row.financial_role in request.financial_roles
        )
    )
    ordered = sorted(
        filtered,
        key=lambda item: (cast(date, item[1].transaction_date), item[0]),
        reverse=True,
    )
    pagination = request.pagination
    selected = ordered[pagination.offset : pagination.offset + pagination.limit]
    return WorkspaceTransactionSearchResult(
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        account_name=workspace.account_name,
        currency=workspace.currency,
        items=tuple(_transaction_view(workspace, row) for _position, row in selected),
        limit=pagination.limit,
        offset=pagination.offset,
        total=len(ordered),
    )


__all__ = [
    "WorkspaceAnalyticsError",
    "WorkspaceAnalyticsErrorCode",
    "compute_workspace_analytics",
    "search_workspace_transactions",
]
