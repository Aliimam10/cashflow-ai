"""Deterministic, read-only, coverage-aware cash-flow analytics."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from itertools import pairwise

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    BalanceSnapshotRecord,
    FinancialRoleSuggestionRecord,
    StatementCoverageRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.persistence.repositories import (
    AnalyticsRepository,
    AnalyticsTransactionRow,
    ConfirmedTransferRow,
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
    LargestTransaction,
    MonthlyCashFlow,
    MonthlyComparison,
    MonthlyComparisonUnavailableReason,
    SavingsRateResult,
    SavingsRateUnavailableReason,
    SpendingCadenceBreakdown,
)
from cashflow_ai.schemas.statements import (
    BalanceSnapshotSource,
    CoverageStatus,
    DateRange,
    StatementCoverage,
)
from cashflow_ai.schemas.transactions import Currency, FinancialRole

_ONE_DAY = timedelta(days=1)
_ZERO = Decimal("0.00")
_PERCENT_QUANTUM = Decimal("0.01")


class AnalyticsServiceErrorCode(StrEnum):
    """Stable failures at the analytics application boundary."""

    ACCOUNT_SCOPE_NOT_FOUND = "account_scope_not_found"
    MIXED_ACCOUNT_CURRENCIES = "mixed_account_currencies"
    DATA_CURRENCY_MISMATCH = "data_currency_mismatch"
    INVALID_FINANCIAL_ROLE = "invalid_financial_role"
    INVALID_FINANCIAL_ROLE_SIGN = "invalid_financial_role_sign"


class AnalyticsServiceError(ValueError):
    """Controlled analytics failure that does not disclose financial values."""

    def __init__(self, code: AnalyticsServiceErrorCode, message: str) -> None:
        """Store a stable public code beside a privacy-safe message."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _TransferLink:
    counterpart_account_id: str


def _range_days(item: DateRange) -> int:
    return (item.end_date - item.start_date).days + 1


def _ranges_days(items: Iterable[DateRange]) -> int:
    return sum(_range_days(item) for item in items)


def _merge_ranges(items: Iterable[DateRange]) -> tuple[DateRange, ...]:
    ordered = sorted(items, key=lambda item: (item.start_date, item.end_date))
    merged: list[DateRange] = []
    for current in ordered:
        if not merged or current.start_date > merged[-1].end_date + _ONE_DAY:
            merged.append(current)
            continue
        previous = merged[-1]
        merged[-1] = DateRange(
            start_date=previous.start_date,
            end_date=max(previous.end_date, current.end_date),
        )
    return tuple(merged)


def _clip_ranges(
    items: Iterable[DateRange],
    period: DateRange,
) -> tuple[DateRange, ...]:
    clipped = (
        DateRange(
            start_date=max(item.start_date, period.start_date),
            end_date=min(item.end_date, period.end_date),
        )
        for item in items
        if item.end_date >= period.start_date and item.start_date <= period.end_date
    )
    return _merge_ranges(clipped)


def _subtract_ranges(
    bases: Iterable[DateRange],
    removed: Iterable[DateRange],
) -> tuple[DateRange, ...]:
    cuts = _merge_ranges(removed)
    result: list[DateRange] = []
    for base in _merge_ranges(bases):
        cursor = base.start_date
        for cut in cuts:
            if cut.end_date < cursor:
                continue
            if cut.start_date > base.end_date:
                break
            if cut.start_date > cursor:
                result.append(
                    DateRange(
                        start_date=cursor,
                        end_date=min(base.end_date, cut.start_date - _ONE_DAY),
                    )
                )
            cursor = max(cursor, cut.end_date + _ONE_DAY)
            if cursor > base.end_date:
                break
        if cursor <= base.end_date:
            result.append(DateRange(start_date=cursor, end_date=base.end_date))
    return tuple(result)


