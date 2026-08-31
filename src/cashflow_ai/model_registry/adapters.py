"""Data-minimising adapters from evaluated model outputs into the registry."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from cashflow_ai.forecasting.model import TrainedPrimaryForecaster
from cashflow_ai.schemas.anomalies import AnomalyDetectionResult
from cashflow_ai.schemas.forecast_models import FEATURE_NAMES, RegressionMetrics
from cashflow_ai.schemas.ml_categorisation import (
    ClassificationMetrics,
    MLCategoriserTrainingResult,
)
from cashflow_ai.schemas.model_registry import (
    ModelFeatureSchema,
    ModelMetric,
    ModelMetricUnit,
    ModelParameter,
    ModelRegistration,
    ModelTask,
)

_METRIC_QUANTUM = Decimal("0.00000001")


def _value(value: int | float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_METRIC_QUANTUM)


def _parameter(name: str, value: Any) -> ModelParameter:
    return ModelParameter(
        name=name,
        value=json.dumps(value, sort_keys=True, separators=(",", ":")),
    )


def _classification_metrics(
    *,
    evaluation_slice: str,
    metrics: ClassificationMetrics,
) -> tuple[ModelMetric, ...]:
    return tuple(
        ModelMetric(
            name=name,
            evaluation_slice=evaluation_slice,
            value=_value(value),
            unit=ModelMetricUnit.SCORE,
        )
        for name, value in (
            ("macro_f1", metrics.macro_f1),
            ("weighted_f1", metrics.weighted_f1),
            ("macro_precision", metrics.macro_precision),
            ("weighted_precision", metrics.weighted_precision),
            ("macro_recall", metrics.macro_recall),
            ("weighted_recall", metrics.weighted_recall),
        )
    )


def registration_from_categorisation(
    result: MLCategoriserTrainingResult,
) -> ModelRegistration:
    """Keep aggregate classifier evidence while excluding private vocabulary."""
    metadata = result.metadata
    evaluation = metadata.evaluation
    metrics = (
        ModelMetric(
            name="training_samples",
            evaluation_slice="final_training",
            value=_value(metadata.training_count),
            unit=ModelMetricUnit.COUNT,
        ),
        *_classification_metrics(
            evaluation_slice="chronological_candidate",
            metrics=evaluation.chronological.candidate,
        ),
        *_classification_metrics(
            evaluation_slice="chronological_baseline",
            metrics=evaluation.chronological.baseline,
        ),
        *_classification_metrics(
            evaluation_slice="unseen_merchant_candidate",
            metrics=evaluation.unseen_merchant.candidate,
        ),
        *_classification_metrics(
            evaluation_slice="unseen_merchant_baseline",
            metrics=evaluation.unseen_merchant.baseline,
        ),
    )
    parameters = (
        *(
            _parameter(name, value)
            for name, value in sorted(
                metadata.parameters.model_dump(mode="json").items()
            )
        ),
        _parameter(
            "unseen_merchant_test_fraction",
            metadata.unseen_merchant_test_fraction,
        ),
        _parameter("minimum_training_samples", metadata.minimum_training_samples),
        _parameter("minimum_test_samples", metadata.minimum_test_samples),
        _parameter("python_version", metadata.python_version),
        _parameter("scikit_learn_version", metadata.scikit_learn_version),
        _parameter("artifact_sha256", metadata.artifact_sha256),
    )
    return ModelRegistration(
        model_name=metadata.manifest.model_name,
        model_type="tfidf_logistic_regression",
        model_version=metadata.manifest.model_version,
        task=ModelTask.TRANSACTION_CATEGORISATION,
        training_start_date=metadata.training_start_date,
        training_end_date=metadata.training_end_date,
        feature_schema=ModelFeatureSchema(
            version=metadata.manifest.feature_schema_version,
            feature_names=("word_tfidf", "character_tfidf"),
        ),
        taxonomy_version=metadata.manifest.taxonomy_version,
        metrics=metrics,
        parameters=parameters,
        artifact_path=result.artifact_path.as_posix(),
        created_at=metadata.manifest.created_at,
        activation_eligible=evaluation.candidate_selected,
    )


def _regression_metric_set(
    *,
    evaluation_slice: str,
    metrics: RegressionMetrics,
) -> tuple[ModelMetric, ...]:
    return tuple(
        ModelMetric(
            name=name,
            evaluation_slice=evaluation_slice,
            value=_value(value),
            unit=ModelMetricUnit.GBP,
        )
        for name, value in (
            ("mae", metrics.mae),
            ("rmse", metrics.rmse),
            ("bias", metrics.bias),
        )
    )


def registration_from_forecast(
    trained: TrainedPrimaryForecaster,
    *,
    model_version: str,
    created_at: datetime,
) -> ModelRegistration:
    """Summarise forecast comparison without storing held-out weekly values."""
    comparison = trained.comparison
    if not comparison.training_week_starts:
        raise ValueError("forecast registry metadata requires training dates")
    metrics: tuple[ModelMetric, ...] = (
        ModelMetric(
            name="training_samples",
            evaluation_slice="final_training",
            value=_value(comparison.training_sample_count),
            unit=ModelMetricUnit.COUNT,
        ),
    )
    if comparison.final_test is not None:
        metrics += _regression_metric_set(
            evaluation_slice="final_test_candidate",
            metrics=comparison.final_test,
        )
    if comparison.expanding_validation is not None:
        metrics += _regression_metric_set(
            evaluation_slice="expanding_candidate",
            metrics=comparison.expanding_validation,
        )
    baseline_values = (
        ("final_test_baseline", "mae", comparison.best_baseline_final_mae),
        ("final_test_baseline", "rmse", comparison.best_baseline_final_rmse),
        ("final_test_baseline", "bias", comparison.best_baseline_final_bias),
        ("expanding_baseline", "mae", comparison.best_baseline_expanding_mae),
        ("expanding_baseline", "rmse", comparison.best_baseline_expanding_rmse),
        ("expanding_baseline", "bias", comparison.best_baseline_expanding_bias),
    )
    for evaluation_slice, name, value in baseline_values:
        if value is not None:
            metrics += (
                ModelMetric(
                    name=name,
                    evaluation_slice=evaluation_slice,
                    value=_value(value),
                    unit=ModelMetricUnit.GBP,
                ),
            )
    parameters = (
        *(
            _parameter(name, value)
            for name, value in sorted(comparison.policy.model_dump(mode="json").items())
        ),
        _parameter("selected_model", comparison.selected_model.value),
        _parameter("best_baseline", comparison.best_baseline.value),
    )
    return ModelRegistration(
        model_name="weekly_discretionary_cash_flow",
        model_type=comparison.model_name.value,
        model_version=model_version,
        task=ModelTask.CASH_FLOW_FORECASTING,
        training_start_date=min(comparison.training_week_starts),
        training_end_date=max(comparison.training_week_starts),
        feature_schema=ModelFeatureSchema(
            version="weekly_cash_flow_v1",
            feature_names=FEATURE_NAMES,
        ),
        metrics=metrics,
        parameters=parameters,
        artifact_path=None,
        created_at=created_at,
        activation_eligible=comparison.selected,
    )


def registration_from_anomaly_detection(
    result: AnomalyDetectionResult,
    *,
    created_at: datetime,
    taxonomy_version: str | None = None,
) -> ModelRegistration:
    """Summarise one Isolation Forest run without persisting transaction alerts."""
    metadata = result.model_metadata
    if metadata is None:
        raise ValueError("anomaly registry metadata requires a completed model run")
    metrics = (
        ModelMetric(
            name="verified_transactions",
            evaluation_slice="run_summary",
            value=_value(result.verified_transaction_count),
            unit=ModelMetricUnit.COUNT,
        ),
        ModelMetric(
            name="reference_transactions",
            evaluation_slice="run_summary",
            value=_value(result.reference_transaction_count),
            unit=ModelMetricUnit.COUNT,
        ),
        ModelMetric(
            name="scored_transactions",
            evaluation_slice="run_summary",
            value=_value(result.scored_transaction_count),
            unit=ModelMetricUnit.COUNT,
        ),
        ModelMetric(
            name="alert_count",
            evaluation_slice="run_summary",
            value=_value(len(result.alerts)),
            unit=ModelMetricUnit.COUNT,
        ),
        ModelMetric(
            name="minimum_coverage_ratio",
            evaluation_slice="run_summary",
            value=_value(result.minimum_reference_coverage_ratio),
            unit=ModelMetricUnit.RATIO,
        ),
    )
    parameters = (
        _parameter("estimators", metadata.estimators),
        _parameter("contamination", metadata.contamination),
        _parameter("random_seed", metadata.random_seed),
        _parameter("training_transaction_count", metadata.training_transaction_count),
    )
    return ModelRegistration(
        model_name="transaction_anomaly_isolation_forest",
        model_type="isolation_forest",
        model_version=metadata.model_version,
        task=ModelTask.TRANSACTION_ANOMALY_DETECTION,
        training_start_date=metadata.training_start_date,
        training_end_date=metadata.training_end_date,
        feature_schema=ModelFeatureSchema(
            version=metadata.feature_schema_version,
            feature_names=metadata.feature_names,
        ),
        taxonomy_version=taxonomy_version,
        metrics=metrics,
        parameters=parameters,
        artifact_path=None,
        created_at=created_at,
        activation_eligible=False,
    )


__all__ = [
    "registration_from_anomaly_detection",
    "registration_from_categorisation",
    "registration_from_forecast",
]
