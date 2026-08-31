"""Typed contracts for isolated financial what-if comparisons."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.analytics import AnalyticsCoverageStatus
from cashflow_ai.schemas.forecast_paths import (
    BalanceForecastPath,
    ForecastIntervalMethod,
    ForecastPathWarningCode,
    ForecastScenario,
)
from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.planning import (
    FinancialGoalType,
    FinancialPlanningResult,
)
from cashflow_ai.schemas.recurrence import RecurrenceFrequency
from cashflow_ai.schemas.transactions import Currency, Identifier


class _ScenarioModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class FinancialScenarioType(StrEnum):
    """Supported user-facing financial changes."""

    ONE_OFF_PURCHASE = "one_off_purchase"
    TRAVEL_EXPENSE = "travel_expense"
    NEW_SUBSCRIPTION = "new_subscription"
    CANCELLED_SUBSCRIPTION = "cancelled_subscription"
    RENT_INCREASE = "rent_increase"
    INCOME_INCREASE = "income_increase"
    INCOME_REDUCTION = "income_reduction"
    CATEGORY_SPENDING_REDUCTION = "category_spending_reduction"
    NEW_SAVINGS_TRANSFER = "new_savings_transfer"


_ONE_OFF_TYPES = {
    FinancialScenarioType.ONE_OFF_PURCHASE,
    FinancialScenarioType.TRAVEL_EXPENSE,
}
_NEW_RECURRING_TYPES = {
    FinancialScenarioType.NEW_SUBSCRIPTION,
    FinancialScenarioType.RENT_INCREASE,
    FinancialScenarioType.INCOME_INCREASE,
    FinancialScenarioType.INCOME_REDUCTION,
    FinancialScenarioType.CATEGORY_SPENDING_REDUCTION,
    FinancialScenarioType.NEW_SAVINGS_TRANSFER,
}


class FinancialScenario(_ScenarioModel):
    """One non-persistent user-authored scenario definition."""

    scenario_id: Identifier
    user_profile_id: Identifier
    account_id: Identifier
    scenario_type: FinancialScenarioType
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    start_date: date
    end_date: date | None = None
    amount: Money | None = Field(default=None, gt=0)
    frequency: RecurrenceFrequency | None = None
    category_id: Identifier | None = None
    recurring_payment_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> FinancialScenario:
        """Require only the fields meaningful for the selected scenario type."""
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("scenario end date cannot precede its start date")
        if self.scenario_type in _ONE_OFF_TYPES:
            if (
                self.amount is None
                or self.end_date is not None
                or self.frequency is not None
                or self.recurring_payment_id is not None
            ):
                raise ValueError("one-off scenario requires only one amount and date")
        elif self.scenario_type in _NEW_RECURRING_TYPES:
            if (
                self.amount is None
                or self.frequency is None
                or self.recurring_payment_id is not None
            ):
                raise ValueError("recurring scenario requires an amount and frequency")
        elif (
            self.amount is not None
            or self.frequency is not None
            or self.recurring_payment_id is None
        ):
            raise ValueError(
                "cancelled subscription requires only a recurring-payment ID"
            )
        if (
            self.scenario_type is FinancialScenarioType.CATEGORY_SPENDING_REDUCTION
            and self.category_id is None
        ):
            raise ValueError("category-spending reduction requires a category")
        if (
            self.scenario_type
            in {
                FinancialScenarioType.INCOME_INCREASE,
                FinancialScenarioType.INCOME_REDUCTION,
                FinancialScenarioType.NEW_SAVINGS_TRANSFER,
            }
            and self.category_id is not None
        ):
            raise ValueError("income and savings scenarios cannot target a category")
        return self


class ScenarioBalanceEffect(_ScenarioModel):
    """Expected and cautious balance differences caused by an overlay."""

    currency: Currency
    baseline_end_balance: Money
    scenario_end_balance: Money
    end_balance_difference: Money
    baseline_lowest_lower_balance: Money
    scenario_lowest_lower_balance: Money
    lowest_balance_difference: Money

    @model_validator(mode="after")
    def validate_differences(self) -> ScenarioBalanceEffect:
        """Require reported differences to match their source balances."""
        if self.end_balance_difference != (
            self.scenario_end_balance - self.baseline_end_balance
        ):
            raise ValueError("scenario end-balance difference is inconsistent")
        if self.lowest_balance_difference != (
            self.scenario_lowest_lower_balance - self.baseline_lowest_lower_balance
        ):
            raise ValueError("scenario lowest-balance difference is inconsistent")
        return self


class ScenarioBudgetEffect(_ScenarioModel):
    """Coverage-bound projected-use change for one active budget."""

    budget_id: Identifier
    coverage_status: AnalyticsCoverageStatus
    budget_limit: Money = Field(ge=0)
    baseline_projected_use: Money | None
    scenario_projected_use: Money | None
    projected_use_difference: Money | None
    baseline_projected_overrun: Money | None
    scenario_projected_overrun: Money | None

    @model_validator(mode="after")
    def validate_availability(self) -> ScenarioBudgetEffect:
        """Forbid scenario projections when the baseline projection is unknown."""
        values = (
            self.scenario_projected_use,
            self.projected_use_difference,
            self.scenario_projected_overrun,
        )
        if self.baseline_projected_use is None:
            if any(item is not None for item in values):
                raise ValueError("unknown baseline budget use cannot be adjusted")
        elif (
            self.scenario_projected_use is None
            or self.projected_use_difference
            != self.scenario_projected_use - self.baseline_projected_use
        ):
            raise ValueError("scenario budget difference is inconsistent")
        else:
            expected_baseline_overrun = max(
                Decimal("0.00"), self.baseline_projected_use - self.budget_limit
            )
            expected_scenario_overrun = max(
                Decimal("0.00"), self.scenario_projected_use - self.budget_limit
            )
            if (
                self.baseline_projected_overrun != expected_baseline_overrun
                or self.scenario_projected_overrun != expected_scenario_overrun
            ):
                raise ValueError("scenario budget overrun is inconsistent")
        return self


class ScenarioGoalEffect(_ScenarioModel):
    """Change in risk or projected shortfall for one financial goal."""

    goal_id: Identifier
    goal_type: FinancialGoalType
    required_monthly_contribution: Money | None
    baseline_projected_shortfall: Money | None
    scenario_projected_shortfall: Money | None
    projected_shortfall_difference: Money | None
    baseline_at_risk: bool
    scenario_at_risk: bool

    @model_validator(mode="after")
    def validate_goal_fields(self) -> ScenarioGoalEffect:
        """Separate savings-capacity evidence from balance-floor shortfalls."""
        if self.goal_type is FinancialGoalType.MINIMUM_BALANCE:
            if (
                self.required_monthly_contribution is not None
                or self.baseline_projected_shortfall is None
                or self.scenario_projected_shortfall is None
                or self.projected_shortfall_difference
                != self.scenario_projected_shortfall - self.baseline_projected_shortfall
            ):
                raise ValueError("minimum-balance scenario effect is inconsistent")
        elif (
            self.required_monthly_contribution is None
            or self.baseline_projected_shortfall is not None
            or self.scenario_projected_shortfall is not None
            or self.projected_shortfall_difference is not None
        ):
            raise ValueError("savings scenario effect is inconsistent")
        return self


class ScenarioSafeSpendingEffect(_ScenarioModel):
    """Difference in Commit 27's conservative weekly estimate."""

    currency: Currency
    baseline_safe_weekly_spending: Money = Field(ge=0)
    scenario_safe_weekly_spending: Money = Field(ge=0)
    difference: Money

    @model_validator(mode="after")
    def validate_difference(self) -> ScenarioSafeSpendingEffect:
        """Require the comparison to match its two estimates."""
        if self.difference != (
            self.scenario_safe_weekly_spending - self.baseline_safe_weekly_spending
        ):
            raise ValueError("safe-spending scenario difference is inconsistent")
        return self