def _intersect_ranges(
    left: Iterable[DateRange],
    right: Iterable[DateRange],
) -> tuple[DateRange, ...]:
    intersections: list[DateRange] = []
    for first in left:
        for second in right:
            start = max(first.start_date, second.start_date)
            end = min(first.end_date, second.end_date)
            if start <= end:
                intersections.append(DateRange(start_date=start, end_date=end))
    return _merge_ranges(intersections)


def _missing_ranges(
    period: DateRange,
    covered: Iterable[DateRange],
) -> tuple[DateRange, ...]:
    return _subtract_ranges((period,), covered)


def _known_ranges(record: StatementCoverageRecord) -> tuple[DateRange, ...]:
    coverage = StatementCoverage(
        statement_start_date=record.statement_start_date,
        statement_end_date=record.statement_end_date,
        status=CoverageStatus(record.coverage_status),
        missing_periods=tuple(
            DateRange.model_validate(item) for item in record.missing_periods_json
        ),
    )
    if coverage.status in {CoverageStatus.PARTIAL, CoverageStatus.UNKNOWN}:
        return ()
    whole = (
        DateRange(
            start_date=coverage.statement_start_date,
            end_date=coverage.statement_end_date,
        ),
    )
    if coverage.status is CoverageStatus.GAPPED:
        return _subtract_ranges(whole, coverage.missing_periods)
    return whole


def _coverage_status(covered_days: int, requested_days: int) -> AnalyticsCoverageStatus:
    if covered_days == requested_days:
        return AnalyticsCoverageStatus.COMPLETE
    if covered_days == 0:
        return AnalyticsCoverageStatus.MISSING
    return AnalyticsCoverageStatus.PARTIAL


def _coverage_indicator(
    account_ids: tuple[str, ...],
    ranges_by_account: dict[str, tuple[DateRange, ...]],
    period: DateRange,
) -> DataCoverageIndicator:
    requested_days = _range_days(period)
    clipped_by_account = {
        account_id: _clip_ranges(ranges_by_account[account_id], period)
        for account_id in account_ids
    }
    accounts = tuple(
        AccountCoverageIndicator(
            account_id=account_id,
            status=_coverage_status(
                _ranges_days(clipped_by_account[account_id]), requested_days
            ),
            covered_periods=clipped_by_account[account_id],
            missing_periods=_missing_ranges(
                period,
                clipped_by_account[account_id],
            ),
            covered_days=_ranges_days(clipped_by_account[account_id]),
            missing_days=requested_days - _ranges_days(clipped_by_account[account_id]),
        )
        for account_id in account_ids
    )

    union = _merge_ranges(
        item for ranges in clipped_by_account.values() for item in ranges
    )
    fully_covered = clipped_by_account[account_ids[0]]
    for account_id in account_ids[1:]:
        fully_covered = _intersect_ranges(
            fully_covered,
            clipped_by_account[account_id],
        )
    partially_covered = _subtract_ranges(union, fully_covered)
    missing = _missing_ranges(period, union)
    fully_covered_days = _ranges_days(fully_covered)
    if fully_covered_days == requested_days:
        overall_status = AnalyticsCoverageStatus.COMPLETE
    elif union:
        overall_status = AnalyticsCoverageStatus.PARTIAL
    else:
        overall_status = AnalyticsCoverageStatus.MISSING
    return DataCoverageIndicator(
        requested_period=period,
        status=overall_status,
        fully_covered_periods=fully_covered,
        partially_covered_periods=partially_covered,
        missing_periods=missing,
        requested_days=requested_days,
        fully_covered_days=fully_covered_days,
        partially_covered_days=_ranges_days(partially_covered),
        missing_days=_ranges_days(missing),
        accounts=accounts,
    )


