from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from cashflow_ai.forecasting.model import (
    TrainedPrimaryForecaster,
    train_primary_forecaster,
)
from cashflow_ai.forecasting.service import build_forecast_feature_rows
from cashflow_ai.model_registry import (
    registration_from_anomaly_detection,
    registration_from_categorisation,
    registration_from_forecast,
)
from cashflow_ai.schemas.anomalies import (
    AnomalyDetectionMode,
    AnomalyDetectionPlan,
    AnomalyDetectionResult,
    AnomalyWarningCode,
    IsolationForestRunMetadata,
)
from cashflow_ai.schemas.forecast_models import ForecastModelPolicy
from cashflow_ai.schemas.forecasting import (
    ForecastDataset,
    ForecastDatasetPlan,
    RecurringOutflowProjection,
    WeeklyForecastTarget,
)
from cashflow_ai.schemas.ml_categorisation import (
    CategoryMetric,
    CategorySupport,
    ClassificationMetrics,
    ConfusionMatrix,
    HoldoutEvaluation,
    MLCategorisationEvaluation,
    MLCategoriserTrainingResult,
    MLHoldoutKind,
    MLModelManifest,
    MLPipelineParameters,
    MLTrainingMetadata,
    TrainingCutoff,
)
from cashflow_ai.schemas.model_registry import ModelTask
from cashflow_ai.schemas.statements import DateRange


def _classification_metrics(score: float) -> ClassificationMetrics:
    return ClassificationMetrics(
        macro_f1=score,
        weighted_f1=score,
        macro_precision=score,
        weighted_precision=score,
        macro_recall=score,
        weighted_recall=score,
        per_category=(
            CategoryMetric(
                category_id="food",
                precision=score,
                recall=score,
                f1=score,
                support=2,
            ),
            CategoryMetric(
                category_id="housing",
                precision=score,
                recall=score,
                f1=score,
                support=2,
            ),
        ),
        confusion_matrix=ConfusionMatrix(
            labels=("food", "housing"),
            rows=((2, 0), (0, 2)),
        ),
    )


def _categorisation_result() -> MLCategoriserTrainingResult:
    candidate = _classification_metrics(0.8)
    baseline = _classification_metrics(0.5)
    chronological = HoldoutEvaluation(
        kind=MLHoldoutKind.CHRONOLOGICAL,
        training_count=4,
        test_count=4,
        training_start_date=date(2025, 1, 1),
        training_end_date=date(2025, 4, 30),
        test_start_date=date(2025, 5, 1),
        test_end_date=date(2025, 6, 30),
        training_merchant_groups=4,
        test_merchant_groups=4,
        candidate=candidate,
        baseline=baseline,
    )
    unseen = HoldoutEvaluation(
        kind=MLHoldoutKind.UNSEEN_MERCHANT,
        training_count=4,
        test_count=4,
        training_start_date=date(2025, 1, 1),
        training_end_date=date(2025, 6, 1),
        test_start_date=date(2025, 2, 1),
        test_end_date=date(2025, 6, 30),
        training_merchant_groups=4,
        test_merchant_groups=2,
        candidate=candidate,
        baseline=baseline,
    )
    manifest = MLModelManifest(
        model_version="synthetic-1",
        taxonomy_version="1.0",
        classes=("food", "housing"),
        created_at=datetime(2025, 7, 1, tzinfo=UTC),
    )
    metadata = MLTrainingMetadata(
        manifest=manifest,
        final_cutoff=TrainingCutoff(
            transaction_date=date(2025, 6, 30),
            knowledge_cutoff_at=datetime(2025, 6, 30, 23, tzinfo=UTC),
        ),
        chronological_training_cutoff=TrainingCutoff(
            transaction_date=date(2025, 4, 30),
            knowledge_cutoff_at=datetime(2025, 4, 30, 23, tzinfo=UTC),
        ),
        chronological_test_start=date(2025, 5, 1),
        unseen_merchant_test_fraction=0.25,
        minimum_training_samples=4,
        minimum_test_samples=2,
        parameters=MLPipelineParameters(random_seed=7),
        training_count=4,
        training_start_date=date(2025, 1, 1),
        training_end_date=date(2025, 6, 30),
        category_support=(
            CategorySupport(category_id="food", count=2),
            CategorySupport(category_id="housing", count=2),
        ),
        evaluation=MLCategorisationEvaluation(
            chronological=chronological,
            unseen_merchant=unseen,
            candidate_selected=True,
            selection_reason=(
                "candidate beats the most-frequent baseline on both required holdouts"
            ),
        ),
        python_version="3.12",
        scikit_learn_version="1.7",
        artifact_sha256="a" * 64,
    )
    return MLCategoriserTrainingResult(
        artifact_path=Path("models/categorisation/synthetic-1.joblib"),
        metadata_path=Path("models/categorisation/synthetic-1.metadata.json"),
        metadata=metadata,
    )