class ScenarioUncertainty(_ScenarioModel):
    """Evidence that the scenario inherited the baseline uncertainty model."""

    inherited: Literal[True] = True
    interval_method: ForecastIntervalMethod
    interval_probability: Decimal = Field(gt=Decimal("0.50"), lt=Decimal("1"))
    widening_multiplier: Decimal = Field(ge=1)
    forecast_warnings: tuple[ForecastPathWarningCode, ...]


class ScenarioComparisonWarningCode(StrEnum):
    """Stable baseline limitations attached to a scenario comparison."""

    BASELINE_FORECAST_LIMITATION = "baseline_forecast_limitation"
    INCOMPLETE_BASELINE_COVERAGE = "incomplete_baseline_coverage"


class FinancialScenarioComparison(_ScenarioModel):
    """Baseline and isolated what-if paths with planning impacts."""

    scenario: FinancialScenario
    overlay: ForecastScenario
    baseline_forecast: BalanceForecastPath
    scenario_forecast: BalanceForecastPath
    baseline_plan: FinancialPlanningResult
    scenario_plan: FinancialPlanningResult
    balance_effect: ScenarioBalanceEffect
    budget_effects: tuple[ScenarioBudgetEffect, ...]
    goal_effects: tuple[ScenarioGoalEffect, ...]
    safe_spending_effect: ScenarioSafeSpendingEffect
    uncertainty: ScenarioUncertainty
    warnings: tuple[ScenarioComparisonWarningCode, ...]
    hypothetical: bool = True

    @model_validator(mode="after")
    def validate_comparison(self) -> FinancialScenarioComparison:
        """Keep baseline, overlay, scenario path, and warning state aligned."""
        if not self.hypothetical:
            raise ValueError("scenario comparison must remain hypothetical")
        if self.baseline_forecast.scenario != ForecastScenario():
            raise ValueError("baseline forecast cannot contain a scenario")
        if (
            self.overlay.scenario_id != self.scenario.scenario_id
            or self.scenario_forecast.scenario != self.overlay
        ):
            raise ValueError("scenario overlay does not match the scenario path")
        expected_warning = bool(self.baseline_forecast.warnings)
        if expected_warning != (
            ScenarioComparisonWarningCode.BASELINE_FORECAST_LIMITATION in self.warnings
        ):
            raise ValueError("baseline forecast warning state is inconsistent")
        incomplete = any(
            item.coverage_status is not AnalyticsCoverageStatus.COMPLETE
            for item in self.budget_effects
        )
        if incomplete != (
            ScenarioComparisonWarningCode.INCOMPLETE_BASELINE_COVERAGE in self.warnings
        ):
            raise ValueError("baseline coverage warning state is inconsistent")
        if len(set(self.warnings)) != len(self.warnings):
            raise ValueError("scenario comparison warnings must be unique")
        return self


__all__ = [
    "FinancialScenario",
    "FinancialScenarioComparison",
    "FinancialScenarioType",
    "ScenarioBalanceEffect",
    "ScenarioBudgetEffect",
    "ScenarioComparisonWarningCode",
    "ScenarioGoalEffect",
    "ScenarioSafeSpendingEffect",
    "ScenarioUncertainty",
]