def _coverage_by_account(
    account_ids: tuple[str, ...],
    rows: Iterable[tuple[str, StatementCoverageRecord]],
    period: DateRange,
) -> dict[str, tuple[DateRange, ...]]:
    collected: dict[str, list[DateRange]] = {
        account_id: [] for account_id in account_ids
    }
    for account_id, record in rows:
        collected[account_id].extend(_known_ranges(record))
    return {
        account_id: _clip_ranges(items, period)
        for account_id, items in collected.items()
    }


def _financial_role(record: VerifiedTransactionRecord) -> FinancialRole:
    try:
        return FinancialRole(record.financial_role_id)
    except ValueError as exc:
        raise AnalyticsServiceError(
            AnalyticsServiceErrorCode.INVALID_FINANCIAL_ROLE,
            "transaction has an unsupported financial role",
        ) from exc


def _validate_role_sign(record: VerifiedTransactionRecord) -> None:
    role = _financial_role(record)
    positive_roles = {
        FinancialRole.INCOME,
        FinancialRole.TRANSFER_IN,
        FinancialRole.REFUND,
        FinancialRole.REIMBURSEMENT,
    }
    negative_roles = {
        FinancialRole.EXPENSE,
        FinancialRole.TRANSFER_OUT,
        FinancialRole.CASH_WITHDRAWAL,
    }
    if (role in positive_roles and record.amount <= 0) or (
        role in negative_roles and record.amount >= 0
    ):
        raise AnalyticsServiceError(
            AnalyticsServiceErrorCode.INVALID_FINANCIAL_ROLE_SIGN,
            "transaction amount sign is incompatible with its financial role",
        )


def _transfer_links(rows: Iterable[ConfirmedTransferRow]) -> dict[str, _TransferLink]:
    links: dict[str, _TransferLink] = {}
    for suggestion, subject, counterpart in rows:
        if not _valid_transfer_pair(suggestion, subject, counterpart):
            continue
        links.setdefault(
            subject.id,
            _TransferLink(counterpart_account_id=counterpart.account_id),
        )
        links.setdefault(
            counterpart.id,
            _TransferLink(counterpart_account_id=subject.account_id),
        )
    return links


def _valid_transfer_pair(
    suggestion: FinancialRoleSuggestionRecord,
    subject: VerifiedTransactionRecord,
    counterpart: VerifiedTransactionRecord,
) -> bool:
    try:
        subject_role = FinancialRole(subject.financial_role_id)
        counterpart_role = FinancialRole(counterpart.financial_role_id)
    except ValueError:
        return False
    return (
        subject.financial_role_id == suggestion.suggested_role_id
        and counterpart.financial_role_id == suggestion.counterpart_role_id
        and {subject_role, counterpart_role}
        == {FinancialRole.TRANSFER_IN, FinancialRole.TRANSFER_OUT}
        and subject.account_id != counterpart.account_id
        and subject.currency == counterpart.currency
        and subject.amount == -counterpart.amount
    )