def _forecast_dataset(*, weeks: int, flat: bool = False) -> ForecastDataset:
    first = date(2024, 1, 1)
    values = tuple(
        Decimal("40") if flat else Decimal(30 if index % 2 else 180)
        for index in range(weeks)
    )
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=value,
            known_recurring_outflow=Decimal("10") if index % 4 == 0 else Decimal("0"),
            known_at=datetime.combine(
                first + timedelta(weeks=index, days=6), time.max, tzinfo=UTC
            ),
        )
        for index, value in enumerate(values)
    )
    period_end = first + timedelta(weeks=weeks, days=-1)
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic-profile",
        account_ids=("synthetic-account",),
        period=DateRange(start_date=first, end_date=period_end),
        knowledge_cutoff_at=datetime.combine(period_end, time.max, tzinfo=UTC),
        payday_days=(1, 15),
    )
    return ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets,
        feature_rows=build_forecast_feature_rows(targets, (1, 15)),
        next_recurring_outflow=RecurringOutflowProjection(
            week_start=first + timedelta(weeks=weeks),
            amount=Decimal("0"),
            known_at=plan.knowledge_cutoff_at,
        ),
    )


def _forecast_policy(*, final_test_weeks: int) -> ForecastModelPolicy:
    return ForecastModelPolicy(
        initial_training_weeks=8,
        final_test_weeks=final_test_weeks,
        minimum_training_weeks=8,
        minimum_relative_mae_improvement=0.05,
        maximum_relative_rmse_regression=0,
        maximum_absolute_bias_increase=Decimal("1"),
        maximum_iterations=30,
        learning_rate=0.1,
        maximum_leaf_nodes=10,
        minimum_samples_leaf=2,
        random_seed=7,
    )


def _anomaly_result(*, with_model: bool = True) -> AnomalyDetectionResult:
    plan = AnomalyDetectionPlan(
        user_profile_id="synthetic-profile",
        account_ids=("synthetic-account",),
        as_of_date=date(2025, 6, 30),
        knowledge_cutoff_at=datetime(2025, 7, 1, tzinfo=UTC),
    )
    if not with_model:
        return AnomalyDetectionResult(
            plan=plan,
            mode=AnomalyDetectionMode.RULES_ONLY,
            alerts=(),
            verified_transaction_count=1,
            reference_transaction_count=0,
            scored_transaction_count=0,
            minimum_reference_covered_days=0,
            minimum_reference_coverage_ratio=0,
            exclusions=(),
            warnings=(AnomalyWarningCode.INSUFFICIENT_HISTORY,),
        )
    return AnomalyDetectionResult(
        plan=plan,
        mode=AnomalyDetectionMode.RULES_AND_MODEL,
        alerts=(),
        verified_transaction_count=30,
        reference_transaction_count=25,
        scored_transaction_count=5,
        minimum_reference_covered_days=120,
        minimum_reference_coverage_ratio=0.9,
        exclusions=(),
        warnings=(),
        model_metadata=IsolationForestRunMetadata(
            model_type="IsolationForest",
            model_version="synthetic-1",
            feature_schema_version="transaction_anomaly_v1",
            feature_names=tuple(f"feature_{index}" for index in range(8)),
            training_start_date=date(2025, 1, 1),
            training_end_date=date(2025, 5, 31),
            training_transaction_count=25,
            scored_transaction_count=5,
            category_levels=("food", "housing"),
            estimators=100,
            contamination=0.05,
            random_seed=7,
        ),
    )


