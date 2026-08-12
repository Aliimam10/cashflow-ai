"""Typed contracts for coverage-aware, read-only cash-flow analytics."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.statements import BalanceSnapshotSource, DateRange
from cashflow_ai.schemas.transactions import Currency, FinancialRole, Identifier


class _AnalyticsModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class AnalyticsView(StrEnum):
    """Whether results describe one account or a consolidated account set."""

    ACCOUNT = "account"
    CONSOLIDATED = "consolidated"


class AnalyticsCoverageStatus(StrEnum):
    """Completeness of known statement coverage for a requested period."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSING = "missing"


class AnalyticsValueBasis(StrEnum):
    """Whether values cover the whole request or only observed activity."""

    COMPLETE_PERIOD = "complete_period"
    OBSERVED_ONLY = "observed_only"


class SavingsRateUnavailableReason(StrEnum):
    """Stable reason that a trustworthy savings rate cannot be calculated."""

    INCOMPLETE_COVERAGE = "incomplete_coverage"
    UNRESOLVED_FINANCIAL_ROLES = "unresolved_financial_roles"
    NO_INCOME = "no_income"


class MonthlyComparisonUnavailableReason(StrEnum):
    """Stable reason that two monthly values should not be compared."""

    PARTIAL_CALENDAR_MONTH = "partial_calendar_month"
    INCOMPLETE_COVERAGE = "incomplete_coverage"
    UNRESOLVED_FINANCIAL_ROLES = "unresolved_financial_roles"


class AnalyticsScope(_AnalyticsModel):
    """Inclusive owned-account range requested from the analytics service."""

    user_profile_id: Identifier
    account_ids: tuple[Identifier, ...] = Field(min_length=1)
    period: DateRange
    view: AnalyticsView
    largest_transaction_limit: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def validate_account_selection(self) -> AnalyticsScope:
        """Require unique accounts and exactly one account in account view."""
        if len(set(self.account_ids)) != len(self.account_ids):
            msg = "analytics account IDs must be unique"
            raise ValueError(msg)
        if self.view is AnalyticsView.ACCOUNT and len(self.account_ids) != 1:
            msg = "account analytics requires exactly one account"
            raise ValueError(msg)
        return self


class AccountCoverageIndicator(_AnalyticsModel):
    """Known and missing inclusive periods for one selected account."""

    account_id: Identifier
    status: AnalyticsCoverageStatus
    covered_periods: tuple[DateRange, ...]
    missing_periods: tuple[DateRange, ...]
    covered_days: int = Field(ge=0)
    missing_days: int = Field(ge=0)


class DataCoverageIndicator(_AnalyticsModel):
    """Coverage intersection, partial union, and gaps across the account scope."""

    requested_period: DateRange
    status: AnalyticsCoverageStatus
    fully_covered_periods: tuple[DateRange, ...]
    partially_covered_periods: tuple[DateRange, ...]
    missing_periods: tuple[DateRange, ...]
    requested_days: int = Field(ge=1)
    fully_covered_days: int = Field(ge=0)
    partially_covered_days: int = Field(ge=0)
    missing_days: int = Field(ge=0)
    accounts: tuple[AccountCoverageIndicator, ...] = Field(min_length=1)


class CashFlowTotals(_AnalyticsModel):
    """Role-aware observed cash-flow totals in one currency."""

    currency: Currency
    basis: AnalyticsValueBasis
    total_income: Money
    total_expenses: Money
    total_refunds: Money
    total_reimbursements: Money
    total_cash_withdrawals: Money
    net_cash_flow: Money
    transfer_inflow: Money
    transfer_outflow: Money
    net_transfer_movement: Money
    unknown_inflow: Money
    unknown_outflow: Money
    excluded_inflow: Money
    excluded_outflow: Money
    transaction_count: int = Field(ge=0)
    unknown_transaction_count: int = Field(ge=0)
    excluded_transaction_count: int = Field(ge=0)
    matched_internal_transfer_count: int = Field(ge=0)


class SavingsRateResult(_AnalyticsModel):
    """Savings percentage or a deterministic reason it is unavailable."""

    rate_percent: Money | None = None
    unavailable_reason: SavingsRateUnavailableReason | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> SavingsRateResult:
        """Require exactly one of a rate or an unavailable reason."""
        if (self.rate_percent is None) == (self.unavailable_reason is None):
            msg = "savings rate requires either a value or an unavailable reason"
            raise ValueError(msg)
        return self


class CategorySpending(_AnalyticsModel):
    """Observed expense-role spending for one category or uncategorised bucket."""

    category_id: str | None
    category_name: str | None
    amount: Money
    transaction_count: int = Field(ge=1)


class SpendingCadenceBreakdown(_AnalyticsModel):
    """Expense-role spending split by an explicit recurrence classification."""

    recurring: Money
    discretionary: Money
    unclassified: Money
    recurring_count: int = Field(ge=0)
    discretionary_count: int = Field(ge=0)
    unclassified_count: int = Field(ge=0)


class LargestTransaction(_AnalyticsModel):
    """One observed non-excluded transaction ranked by absolute amount."""

    transaction_id: Identifier
    account_id: Identifier
    transaction_date: date
    description: str = Field(min_length=1, max_length=500)
    amount: Money
    currency: Currency
    financial_role: FinancialRole
    category_id: str | None


class BalanceHistoryPoint(_AnalyticsModel):
    """One selected verified balance observation without interpolation."""

    snapshot_id: Identifier
    account_id: Identifier
    as_of_date: date
    balance: Money
    currency: Currency
    source: BalanceSnapshotSource


class BalanceHistorySegment(_AnalyticsModel):
    """Chart-safe balance points that may be connected only within one segment."""

    coverage_period: DateRange | None
    points: tuple[BalanceHistoryPoint, ...] = Field(min_length=1)


class AccountBalanceHistory(_AnalyticsModel):
    """Gap-preserving verified balance history for one account."""

    account_id: Identifier
    segments: tuple[BalanceHistorySegment, ...]


class MonthlyCashFlow(_AnalyticsModel):
    """One calendar-month slice, clipped to the requested period."""

    month: date
    period: DateRange
    full_calendar_month: bool
    coverage: DataCoverageIndicator
    totals: CashFlowTotals | None
    savings_rate: SavingsRateResult
    observed_transaction_count: int = Field(ge=0)


class MonthlyComparison(_AnalyticsModel):
    """Change between adjacent monthly slices when comparison is responsible."""

    previous_period: DateRange
    current_period: DateRange
    comparable: bool
    unavailable_reason: MonthlyComparisonUnavailableReason | None = None
    income_change: Money | None = None
    expense_change: Money | None = None
    net_cash_flow_change: Money | None = None


class CashFlowAnalytics(_AnalyticsModel):
    """Complete read-only analytics result for one explicit account scope."""

    scope: AnalyticsScope
    currency: Currency
    coverage: DataCoverageIndicator
    totals: CashFlowTotals | None
    savings_rate: SavingsRateResult
    category_spending: tuple[CategorySpending, ...] | None
    spending_cadence: SpendingCadenceBreakdown | None
    largest_transactions: tuple[LargestTransaction, ...]
    balance_history: tuple[AccountBalanceHistory, ...]
    monthly_cash_flow: tuple[MonthlyCashFlow, ...]
    monthly_comparisons: tuple[MonthlyComparison, ...]
    observed_transaction_count: int = Field(ge=0)