def _totals(
    rows: tuple[AnalyticsTransactionRow, ...],
    *,
    coverage_status: AnalyticsCoverageStatus,
    currency: Currency,
    scope: AnalyticsScope,
    transfer_links: dict[str, _TransferLink],
) -> CashFlowTotals | None:
    if coverage_status is AnalyticsCoverageStatus.MISSING:
        return None

    income = expenses = refunds = reimbursements = withdrawals = _ZERO
    transfer_in = transfer_out = _ZERO
    unknown_in = unknown_out = excluded_in = excluded_out = _ZERO
    unknown_count = excluded_count = matched_internal_count = 0
    selected_accounts = set(scope.account_ids)
    for transaction, _category in rows:
        role = _financial_role(transaction)
        amount = transaction.amount
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
        elif role in {FinancialRole.TRANSFER_IN, FinancialRole.TRANSFER_OUT}:
            link = transfer_links.get(transaction.id)
            matched_internal = (
                link is not None and link.counterpart_account_id in selected_accounts
            )
            if matched_internal:
                matched_internal_count += 1
            hidden_consolidated = (
                scope.view is AnalyticsView.CONSOLIDATED and matched_internal
            )
            if not hidden_consolidated:
                if role is FinancialRole.TRANSFER_IN:
                    transfer_in += amount
                else:
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
        matched_internal_transfer_count=matched_internal_count,
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
            unavailable_reason=(SavingsRateUnavailableReason.UNRESOLVED_FINANCIAL_ROLES)
        )
    if totals.total_income <= 0:
        return SavingsRateResult(
            unavailable_reason=SavingsRateUnavailableReason.NO_INCOME
        )
    rate = (totals.net_cash_flow / totals.total_income * 100).quantize(
        _PERCENT_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    return SavingsRateResult(rate_percent=rate)


def _category_spending(
    rows: tuple[AnalyticsTransactionRow, ...],
) -> tuple[CategorySpending, ...]:
    grouped: dict[tuple[str | None, str | None], tuple[Decimal, int]] = {}
    for transaction, category in rows:
        if _financial_role(transaction) is not FinancialRole.EXPENSE:
            continue
        key = (
            category.id if category is not None else None,
            category.name if category is not None else None,
        )
        amount, count = grouped.get(key, (_ZERO, 0))
        grouped[key] = (amount - transaction.amount, count + 1)
    result = (
        CategorySpending(
            category_id=category_id,
            category_name=category_name,
            amount=amount,
            transaction_count=count,
        )
        for (category_id, category_name), (amount, count) in grouped.items()
    )
    return tuple(
        sorted(
            result,
            key=lambda item: (-item.amount, item.category_id or ""),
        )
    )


def _spending_cadence(
    rows: tuple[AnalyticsTransactionRow, ...],
) -> SpendingCadenceBreakdown:
    expenses = tuple(
        transaction
        for transaction, _category in rows
        if _financial_role(transaction) is FinancialRole.EXPENSE
    )
    return SpendingCadenceBreakdown(
        recurring=_ZERO,
        discretionary=_ZERO,
        unclassified=sum((-item.amount for item in expenses), start=_ZERO),
        recurring_count=0,
        discretionary_count=0,
        unclassified_count=len(expenses),
    )


def _largest_transactions(
    rows: tuple[AnalyticsTransactionRow, ...],
    limit: int,
) -> tuple[LargestTransaction, ...]:
    included = tuple(
        (transaction, category)
        for transaction, category in rows
        if _financial_role(transaction) is not FinancialRole.EXCLUDED
    )
    ordered = sorted(
        included,
        key=lambda item: (
            -abs(item[0].amount),
            item[0].transaction_date,
            item[0].account_id,
            item[0].id,
        ),
    )
    return tuple(
        LargestTransaction(
            transaction_id=transaction.id,
            account_id=transaction.account_id,
            transaction_date=transaction.transaction_date,
            description=transaction.description,
            amount=transaction.amount,
            currency=Currency(transaction.currency),
            financial_role=_financial_role(transaction),
            category_id=category.id if category is not None else None,
        )
        for transaction, category in ordered[:limit]
    )


def _balance_history(
    account_ids: tuple[str, ...],
    records: tuple[BalanceSnapshotRecord, ...],
    ranges_by_account: dict[str, tuple[DateRange, ...]],
    currency: Currency,
) -> tuple[AccountBalanceHistory, ...]:
    selected: list[BalanceSnapshotRecord] = []
    seen_dates: set[tuple[str, date]] = set()
    for record in records:
        if record.currency != currency.value:
            raise AnalyticsServiceError(
                AnalyticsServiceErrorCode.DATA_CURRENCY_MISMATCH,
                "balance currency does not match the analytics scope",
            )
        key = (record.account_id, record.as_of_date)
        if key not in seen_dates:
            selected.append(record)
            seen_dates.add(key)

    histories: list[AccountBalanceHistory] = []
    for account_id in account_ids:
        covered_groups: dict[int, list[BalanceHistoryPoint]] = defaultdict(list)
        standalone: list[BalanceHistorySegment] = []
        coverage_ranges = ranges_by_account[account_id]
        for record in selected:
            if record.account_id != account_id:
                continue
            point = BalanceHistoryPoint(
                snapshot_id=record.id,
                account_id=record.account_id,
                as_of_date=record.as_of_date,
                balance=record.balance,
                currency=Currency(record.currency),
                source=BalanceSnapshotSource(record.source),
            )
            range_index = next(
                (
                    index
                    for index, item in enumerate(coverage_ranges)
                    if item.start_date <= record.as_of_date <= item.end_date
                ),
                None,
            )
            if range_index is None:
                standalone.append(
                    BalanceHistorySegment(coverage_period=None, points=(point,))
                )
            else:
                covered_groups[range_index].append(point)

        segments = [
            BalanceHistorySegment(
                coverage_period=coverage_ranges[index],
                points=tuple(points),
            )
            for index, points in covered_groups.items()
        ]
        segments.extend(standalone)
        segments.sort(
            key=lambda item: (
                item.points[0].as_of_date,
                item.points[0].snapshot_id,
            )
        )
        histories.append(
            AccountBalanceHistory(account_id=account_id, segments=tuple(segments))
        )
    return tuple(histories)


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
    scope: AnalyticsScope,
    rows: tuple[AnalyticsTransactionRow, ...],
    ranges_by_account: dict[str, tuple[DateRange, ...]],
    currency: Currency,
    transfer_links: dict[str, _TransferLink],
) -> tuple[MonthlyCashFlow, ...]:
    results: list[MonthlyCashFlow] = []
    for month, period, full_month in _calendar_months(scope.period):
        coverage = _coverage_indicator(scope.account_ids, ranges_by_account, period)
        monthly_rows = tuple(
            row
            for row in rows
            if period.start_date <= row[0].transaction_date <= period.end_date
        )
        totals = _totals(
            monthly_rows,
            coverage_status=coverage.status,
            currency=currency,
            scope=scope,
            transfer_links=transfer_links,
        )
        results.append(
            MonthlyCashFlow(
                month=month,
                period=period,
                full_calendar_month=full_month,
                coverage=coverage,
                totals=totals,
                savings_rate=_savings_rate(totals, coverage.status),
                observed_transaction_count=len(monthly_rows),
            )
        )
    return tuple(results)


