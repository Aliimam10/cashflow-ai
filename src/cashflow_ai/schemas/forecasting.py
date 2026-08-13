"""Typed contracts for leakage-safe forecast datasets and baseline evaluation."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.statements import DateRange
from cashflow_ai.schemas.transactions import Identifier


class _ForecastModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ForecastDayStatus(StrEnum):
    """Whether a calendar day is known or absent from verified coverage."""

    COVERED = "covered"
    UNKNOWN = "unknown"


class ForecastDatasetPlan(_ForecastModel):
    """Explicit scope and leakage cutoff for forecast feature construction."""

    user_profile_id: Identifier
    account_ids: tuple[Identifier, ...] = Field(min_length=1)
    period: DateRange
    knowledge_cutoff_at: datetime
    payday_days: tuple[int, ...] = Field(default=(1,), min_length=1)

    @model_validator(mode="after")
    def validate_plan(self) -> ForecastDatasetPlan:
        """Require unique accounts, aware cutoff, and valid unique payday days."""
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("forecast account IDs must be unique")
        if (
            self.knowledge_cutoff_at.tzinfo is None
            or self.knowledge_cutoff_at.utcoffset() is None
        ):
            raise ValueError("knowledge_cutoff_at must be timezone-aware")
        if self.period.end_date > self.knowledge_cutoff_at.date():
            raise ValueError(
                "forecast dataset period cannot extend beyond its knowledge cutoff"
            )
        if len(set(self.payday_days)) != len(self.payday_days) or any(
            day < 1 or day > 28 for day in self.payday_days
        ):
            raise ValueError("payday days must be unique values from 1 through 28")
        return self


class DailyForecastObservation(_ForecastModel):
    """One calendar day, retaining unknown dates instead of inventing zeroes."""

    observation_date: date
    status: ForecastDayStatus
    discretionary_spending: Money | None
    transaction_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_known_value(self) -> DailyForecastObservation:
        """Require values only for covered days and use non-negative spend magnitude."""
        covered = self.status is ForecastDayStatus.COVERED
        if covered != (
            self.discretionary_spending is not None
            and self.transaction_count is not None
        ):
            raise ValueError(
                "covered days require values and unknown days must retain nulls"
            )
        if self.discretionary_spending is not None and self.discretionary_spending < 0:
            raise ValueError(
                "daily discretionary spending must be a non-negative magnitude"
            )
        return self


class WeeklyForecastTarget(_ForecastModel):
    """A fully covered Monday-to-Sunday discretionary-spending target."""

    week_start: date
    week_end: date
    discretionary_spending: Money
    known_recurring_outflow: Money

    @model_validator(mode="after")
    def validate_week(self) -> WeeklyForecastTarget:
        """Require exactly one non-negative seven-day target week."""
        if (
            self.week_end - self.week_start
        ).days != 6 or self.week_start.weekday() != 0:
            raise ValueError("weekly targets must cover Monday through Sunday")
        if self.discretionary_spending < 0 or self.known_recurring_outflow < 0:
            raise ValueError("weekly targets must use non-negative magnitudes")
        return self


class ForecastFeatureRow(_ForecastModel):
    """One target with features known strictly before its week starts."""

    week_start: date
    target: Money
    lag_1: Money
    lag_2: Money
    lag_4: Money
    rolling_mean_4: Decimal
    rolling_mean_8: Decimal
    days_since_payday: int = Field(ge=0)
    days_until_payday: int = Field(ge=0)
    month: int = Field(ge=1, le=12)
    week_of_year: int = Field(ge=1, le=53)
    known_recurring_outflow: Money


class ForecastDataset(_ForecastModel):
    """Coverage calendar, eligible targets, and leakage-safe model rows."""

    plan: ForecastDatasetPlan
    daily_calendar: tuple[DailyForecastObservation, ...]
    weekly_targets: tuple[WeeklyForecastTarget, ...]
    feature_rows: tuple[ForecastFeatureRow, ...]


class ForecastBaselineName(StrEnum):
    """Required simple forecasting references."""

    HISTORICAL_MEAN = "historical_mean"
    RECENT_ROLLING_MEAN = "recent_rolling_mean"
    SEASONAL_NAIVE = "seasonal_naive"
    RECURRING_ONLY = "recurring_only"
    ZERO_DISCRETIONARY = "zero_discretionary"


class BaselineMetrics(_ForecastModel):
    """Error summary for one simple baseline."""

    baseline: ForecastBaselineName
    mae: Decimal = Field(ge=0)
    rmse: Decimal = Field(ge=0)
    bias: Decimal
    predictions: tuple[Decimal, ...]


class ExpandingWindowFold(_ForecastModel):
    """Chronological indices for one growing-training evaluation fold."""

    training_week_starts: tuple[date, ...] = Field(min_length=1)
    test_week_starts: tuple[date, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_chronology(self) -> ExpandingWindowFold:
        """Require disjoint, ordered training and future test weeks."""
        if max(self.training_week_starts) >= min(self.test_week_starts):
            raise ValueError("expanding-window training must strictly precede testing")
        return self


class ForecastBaselineEvaluation(_ForecastModel):
    """Final chronological holdout and expanding folds for all baselines."""

    final_training_week_starts: tuple[date, ...] = Field(min_length=1)
    final_test_week_starts: tuple[date, ...] = Field(min_length=1)
    expanding_folds: tuple[ExpandingWindowFold, ...] = Field(min_length=1)
    metrics: tuple[BaselineMetrics, ...] = Field(min_length=5)


__all__ = [
    name
    for name in globals()
    if name.startswith("Forecast")
    or name
    in {
        "BaselineMetrics",
        "DailyForecastObservation",
        "ExpandingWindowFold",
        "WeeklyForecastTarget",
    }
]
