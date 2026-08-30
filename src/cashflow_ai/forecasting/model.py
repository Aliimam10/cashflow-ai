"""Primary gradient-boosting forecast training and conservative selection."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sklearn import ensemble  # type: ignore[import-untyped]
from sklearn.inspection import permutation_importance  # type: ignore[import-untyped]

from cashflow_ai.forecasting.service import (
    ForecastingDataError,
    ForecastingDataErrorCode,
    evaluate_forecast_baselines,
    validate_forecast_dataset,
)
from cashflow_ai.schemas.forecast_models import (
    FEATURE_NAMES,
    ForecastFeatureImportance,
    ForecastInferenceRow,
    ForecastModelComparison,
    ForecastModelName,
    ForecastModelPolicy,
    ForecastPrediction,
    HorizonPerformance,
    RegressionMetrics,
    feature_vector,
)
from cashflow_ai.schemas.forecasting import (
    BaselineMetrics,
    ForecastBaselineEvaluation,
    ForecastBaselineName,
    ForecastDataset,
    ForecastFeatureRow,
)

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class TrainedPrimaryForecaster:
    """In-memory estimator and privacy-safe model comparison."""

    estimator: ensemble.HistGradientBoostingRegressor | None
    comparison: ForecastModelComparison
    latest_observed_week: date | None
    latest_observed_known_at: datetime | None
    target_history: tuple[tuple[date, Decimal, datetime], ...]


def _estimator(policy: ForecastModelPolicy) -> ensemble.HistGradientBoostingRegressor:
    return ensemble.HistGradientBoostingRegressor(
        loss="squared_error",
        learning_rate=policy.learning_rate,
        max_iter=policy.maximum_iterations,
        max_leaf_nodes=policy.maximum_leaf_nodes,
        min_samples_leaf=policy.minimum_samples_leaf,
        random_state=policy.random_seed,
        early_stopping=False,
    )


def _matrix(
    rows: tuple[ForecastFeatureRow, ...],
) -> tuple[list[list[float]], list[float]]:
    return (
        [list(feature_vector(row)) for row in rows],
        [float(row.target) for row in rows],
    )


def _feature_matrix(
    rows: Sequence[ForecastFeatureRow | ForecastInferenceRow],
) -> list[list[float]]:
    return [list(feature_vector(row)) for row in rows]


def _regression_metrics(
    rows: tuple[ForecastFeatureRow, ...], predictions: tuple[float, ...]
) -> RegressionMetrics:
    actual = tuple(row.target for row in rows)
    predicted = tuple(Decimal(str(max(0.0, value))) for value in predictions)
    errors = tuple(
        prediction - truth for prediction, truth in zip(predicted, actual, strict=True)
    )
    mae = sum((abs(item) for item in errors), start=_ZERO) / len(errors)
    mse = sum((item * item for item in errors), start=_ZERO) / len(errors)
    return RegressionMetrics(
        mae=mae,
        rmse=Decimal(str(math.sqrt(float(mse)))),
        bias=sum(errors, start=_ZERO) / len(errors),
        actuals=actual,
        predictions=predicted,
    )


def _predict(
    estimator: ensemble.HistGradientBoostingRegressor,
    rows: Sequence[ForecastFeatureRow | ForecastInferenceRow],
) -> tuple[float, ...]:
    return tuple(
        float(item) for item in cast(Any, estimator.predict(_feature_matrix(rows)))
    )


def _fit(
    policy: ForecastModelPolicy, rows: tuple[ForecastFeatureRow, ...]
) -> ensemble.HistGradientBoostingRegressor:
    matrix, targets = _matrix(rows)
    return _estimator(policy).fit(matrix, targets)


def _baseline_evidence(
    dataset: ForecastDataset, policy: ForecastModelPolicy
) -> tuple[ForecastBaselineEvaluation, BaselineMetrics, BaselineMetrics]:
    evaluation = evaluate_forecast_baselines(
        dataset,
        initial_training_weeks=policy.initial_training_weeks,
        final_test_weeks=policy.final_test_weeks,
    )
    return (
        evaluation,
        min(evaluation.metrics, key=lambda item: (item.mae, item.baseline.value)),
        min(
            evaluation.expanding_metrics,
            key=lambda item: (item.mae, item.baseline.value),
        ),
    )


def _expanding_predictions(
    rows_by_week: dict[date, ForecastFeatureRow],
    evaluation: ForecastBaselineEvaluation,
    policy: ForecastModelPolicy,
) -> tuple[tuple[ForecastFeatureRow, ...], tuple[float, ...]]:
    evaluated: list[ForecastFeatureRow] = []
    predictions: list[float] = []
    for fold in evaluation.expanding_folds:
        training = tuple(rows_by_week[value] for value in fold.training_week_starts)
        testing = tuple(rows_by_week[value] for value in fold.test_week_starts)
        model = _fit(policy, training)
        evaluated.extend(testing)
        predictions.extend(_predict(model, testing))
    return tuple(evaluated), tuple(predictions)


def _importance(
    estimator: ensemble.HistGradientBoostingRegressor,
    rows: tuple[ForecastFeatureRow, ...],
    seed: int,
) -> tuple[ForecastFeatureImportance, ...]:
    matrix, targets = _matrix(rows)

    def clipped_negative_mae(
        fitted: ensemble.HistGradientBoostingRegressor,
        feature_matrix: list[list[float]],
        actual: list[float],
    ) -> float:
        """Score the same non-negative predictor used by evaluation and inference."""
        predicted = (
            max(0.0, float(item)) for item in cast(Any, fitted.predict(feature_matrix))
        )
        return -sum(
            abs(prediction - truth)
            for prediction, truth in zip(predicted, actual, strict=True)
        ) / len(actual)

    result = permutation_importance(
        estimator,
        matrix,
        targets,
        scoring=clipped_negative_mae,
        n_repeats=5,
        random_state=seed,
        n_jobs=1,
    )
    items = (
        ForecastFeatureImportance(
            feature_name=name,
            mae_increase=Decimal(str(float(value))),
        )
        for name, value in zip(FEATURE_NAMES, result.importances_mean, strict=True)
    )
    return tuple(
        sorted(items, key=lambda item: (-item.mae_increase, item.feature_name))
    )


def _meets_selection_policy(
    candidate: RegressionMetrics,
    baseline: RegressionMetrics,
    policy: ForecastModelPolicy,
) -> bool:
    improvement_multiplier = Decimal(str(1 - policy.minimum_relative_mae_improvement))
    rmse_multiplier = Decimal(str(1 + policy.maximum_relative_rmse_regression))
    return (
        candidate.mae < baseline.mae * improvement_multiplier
        and candidate.rmse <= baseline.rmse * rmse_multiplier
        and abs(candidate.bias)
        <= abs(baseline.bias) + policy.maximum_absolute_bias_increase
    )


def _baseline_prediction(
    trained: TrainedPrimaryForecaster, row: ForecastInferenceRow
) -> Decimal:
    """Apply the selected simple model using only information available at inference."""
    name = trained.comparison.selected_model
    eligible_history = tuple(
        item for item in trained.target_history if item[2] < row.forecast_origin_at
    )
    historical_mean = (
        sum((item[1] for item in eligible_history), start=_ZERO) / len(eligible_history)
        if eligible_history
        else _ZERO
    )
    if name is ForecastBaselineName.RECENT_ROLLING_MEAN:
        return row.rolling_mean_4
    if name is ForecastBaselineName.HISTORICAL_MEAN:
        return historical_mean
    if name is ForecastBaselineName.SEASONAL_NAIVE:
        seasonal_week = row.week_start - timedelta(weeks=52)
        seasonal = next(
            (item[1] for item in eligible_history if item[0] == seasonal_week), None
        )
        return seasonal if seasonal is not None else historical_mean
    return _ZERO


def train_primary_forecaster(
    dataset: ForecastDataset, *, policy: ForecastModelPolicy
) -> TrainedPrimaryForecaster:
    """Train and select gradient boosting only after two chronological comparisons."""
    validate_forecast_dataset(dataset)
    rows = dataset.feature_rows
    history = tuple(
        (item.week_start, item.discretionary_spending, item.known_at)
        for item in dataset.weekly_targets
    )
    latest_week = (
        dataset.weekly_targets[-1].week_start if dataset.weekly_targets else None
    )
    split = len(rows) - policy.final_test_weeks
    has_independent_validation = split > policy.initial_training_weeks
    final_origin = rows[split].forecast_origin_at if split >= 0 and rows else None
    final_training = (
        tuple(
            row
            for row in rows[:split]
            if final_origin is not None and row.target_known_at < final_origin
        )
        if split > 0
        else ()
    )
    has_point_in_time_validation = any(
        sum(
            candidate.target_known_at < rows[index].forecast_origin_at
            for candidate in rows[:index]
        )
        >= policy.initial_training_weeks
        for index in range(policy.initial_training_weeks, max(split, 0))
    )
    if (
        not has_independent_validation
        or not has_point_in_time_validation
        or len(final_training)
        < max(policy.minimum_training_weeks, policy.initial_training_weeks)
    ):
        comparison = ForecastModelComparison(
            model_name=ForecastModelName.LOW_DATA_FALLBACK,
            policy=policy,
            knowledge_cutoff_at=dataset.plan.knowledge_cutoff_at,
            training_sample_count=len(rows),
            selected=False,
            selected_model=ForecastBaselineName.RECENT_ROLLING_MEAN,
            selection_reason=(
                "Insufficient complete consecutive weeks; use the recent-mean fallback."
            ),
            final_test=None,
            expanding_validation=None,
            best_baseline=ForecastBaselineName.RECENT_ROLLING_MEAN,
            best_expanding_baseline=None,
            best_baseline_final_mae=None,
            best_baseline_final_rmse=None,
            best_baseline_final_bias=None,
            best_baseline_expanding_mae=None,
            best_baseline_expanding_rmse=None,
            best_baseline_expanding_bias=None,
            horizon_performance=(),
            feature_importance=(),
            training_week_starts=tuple(row.week_start for row in rows),
            final_test_week_starts=(),
        )
        return TrainedPrimaryForecaster(
            estimator=None,
            comparison=comparison,
            latest_observed_week=latest_week,
            latest_observed_known_at=(
                dataset.weekly_targets[-1].known_at if dataset.weekly_targets else None
            ),
            target_history=history,
        )
    evaluation, best_final, best_expanding_baseline = _baseline_evidence(
        dataset, policy
    )
    rows_by_week = {row.week_start: row for row in rows}
    training = tuple(
        rows_by_week[value] for value in evaluation.final_training_week_starts
    )
    testing = tuple(rows_by_week[value] for value in evaluation.final_test_week_starts)
    expanding_rows, expanding_values = _expanding_predictions(
        rows_by_week, evaluation, policy
    )
    expanding_metrics = _regression_metrics(expanding_rows, expanding_values)
    model = _fit(policy, training)
    final_metrics = _regression_metrics(testing, _predict(model, testing))
    final_baseline = RegressionMetrics(
        mae=best_final.mae,
        rmse=best_final.rmse,
        bias=best_final.bias,
        actuals=tuple(row.target for row in testing),
        predictions=best_final.predictions,
    )
    expanding_baseline = RegressionMetrics(
        mae=best_expanding_baseline.mae,
        rmse=best_expanding_baseline.rmse,
        bias=best_expanding_baseline.bias,
        actuals=tuple(row.target for row in expanding_rows),
        predictions=best_expanding_baseline.predictions,
    )
    selected = _meets_selection_policy(
        final_metrics, final_baseline, policy
    ) and _meets_selection_policy(expanding_metrics, expanding_baseline, policy)
    selected_model: ForecastModelName | ForecastBaselineName = (
        ForecastModelName.HIST_GRADIENT_BOOSTING
        if selected
        else best_expanding_baseline.baseline
    )
    reason = (
        "Gradient boosting meets the explicit MAE improvement and RMSE/bias "
        "non-regression thresholds on expanding validation and final test."
        if selected
        else "Gradient boosting does not consistently satisfy the MAE, RMSE, and "
        "bias safeguards against the best simple baselines."
    )
    horizons = (
        HorizonPerformance(
            horizon_weeks=1,
            sample_count=len(testing),
            metrics=final_metrics,
        ),
    )
    eligible_for_refit = tuple(
        row for row in rows if row.target_known_at <= dataset.plan.knowledge_cutoff_at
    )
    comparison = ForecastModelComparison(
        model_name=ForecastModelName.HIST_GRADIENT_BOOSTING,
        policy=policy,
        knowledge_cutoff_at=dataset.plan.knowledge_cutoff_at,
        training_sample_count=len(eligible_for_refit),
        selected=selected,
        selected_model=selected_model,
        selection_reason=reason,
        final_test=final_metrics,
        expanding_validation=expanding_metrics,
        best_baseline=best_final.baseline,
        best_expanding_baseline=best_expanding_baseline.baseline,
        best_baseline_final_mae=best_final.mae,
        best_baseline_final_rmse=best_final.rmse,
        best_baseline_final_bias=best_final.bias,
        best_baseline_expanding_mae=best_expanding_baseline.mae,
        best_baseline_expanding_rmse=best_expanding_baseline.rmse,
        best_baseline_expanding_bias=best_expanding_baseline.bias,
        horizon_performance=horizons,
        feature_importance=_importance(model, testing, policy.random_seed),
        training_week_starts=tuple(row.week_start for row in training),
        final_test_week_starts=tuple(row.week_start for row in testing),
    )
    final_model = _fit(policy, eligible_for_refit) if selected else None
    return TrainedPrimaryForecaster(
        estimator=final_model,
        comparison=comparison,
        latest_observed_week=latest_week,
        latest_observed_known_at=(
            dataset.weekly_targets[-1].known_at if dataset.weekly_targets else None
        ),
        target_history=history,
    )


def predict_discretionary_spending(
    trained: TrainedPrimaryForecaster, row: ForecastInferenceRow
) -> ForecastPrediction:
    """Produce one target-free forecast for the next unobserved week."""
    if (
        trained.latest_observed_week is None
        or row.week_start != trained.latest_observed_week + timedelta(weeks=1)
        or trained.latest_observed_known_at is None
        or trained.latest_observed_known_at >= row.forecast_origin_at
        or any(
            known_at >= row.forecast_origin_at
            for _week, _value, known_at in trained.target_history
        )
    ):
        raise ForecastingDataError(
            ForecastingDataErrorCode.INVALID_EVALUATION_POLICY,
            "prediction requires the next week and history known before its origin",
        )
    if trained.estimator is not None:
        value = Decimal(str(max(0.0, _predict(trained.estimator, (row,))[0])))
    else:
        value = max(_ZERO, _baseline_prediction(trained, row))
    return ForecastPrediction(
        week_start=row.week_start,
        forecast_origin_at=row.forecast_origin_at,
        discretionary_spending=value.quantize(Decimal("0.01")),
        selected_model=trained.comparison.selected_model,
        advanced_model_selected=trained.comparison.selected,
        training_knowledge_cutoff_at=trained.comparison.knowledge_cutoff_at,
    )