def _monthly_comparisons(
    months: tuple[MonthlyCashFlow, ...],
) -> tuple[MonthlyComparison, ...]:
    comparisons: list[MonthlyComparison] = []
    for previous, current in pairwise(months):
        reason: MonthlyComparisonUnavailableReason | None = None
        if not previous.full_calendar_month or not current.full_calendar_month:
            reason = MonthlyComparisonUnavailableReason.PARTIAL_CALENDAR_MONTH
        elif (
            previous.coverage.status is not AnalyticsCoverageStatus.COMPLETE
            or current.coverage.status is not AnalyticsCoverageStatus.COMPLETE
            or previous.totals is None
            or current.totals is None
        ):
            reason = MonthlyComparisonUnavailableReason.INCOMPLETE_COVERAGE
        elif (
            previous.totals.unknown_transaction_count
            or current.totals.unknown_transaction_count
        ):
            reason = MonthlyComparisonUnavailableReason.UNRESOLVED_FINANCIAL_ROLES

        if reason is not None:
            comparisons.append(
                MonthlyComparison(
                    previous_period=previous.period,
                    current_period=current.period,
                    comparable=False,
                    unavailable_reason=reason,
                )
            )
            continue
        assert previous.totals is not None
        assert current.totals is not None
        comparisons.append(
            MonthlyComparison(
                previous_period=previous.period,
                current_period=current.period,
                comparable=True,
                income_change=(
                    current.totals.total_income - previous.totals.total_income
                ),
                expense_change=(
                    current.totals.total_expenses - previous.totals.total_expenses
                ),
                net_cash_flow_change=(
                    current.totals.net_cash_flow - previous.totals.net_cash_flow
                ),
            )
        )
    return tuple(comparisons)