def test_categorisation_adapter_keeps_aggregate_evidence_only() -> None:
    registration = registration_from_categorisation(_categorisation_result())

    assert registration.task is ModelTask.TRANSACTION_CATEGORISATION
    assert registration.model_type == "tfidf_logistic_regression"
    assert registration.taxonomy_version == "1.0"
    assert registration.artifact_path == "models/categorisation/synthetic-1.joblib"
    assert registration.activation_eligible
    assert len(registration.metrics) == 25
    assert {item.evaluation_slice for item in registration.metrics} == {
        "final_training",
        "chronological_candidate",
        "chronological_baseline",
        "unseen_merchant_candidate",
        "unseen_merchant_baseline",
    }
    serialised = registration.model_dump_json()
    assert "Fictional Grocer" not in serialised
    assert "Private bank description" not in serialised


def test_forecast_adapter_records_candidate_baseline_and_fallback_evidence() -> None:
    trained = train_primary_forecaster(
        _forecast_dataset(weeks=36),
        policy=_forecast_policy(final_test_weeks=4),
    )
    selected = registration_from_forecast(
        trained,
        model_version="synthetic-1",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )

    assert selected.task is ModelTask.CASH_FLOW_FORECASTING
    assert selected.activation_eligible == trained.comparison.selected
    assert selected.artifact_path is None
    assert selected.feature_schema.feature_names[0] == "lag_1"
    assert {item.evaluation_slice for item in selected.metrics} >= {
        "final_training",
        "final_test_candidate",
        "expanding_candidate",
        "final_test_baseline",
        "expanding_baseline",
    }

    fallback_model = train_primary_forecaster(
        _forecast_dataset(weeks=16, flat=True),
        policy=_forecast_policy(final_test_weeks=4),
    )
    fallback = registration_from_forecast(
        fallback_model,
        model_version="synthetic-fallback-1",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert not fallback.activation_eligible
    assert {item.evaluation_slice for item in fallback.metrics} == {"final_training"}

    no_dates = TrainedPrimaryForecaster(
        estimator=fallback_model.estimator,
        comparison=fallback_model.comparison.model_copy(
            update={"training_week_starts": ()}
        ),
        latest_observed_week=fallback_model.latest_observed_week,
        latest_observed_known_at=fallback_model.latest_observed_known_at,
        target_history=fallback_model.target_history,
    )
    with pytest.raises(ValueError, match="requires training dates"):
        registration_from_forecast(
            no_dates,
            model_version="invalid-no-dates",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )


def test_anomaly_adapter_records_run_counts_but_does_not_make_it_active() -> None:
    registration = registration_from_anomaly_detection(
        _anomaly_result(),
        created_at=datetime(2025, 7, 2, tzinfo=UTC),
        taxonomy_version="1.0",
    )

    assert registration.task is ModelTask.TRANSACTION_ANOMALY_DETECTION
    assert registration.model_type == "isolation_forest"
    assert not registration.activation_eligible
    assert registration.artifact_path is None
    assert registration.taxonomy_version == "1.0"
    assert {item.name for item in registration.metrics} == {
        "verified_transactions",
        "reference_transactions",
        "scored_transactions",
        "alert_count",
        "minimum_coverage_ratio",
    }
    with pytest.raises(ValueError, match="requires a completed model run"):
        registration_from_anomaly_detection(
            _anomaly_result(with_model=False),
            created_at=datetime(2025, 7, 2, tzinfo=UTC),
        )
