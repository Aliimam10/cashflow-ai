"""Contracts for uncertainty-aware daily cash-flow and balance paths."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.forecast_models import ForecastModelName
from cashflow_ai.schemas.forecasting import ForecastBaselineName
from cashflow_ai.schemas.freshness import FreshnessPolicy, FreshnessWarningCode
from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.statements import BalanceSnapshotSource
from cashflow_ai.schemas.transactions import Currency, FinancialRole, Identifier


class _ForecastPathModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ForecastIntervalMethod(StrEnum):
    """Supported uncertainty construction method."""

    RESIDUAL_BOOTSTRAP = "residual_bootstrap"


class ForecastPathWarningCode(StrEnum):
    """Stable limitations attached to a generated forecast path."""

    LOW_CONFIDENCE_MODEL = "low_confidence_model"
    LIMITED_RESIDUAL_HISTORY = "limited_residual_history"
    STALE_DATA = "stale_data"


class ScenarioAdjustmentKind(StrEnum):
    """Supported one-off what-if cash adjustments."""

    INFLOW = "inflow"
    OUTFLOW = "outflow"


class ForecastPathPolicy(_ForecastPathModel):
    """Explicit simulation, interval, widening, and freshness settings."""

    interval_probability: Decimal = Field(gt=Decimal("0.50"), lt=Decimal("1"))
    simulation_count: int = Field(ge=100, le=20_000)
    minimum_residual_samples: int = Field(ge=2)
    minimum_weekly_uncertainty: Money = Field(ge=0)
    low_confidence_multiplier: Decimal = Field(ge=1, le=5)
    stale_data_multiplier: Decimal = Field(ge=1, le=5)
    random_seed: int = Field(ge=0)
    freshness: FreshnessPolicy


class ForecastPathPlan(_ForecastPathModel):
    """Account, cutoff, horizon, and policy for one balance forecast."""

    user_profile_id: Identifier
    account_id: Identifier
    forecast_start: date
    horizon_days: int = Field(ge=1, le=365)
    knowledge_cutoff_at: datetime
    policy: ForecastPathPolicy

    @model_validator(mode="after")
    def validate_origin(self) -> ForecastPathPlan:
        """Require a Monday forecast strictly after an aware evidence cutoff."""
        if self.forecast_start.weekday() != 0:
            raise ValueError("balance forecast must start on Monday")
        if (
            self.knowledge_cutoff_at.tzinfo is None
            or self.knowledge_cutoff_at.utcoffset() is None
        ):
            raise ValueError("forecast knowledge cutoff must be timezone-aware")
        origin = datetime.combine(self.forecast_start, time.min, tzinfo=UTC)
        if self.knowledge_cutoff_at.astimezone(UTC) >= origin:
            raise ValueError("forecast knowledge cutoff must precede its origin")
        return self


class ForecastScenarioAdjustment(_ForecastPathModel):
    """One explicit signed hypothetical cash event."""

    adjustment_id: Identifier
    adjustment_date: date
    kind: ScenarioAdjustmentKind
    amount: Money

    @model_validator(mode="after")
    def validate_sign(self) -> ForecastScenarioAdjustment:
        """Apply repository-wide positive-inflow and negative-outflow signs."""
        if (
            self.amount == 0
            or (self.kind is ScenarioAdjustmentKind.INFLOW and self.amount < 0)
            or (self.kind is ScenarioAdjustmentKind.OUTFLOW and self.amount > 0)
        ):
            raise ValueError("scenario adjustment amount must match its cash direction")
        return self


class ForecastScenario(_ForecastPathModel):
    """Non-persistent what-if changes applied to one forecast run."""

    scenario_id: Identifier | None = None
    discretionary_spending_multiplier: Decimal = Field(default=Decimal("1"), ge=0, le=5)
    adjustments: tuple[ForecastScenarioAdjustment, ...] = ()

    @model_validator(mode="after")
    def validate_adjustments(self) -> ForecastScenario:
        """Require stable unique adjustment identities."""
        identities = tuple(item.adjustment_id for item in self.adjustments)
        if len(set(identities)) != len(identities):
            raise ValueError("scenario adjustment IDs must be unique")
        return self


class ForecastOpeningBalance(_ForecastPathModel):
    """Verified balance evidence anchoring all simulated paths."""

    balance: Money
    currency: Currency
    as_of_date: date
    recorded_at: AwareDatetime
    source: BalanceSnapshotSource


class RecurringForecastOccurrence(_ForecastPathModel):
    """One confirmed future recurring cash event known by the forecast cutoff."""

    candidate_id: Identifier
    occurrence_date: date
    signed_amount: Money
    financial_role: FinancialRole
    known_at: datetime

    @model_validator(mode="after")
    def validate_cash_role(self) -> RecurringForecastOccurrence:
        """Restrict projected series to cash-affecting roles with valid signs."""
        positive_roles = {
            FinancialRole.INCOME,
            FinancialRole.REFUND,
            FinancialRole.REIMBURSEMENT,
        }
        negative_roles = {
            FinancialRole.EXPENSE,
            FinancialRole.CASH_WITHDRAWAL,
        }
        if self.financial_role not in positive_roles | negative_roles:
            raise ValueError("recurring forecast role must affect external cash flow")
        if (self.financial_role in positive_roles and self.signed_amount <= 0) or (
            self.financial_role in negative_roles and self.signed_amount >= 0
        ):
            raise ValueError("recurring amount sign must match its financial role")
        if self.known_at.tzinfo is None or self.known_at.utcoffset() is None:
            raise ValueError("recurring-flow evidence time must be timezone-aware")
        return self


class ForecastIntervalPerformance(_ForecastPathModel):
    """Held-out empirical coverage and average interval width."""

    nominal_coverage: Decimal = Field(gt=0, lt=1)
    empirical_coverage: Decimal = Field(ge=0, le=1)
    mean_interval_width: Money = Field(ge=0)
    sample_count: int = Field(ge=1)


class WeeklySpendingPath(_ForecastPathModel):
    """Point and interval estimates for one recursively forecast week."""

    week_start: date
    week_end: date
    expected_discretionary_spending: Money = Field(ge=0)
    lower_discretionary_spending: Money = Field(ge=0)
    upper_discretionary_spending: Money = Field(ge=0)

    @model_validator(mode="after")
    def validate_week(self) -> WeeklySpendingPath:
        """Require a Monday-to-Sunday week and ordered uncertainty bounds."""
        if (
            self.week_start.weekday() != 0
            or self.week_end != self.week_start + timedelta(days=6)
        ):
            raise ValueError("weekly spending path must cover Monday through Sunday")
        if not (
            self.lower_discretionary_spending
            <= self.expected_discretionary_spending
            <= self.upper_discretionary_spending
        ):
            raise ValueError("weekly spending interval must contain its expected value")
        return self


class DailyBalancePathPoint(_ForecastPathModel):
    """Expected daily cash movements and simulated balance interval."""

    forecast_date: date
    expected_discretionary_outflow: Money = Field(ge=0)
    recurring_net_flow: Money
    scenario_adjustment: Money
    expected_balance: Money
    lower_balance: Money
    upper_balance: Money

    @model_validator(mode="after")
    def validate_balance_interval(self) -> DailyBalancePathPoint:
        """Require the expected balance to remain inside its interval."""
        if not self.lower_balance <= self.expected_balance <= self.upper_balance:
            raise ValueError("daily balance interval must contain its expected value")
        return self


class BalanceForecastPath(_ForecastPathModel):
    """One explainable uncertainty-aware future balance path."""

    plan: ForecastPathPlan
    scenario: ForecastScenario
    opening_balance: ForecastOpeningBalance
    selected_model: ForecastModelName | ForecastBaselineName
    interval_method: ForecastIntervalMethod
    widening_multiplier: Decimal = Field(ge=1)
    warnings: tuple[ForecastPathWarningCode, ...]
    freshness_warnings: tuple[FreshnessWarningCode, ...]
    recurring_occurrences: tuple[RecurringForecastOccurrence, ...]
    weekly_spending: tuple[WeeklySpendingPath, ...] = Field(min_length=1)
    daily_balances: tuple[DailyBalancePathPoint, ...] = Field(min_length=1)
    interval_performance: ForecastIntervalPerformance | None
    expected_final_balance: Money
    lower_final_balance: Money
    upper_final_balance: Money

    @model_validator(mode="after")
    def validate_path(self) -> BalanceForecastPath:
        """Keep dates, warning state, and final summary internally aligned."""
        expected_dates = tuple(
            self.plan.forecast_start + timedelta(days=offset)
            for offset in range(self.plan.horizon_days)
        )
        actual_dates = tuple(item.forecast_date for item in self.daily_balances)
        if actual_dates != expected_dates:
            raise ValueError("daily balance path must cover every requested date")
        if any(
            later.week_start <= earlier.week_start
            for earlier, later in pairwise(self.weekly_spending)
        ):
            raise ValueError("weekly spending path must be chronological")
        if bool(self.freshness_warnings) != (
            ForecastPathWarningCode.STALE_DATA in self.warnings
        ):
            raise ValueError("stale-data warning must match freshness evidence")
        last = self.daily_balances[-1]
        if (
            self.expected_final_balance != last.expected_balance
            or self.lower_final_balance != last.lower_balance
            or self.upper_final_balance != last.upper_balance
        ):
            raise ValueError("final balance summary must match the final path date")
        return self


__all__ = [
    "BalanceForecastPath",
    "DailyBalancePathPoint",
    "ForecastIntervalMethod",
    "ForecastIntervalPerformance",
    "ForecastOpeningBalance",
    "ForecastPathPlan",
    "ForecastPathPolicy",
    "ForecastPathWarningCode",
    "ForecastScenario",
    "ForecastScenarioAdjustment",
    "RecurringForecastOccurrence",
    "ScenarioAdjustmentKind",
    "WeeklySpendingPath",
]