def _validate_currencies(
    rows: Iterable[AnalyticsTransactionRow],
    currency: Currency,
) -> None:
    for transaction, _category in rows:
        if transaction.currency != currency.value:
            raise AnalyticsServiceError(
                AnalyticsServiceErrorCode.DATA_CURRENCY_MISMATCH,
                "transaction currency does not match the analytics scope",
            )
        _validate_role_sign(transaction)


def compute_cash_flow_analytics(
    factory: sessionmaker[Session],
    scope: AnalyticsScope,
) -> CashFlowAnalytics:
    """Compute deterministic analytics without changing persisted records."""
    with session_scope(factory) as session:
        repository = AnalyticsRepository(session)
        accounts = repository.list_owned_accounts(
            scope.user_profile_id,
            scope.account_ids,
        )
        if {account.id for account in accounts} != set(scope.account_ids):
            raise AnalyticsServiceError(
                AnalyticsServiceErrorCode.ACCOUNT_SCOPE_NOT_FOUND,
                "one or more selected accounts are unavailable to this profile",
            )
        account_currencies = {account.currency for account in accounts}
        if len(account_currencies) != 1:
            raise AnalyticsServiceError(
                AnalyticsServiceErrorCode.MIXED_ACCOUNT_CURRENCIES,
                "selected accounts must share one currency",
            )
        currency = Currency(account_currencies.pop())
        transaction_rows = repository.list_transactions(
            user_profile_id=scope.user_profile_id,
            account_ids=scope.account_ids,
            start_date=scope.period.start_date,
            end_date=scope.period.end_date,
        )
        coverage_rows = repository.list_verified_coverages(
            user_profile_id=scope.user_profile_id,
            account_ids=scope.account_ids,
            start_date=scope.period.start_date,
            end_date=scope.period.end_date,
        )
        balance_records = repository.list_verified_balances(
            user_profile_id=scope.user_profile_id,
            account_ids=scope.account_ids,
            start_date=scope.period.start_date,
            end_date=scope.period.end_date,
        )
        pair_rows = repository.list_confirmed_transfer_pairs(
            tuple(row[0].id for row in transaction_rows)
        )

    _validate_currencies(transaction_rows, currency)
    ranges_by_account = _coverage_by_account(
        scope.account_ids,
        coverage_rows,
        scope.period,
    )
    coverage = _coverage_indicator(
        scope.account_ids,
        ranges_by_account,
        scope.period,
    )
    transfer_links = _transfer_links(pair_rows)
    totals = _totals(
        transaction_rows,
        coverage_status=coverage.status,
        currency=currency,
        scope=scope,
        transfer_links=transfer_links,
    )
    data_available = totals is not None
    months = _monthly_cash_flow(
        scope,
        transaction_rows,
        ranges_by_account,
        currency,
        transfer_links,
    )
    return CashFlowAnalytics(
        scope=scope,
        currency=currency,
        coverage=coverage,
        totals=totals,
        savings_rate=_savings_rate(totals, coverage.status),
        category_spending=(
            _category_spending(transaction_rows) if data_available else None
        ),
        spending_cadence=(
            _spending_cadence(transaction_rows) if data_available else None
        ),
        largest_transactions=(
            _largest_transactions(transaction_rows, scope.largest_transaction_limit)
            if data_available
            else ()
        ),
        balance_history=_balance_history(
            scope.account_ids,
            balance_records,
            ranges_by_account,
            currency,
        ),
        monthly_cash_flow=months,
        monthly_comparisons=_monthly_comparisons(months),
        observed_transaction_count=len(transaction_rows),
    )
