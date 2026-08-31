"""Typed contracts for coverage-aware budgets, goals, and safe spending."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.analytics import (
    AnalyticsCoverageStatus,
    DataCoverageIndicator,
)
from cashflow_ai.schemas.forecast_paths import ForecastPathWarningCode
from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.statements import DateRange
from cashflow_ai.schemas.transactions import Currency, Identifier


class _PlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class BudgetType(StrEnum):
    """Supported persisted budget periods and spending scopes."""

    MONTHLY_CATEGORY = "monthly_category"
    WEEKLY_DISCRETIONARY = "weekly_discretionary"


class FinancialGoalType(StrEnum):
    """Supported savings and balance-floor goals."""

    SAVINGS_TARGET = "savings_target"
    MINIMUM_BALANCE = "minimum_balance"


class PlanningWarningCode(StrEnum):
    """Stable limitations and projected shortfalls for user review."""

    INCOMPLETE_TRANSACTION_COVERAGE = "incomplete_transaction_coverage"
    PROJECTED_CATEGORY_BUDGET_SHORTFALL = "projected_category_budget_shortfall"
    PROJECTED_WEEKLY_BUDGET_SHORTFALL = "projected_weekly_budget_shortfall"
    MINIMUM_BALANCE_SHORTFALL = "minimum_balance_shortfall"
    SAVINGS_CONTRIBUTION_SHORTFALL = "savings_contribution_shortfall"
    OVERDUE_SAVINGS_TARGET = "overdue_savings_target"
    MISSING_SAVINGS_TARGET_DATE = "missing_savings_target_date"
    FORECAST_LIMITATION = "forecast_limitation"


class SafeSpendingLimitingFactor(StrEnum):
    """Which explicit constraint set the returned weekly amount."""

    CASH_HEADROOM = "cash_headroom"
    WEEKLY_BUDGET = "weekly_budget"
    CASH_AND_BUDGET = "cash_and_budget"
    NO_HEADROOM = "no_headroom"


class BudgetCreate(_PlanningModel):
    """Validated request to create one local budget."""

    user_profile_id: Identifier
    budget_type: BudgetType
    category_id: Identifier | None = None
    period: DateRange
    amount_limit: Money = Field(ge=0)
    currency: Currency = Currency.GBP

    @model_validator(mode="after")
    def validate_budget_shape(self) -> BudgetCreate:
        """Tie each budget type to exactly one calendar shape and category scope."""
        if self.budget_type is BudgetType.MONTHLY_CATEGORY:
            last_day = monthrange(
                self.period.start_date.year, self.period.start_date.month
            )[1]
            expected_end = self.period.start_date.replace(day=last_day)
            if (
                self.category_id is None
                or self.period.start_date.day != 1
                or self.period.end_date != expected_end
            ):
                raise ValueError(
                    "monthly category budget requires a category and full month"
                )
        elif (
            self.category_id is not None
            or self.period.start_date.weekday() != 0
            or (self.period.end_date - self.period.start_date).days != 6
        ):
            raise ValueError(
                "weekly discretionary budget requires Monday through Sunday "
                "without a category"
            )
        return self


class Budget(_PlanningModel):
    """One persisted local budget returned to application callers."""

    budget_id: Identifier
    user_profile_id: Identifier
    budget_type: BudgetType
    category_id: Identifier | None
    period: DateRange
    amount_limit: Money = Field(ge=0)
    currency: Currency

    @model_validator(mode="after")
    def validate_stored_shape(self) -> Budget:
        """Apply the same semantic shape required at creation."""
        BudgetCreate(
            user_profile_id=self.user_profile_id,
            budget_type=self.budget_type,
            category_id=self.category_id,
            period=self.period,
            amount_limit=self.amount_limit,
            currency=self.currency,
        )
        return self


class FinancialGoalCreate(_PlanningModel):
    """Validated request for a savings target or minimum-balance floor."""

    user_profile_id: Identifier
    account_id: Identifier
    goal_type: FinancialGoalType
    name: str = Field(min_length=1, max_length=100)
    target_amount: Money = Field(gt=0)
    current_amount: Money = Field(default=Decimal("0.00"), ge=0)
    target_date: date | None = None
    as_of_date: date

    @model_validator(mode="after")
    def validate_goal_shape(self) -> FinancialGoalCreate:
        """Require a future savings deadline and a date-free balance floor."""
        if self.goal_type is FinancialGoalType.SAVINGS_TARGET:
            if self.target_date is None or self.target_date <= self.as_of_date:
                raise ValueError("savings target requires a future target date")
        elif self.target_date is not None or self.current_amount != 0:
            raise ValueError(
                "minimum-balance goal cannot contain a date or saved amount"
            )
        return self


class FinancialGoal(_PlanningModel):
    """One persisted financial goal, including conservatively retained legacy data."""

    goal_id: Identifier
    user_profile_id: Identifier
    account_id: Identifier
    goal_type: FinancialGoalType
    name: str = Field(min_length=1, max_length=100)
    target_amount: Money = Field(gt=0)
    current_amount: Money = Field(ge=0)
    target_date: date | None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_stored_shape(self) -> FinancialGoal:
        """Forbid date or savings-progress fields on a balance-floor goal."""
        if self.goal_type is FinancialGoalType.MINIMUM_BALANCE and (
            self.target_date is not None or self.current_amount != 0
        ):
            raise ValueError(
                "minimum-balance goal cannot contain a date or saved amount"
            )
        return self


class PlanningEvaluationPlan(_PlanningModel):
    """Owned accounts and date used for one deterministic planning calculation."""

    user_profile_id: Identifier
    account_ids: tuple[Identifier, ...] = Field(min_length=1)
    as_of_date: date

    @model_validator(mode="after")
    def validate_accounts(self) -> PlanningEvaluationPlan:
        """Prevent an account from being counted twice."""
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("planning account IDs must be unique")
        return self


class PlanningBalanceProjection(_PlanningModel):
    """Data-minimised balance-path summary consumed by the planning engine."""

    account_id: Identifier
    currency: Currency
    period: DateRange
    lowest_lower_balance: Money
    expected_end_balance: Money
    lower_end_balance: Money
    expected_discretionary_spending: Money = Field(ge=0)
    forecast_warnings: tuple[ForecastPathWarningCode, ...] = ()

    @model_validator(mode="after")
    def validate_projection(self) -> PlanningBalanceProjection:
        """A path minimum cannot exceed its final lower-bound balance."""
        if self.period.start_date.weekday() != 0:
            raise ValueError("planning balance projection must start on Monday")
        if self.lowest_lower_balance > self.lower_end_balance:
            raise ValueError("lowest forecast balance exceeds the final lower balance")
        return self


class BudgetProgress(_PlanningModel):
    """Observed and projected use with its exact transaction-coverage evidence."""

    budget: Budget
    observation_period: DateRange
    coverage: DataCoverageIndicator
    amount_used: Money | None
    amount_remaining: Money | None
    projected_use: Money | None
    projected_overrun: Money | None

    @model_validator(mode="after")
    def validate_progress(self) -> BudgetProgress:
        """Keep monetary availability aligned with coverage and budget arithmetic."""
        if self.coverage.requested_period != self.observation_period:
            raise ValueError("budget coverage must describe its observation period")
        if self.amount_used is None:
            if any(
                value is not None
                for value in (
                    self.amount_remaining,
                    self.projected_use,
                    self.projected_overrun,
                )
            ):
                raise ValueError("unobserved budget use cannot claim derived amounts")
            return self
        expected_remaining = max(
            Decimal("0.00"), self.budget.amount_limit - self.amount_used
        )
        if self.amount_remaining != expected_remaining:
            raise ValueError("budget remaining amount is inconsistent")
        projection_expected = self.coverage.status is AnalyticsCoverageStatus.COMPLETE
        if projection_expected != (self.projected_use is not None):
            raise ValueError("budget projection availability must follow coverage")
        if self.projected_use is None:
            if self.projected_overrun is not None:
                raise ValueError("unavailable projection cannot claim an overrun")
        else:
            expected_overrun = max(
                Decimal("0.00"), self.projected_use - self.budget.amount_limit
            )
            if self.projected_overrun != expected_overrun:
                raise ValueError("budget projected overrun is inconsistent")
        return self


class FinancialGoalProgress(_PlanningModel):
    """Required contribution or forecast balance evidence for one goal."""

    goal: FinancialGoal
    remaining_amount: Money
    contribution_months: int | None = Field(default=None, ge=0)
    required_monthly_contribution: Money | None = Field(default=None, ge=0)
    forecast_lowest_balance: Money | None = None
    projected_shortfall: Money | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_progress(self) -> FinancialGoalProgress:
        """Separate savings-contribution fields from balance-floor evidence."""
        savings = self.goal.goal_type is FinancialGoalType.SAVINGS_TARGET
        contribution_present = (
            self.contribution_months is not None
            and self.required_monthly_contribution is not None
        )
        if savings != contribution_present or savings == (
            self.forecast_lowest_balance is not None
        ):
            raise ValueError("goal progress fields do not match the goal type")
        if savings == (self.projected_shortfall is not None):
            raise ValueError("only minimum-balance progress has a forecast shortfall")
        return self


class PlanningWarning(_PlanningModel):
    """One controlled warning without free-text financial source data."""

    code: PlanningWarningCode
    amount: Money | None = Field(default=None, ge=0)
    budget_id: Identifier | None = None
    goal_id: Identifier | None = None


class SafeWeeklySpending(_PlanningModel):
    """Deterministic weekly estimate after budgets, floors, and savings reserves."""

    currency: Currency
    projection_period: DateRange
    forecast_weeks: Decimal = Field(gt=0)
    expected_forecast_weekly_spending: Money = Field(ge=0)
    lower_balance_headroom: Money
    required_weekly_savings: Money = Field(ge=0)
    cash_based_weekly_limit: Money = Field(ge=0)
    weekly_budget_limit: Money | None = Field(default=None, ge=0)
    safe_weekly_spending: Money = Field(ge=0)
    limiting_factor: SafeSpendingLimitingFactor

    @model_validator(mode="after")
    def validate_limit(self) -> SafeWeeklySpending:
        """Require the result to equal its documented cash and budget constraints."""
        expected = self.cash_based_weekly_limit
        if self.weekly_budget_limit is not None:
            expected = min(expected, self.weekly_budget_limit)
        if self.safe_weekly_spending != expected:
            raise ValueError("safe weekly spending does not match its constraints")
        return self


class FinancialPlanningResult(_PlanningModel):
    """Coverage-aware budgets, goals, shortfalls, and safe weekly estimate."""

    plan: PlanningEvaluationPlan
    currency: Currency
    balance_projections: tuple[PlanningBalanceProjection, ...] = Field(min_length=1)
    budgets: tuple[BudgetProgress, ...]
    goals: tuple[FinancialGoalProgress, ...]
    safe_spending: SafeWeeklySpending
    warnings: tuple[PlanningWarning, ...]

    @model_validator(mode="after")
    def validate_result(self) -> FinancialPlanningResult:
        """Keep projections ordered, scoped, and warning identities unique."""
        projection_ids = tuple(item.account_id for item in self.balance_projections)
        if projection_ids != self.plan.account_ids:
            raise ValueError("planning projections must match the selected accounts")
        if self.balance_projections and any(
            item.period != self.balance_projections[0].period
            or item.currency is not self.currency
            for item in self.balance_projections
        ):
            raise ValueError("planning projections must use one aligned period")
        if self.safe_spending.projection_period != self.balance_projections[0].period:
            raise ValueError("safe spending must use the aligned projection period")
        warning_keys = tuple(
            (item.code, item.budget_id, item.goal_id) for item in self.warnings
        )
        if len(set(warning_keys)) != len(warning_keys):
            raise ValueError("planning warnings must be unique")
        return self


__all__ = [
    "Budget",
    "BudgetCreate",
    "BudgetProgress",
    "BudgetType",
    "FinancialGoal",
    "FinancialGoalCreate",
    "FinancialGoalProgress",
    "FinancialGoalType",
    "FinancialPlanningResult",
    "PlanningBalanceProjection",
    "PlanningEvaluationPlan",
    "PlanningWarning",
    "PlanningWarningCode",
    "SafeSpendingLimitingFactor",
    "SafeWeeklySpending",
]
