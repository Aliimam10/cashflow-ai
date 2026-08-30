"""Contracts for the primary discretionary-spending forecasting model."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.forecasting import ForecastBaselineName, ForecastFeatureRow
from cashflow_ai.schemas.money import Money


class _ModelContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ForecastModelName(StrEnum):
    """Candidate and fallback identities visible to downstream callers."""

    HIST_GRADIENT_BOOSTING = "hist_gradient_boosting"
    LOW_DATA_FALLBACK = "low_data_fallback"


class ForecastModelPolicy(_ModelContract):
    """Explicit training, evaluation, and model-selection thresholds."""

    initial_training_weeks: int = Field(ge=2)
    final_test_weeks: int = Field(ge=1)
    minimum_training_weeks: int = Field(ge=2)
    minimum_relative_mae_improvement: float = Field(ge=0, le=1)
    maximum_relative_rmse_regression: float = Field(default=0, ge=0, le=1)
    maximum_absolute_bias_increase: Money = Field(default=Decimal("0"), ge=0)
    maximum_iterations: int = Field(default=200, ge=10, le=2_000)
    learning_rate: float = Field(default=0.05, gt=0, le=1)
    maximum_leaf_nodes: int = Field(default=15, ge=2, le=255)
    minimum_samples_leaf: int = Field(default=5, ge=1)
    random_seed: int = Field(default=42, ge=0)

    @model_validator(mode="after")
    def validate_training_windows(self) -> ForecastModelPolicy:
        """Ensure expanding evaluation never starts before model eligibility."""
        if self.initial_training_weeks < self.minimum_training_weeks:
            raise ValueError(
                "initial training weeks must meet the minimum training requirement"
            )
        return self


class RegressionMetrics(_ModelContract):
    """Error metrics and predictions for one chronological evaluation slice."""

    mae: Decimal = Field(ge=0)
    rmse: Decimal = Field(ge=0)
    bias: Decimal
    actuals: tuple[Decimal, ...] = Field(min_length=1)
    predictions: tuple[Decimal, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rows(self) -> RegressionMetrics:
        """Require one prediction per held-out actual value."""
        if len(self.actuals) != len(self.predictions):
            raise ValueError("regression actuals and predictions must align")
        return self


class HorizonPerformance(_ModelContract):
    """Chronological performance at one evaluation horizon."""

    horizon_weeks: int = Field(ge=1)
    sample_count: int = Field(ge=1)
    metrics: RegressionMetrics

    @model_validator(mode="after")
    def validate_sample_count(self) -> HorizonPerformance:
        """Keep the declared sample count aligned with its evidence."""
        if self.sample_count != len(self.metrics.actuals):
            raise ValueError("horizon sample count must match its metric rows")
        return self


class ForecastFeatureImportance(_ModelContract):
    """Held-out permutation importance for one controlled feature name."""

    feature_name: str = Field(min_length=1, max_length=100)
    mae_increase: Decimal


class ForecastInferenceRow(_ModelContract):
    """Target-free features for exactly one future weekly prediction."""

    week_start: date
    forecast_origin_at: datetime
    lag_1: Money = Field(ge=0)
    lag_2: Money = Field(ge=0)
    lag_4: Money = Field(ge=0)
    rolling_mean_4: Decimal = Field(ge=0)
    rolling_mean_8: Decimal = Field(ge=0)
    days_since_payday: int = Field(ge=0)
    days_until_payday: int = Field(ge=0)
    month: int = Field(ge=1, le=12)
    week_of_year: int = Field(ge=1, le=53)
    known_recurring_outflow: Money = Field(ge=0)
    recurring_outflow_known_at: datetime

    @model_validator(mode="after")
    def validate_calendar(self) -> ForecastInferenceRow:
        """Require calendar features to describe the target Monday."""
        if self.week_start.weekday() != 0:
            raise ValueError("forecast inference week must start on Monday")
        if (
            self.month != self.week_start.month
            or self.week_of_year != self.week_start.isocalendar().week
        ):
            raise ValueError("forecast calendar features must match the target week")
        if self.forecast_origin_at != datetime.combine(
            self.week_start, time.min, tzinfo=UTC
        ):
            raise ValueError("forecast origin must be Monday 00:00 UTC")
        if (
            self.recurring_outflow_known_at.tzinfo is None
            or self.recurring_outflow_known_at.utcoffset() is None
            or self.recurring_outflow_known_at >= self.forecast_origin_at
        ):
            raise ValueError(
                "recurring outflow must be known before the forecast origin"
            )
        return self


class ForecastModelComparison(_ModelContract):
    """Candidate-versus-baseline result and transparent selection decision."""

    model_name: ForecastModelName
    policy: ForecastModelPolicy
    knowledge_cutoff_at: datetime
    training_sample_count: int = Field(ge=0)
    selected: bool
    selected_model: ForecastModelName | ForecastBaselineName
    selection_reason: str = Field(min_length=1, max_length=500)
    final_test: RegressionMetrics | None
    expanding_validation: RegressionMetrics | None
    best_baseline: ForecastBaselineName
    best_expanding_baseline: ForecastBaselineName | None
    best_baseline_final_mae: Decimal | None = Field(default=None, ge=0)
    best_baseline_final_rmse: Decimal | None = Field(default=None, ge=0)
    best_baseline_final_bias: Decimal | None = None
    best_baseline_expanding_mae: Decimal | None = Field(default=None, ge=0)
    best_baseline_expanding_rmse: Decimal | None = Field(default=None, ge=0)
    best_baseline_expanding_bias: Decimal | None = None
    horizon_performance: tuple[HorizonPerformance, ...]
    feature_importance: tuple[ForecastFeatureImportance, ...]
    training_week_starts: tuple[date, ...]
    final_test_week_starts: tuple[date, ...]

    @model_validator(mode="after")
    def validate_model_state(self) -> ForecastModelComparison:
        """Keep low-data fallback distinct from an evaluated model result."""
        model_metrics = (self.final_test, self.expanding_validation)
        baseline_metrics = (
            self.best_baseline_final_mae,
            self.best_baseline_final_rmse,
            self.best_baseline_final_bias,
            self.best_baseline_expanding_mae,
            self.best_baseline_expanding_rmse,
            self.best_baseline_expanding_bias,
        )
        evaluated = all(item is not None for item in model_metrics)
        if any(item is not None for item in model_metrics) != evaluated:
            raise ValueError("model evaluation metrics must be present together")
        baselines_evaluated = all(item is not None for item in baseline_metrics)
        if any(item is not None for item in baseline_metrics) != baselines_evaluated:
            raise ValueError("baseline evaluation metrics must be present together")
        if self.selected != (
            self.selected_model is ForecastModelName.HIST_GRADIENT_BOOSTING
        ):
            raise ValueError("selected flag must match the selected model identity")
        if not self.selected and not isinstance(
            self.selected_model, ForecastBaselineName
        ):
            raise ValueError("a rejected candidate must select an executable baseline")
        if self.model_name is ForecastModelName.LOW_DATA_FALLBACK:
            if (
                evaluated
                or baselines_evaluated
                or self.best_expanding_baseline is not None
                or self.selected_model is not ForecastBaselineName.RECENT_ROLLING_MEAN
                or self.horizon_performance
                or self.feature_importance
                or self.final_test_week_starts
            ):
                raise ValueError("low-data fallback cannot claim evaluation evidence")
        elif (
            not evaluated
            or not baselines_evaluated
            or self.best_expanding_baseline is None
            or not self.horizon_performance
            or {item.feature_name for item in self.feature_importance}
            != set(FEATURE_NAMES)
        ):
            raise ValueError("evaluated gradient boosting requires complete evidence")
        if len({item.feature_name for item in self.feature_importance}) != len(
            self.feature_importance
        ):
            raise ValueError("feature importance names must be unique")
        for values in (self.training_week_starts, self.final_test_week_starts):
            if any(later <= earlier for earlier, later in pairwise(values)):
                raise ValueError("model comparison dates must be strictly increasing")
        if self.final_test_week_starts and (
            not self.training_week_starts
            or self.training_week_starts[-1] >= self.final_test_week_starts[0]
        ):
            raise ValueError("model training dates must precede final test dates")
        if self.final_test is not None and len(self.final_test.actuals) != len(
            self.final_test_week_starts
        ):
            raise ValueError("final model metrics must align with final test dates")
        return self


class ForecastPrediction(_ModelContract):
    """One non-negative weekly prediction with controlled provenance metadata."""

    week_start: date
    forecast_origin_at: datetime
    discretionary_spending: Money = Field(ge=0)
    selected_model: ForecastModelName | ForecastBaselineName
    advanced_model_selected: bool
    training_knowledge_cutoff_at: datetime

    @model_validator(mode="after")
    def validate_provenance(self) -> ForecastPrediction:
        """Keep prediction timing and selected-model metadata internally coherent."""
        if self.forecast_origin_at != datetime.combine(
            self.week_start, time.min, tzinfo=UTC
        ):
            raise ValueError("prediction origin must be its Monday at 00:00 UTC")
        if (
            self.training_knowledge_cutoff_at.tzinfo is None
            or self.training_knowledge_cutoff_at.utcoffset() is None
            or self.training_knowledge_cutoff_at >= self.forecast_origin_at
        ):
            raise ValueError("prediction training cutoff must precede its origin")
        if self.advanced_model_selected != (
            self.selected_model is ForecastModelName.HIST_GRADIENT_BOOSTING
        ):
            raise ValueError("prediction selection metadata is inconsistent")
        return self


class ForecastTrainingResult(_ModelContract):
    """Public safe result; the fitted estimator stays in a private runtime wrapper."""

    comparison: ForecastModelComparison


def feature_vector(
    row: ForecastFeatureRow | ForecastInferenceRow,
) -> tuple[float, ...]:
    """Convert fixed-precision features into an explicit temporary ML copy."""
    return (
        float(row.lag_1),
        float(row.lag_2),
        float(row.lag_4),
        float(row.rolling_mean_4),
        float(row.rolling_mean_8),
        float(row.days_since_payday),
        float(row.days_until_payday),
        float(row.month),
        float(row.week_of_year),
        float(row.known_recurring_outflow),
    )


FEATURE_NAMES = (
    "lag_1",
    "lag_2",
    "lag_4",
    "rolling_mean_4",
    "rolling_mean_8",
    "days_since_payday",
    "days_until_payday",
    "month",
    "week_of_year",
    "known_recurring_outflow",
)
