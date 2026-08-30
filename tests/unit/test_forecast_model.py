"""Tests for gradient-boosting forecast training and conservative selection."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import cashflow_ai.forecasting.model as model_module
import cashflow_ai.forecasting.model_demo as model_demo
from cashflow_ai.forecasting import (
    ForecastingDataError,
    ForecastingDataErrorCode,
    build_forecast_feature_rows,
    build_next_forecast_inference_row,
    evaluate_forecast_baselines,
    predict_discretionary_spending,
    train_primary_forecaster,
)
from cashflow_ai.schemas import (
    DateRange,
    ForecastBaselineName,
    ForecastDataset,
    ForecastDatasetPlan,
    ForecastFeatureImportance,
    ForecastFeatureRow,
    ForecastModelComparison,
    ForecastModelName,
    ForecastModelPolicy,
    ForecastPrediction,
    HorizonPerformance,
    RecurringOutflowProjection,
    RegressionMetrics,
    WeeklyForecastTarget,
)
from cashflow_ai.schemas.forecast_models import ForecastInferenceRow


def _known_after_week(week_start: date) -> datetime:
    return datetime.combine(week_start + timedelta(days=6), time.max, tzinfo=UTC)


def _dataset(*, weeks: int = 36, predictable: bool = True) -> ForecastDataset:
    first = date(2024, 1, 1)
    values = tuple(
        Decimal(30 if index % 2 else 180) if predictable else Decimal("0")
        for index in range(weeks)
    )
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=values[index],
            known_recurring_outflow=Decimal("10") if index % 4 == 0 else Decimal("0"),
            known_at=_known_after_week(first + timedelta(weeks=index)),
        )
        for index in range(weeks)
    )
    rows = build_forecast_feature_rows(targets, (1, 15))
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
        feature_rows=rows,
        next_recurring_outflow=RecurringOutflowProjection(
            week_start=first + timedelta(weeks=weeks),
            amount=Decimal("10") if weeks % 4 == 0 else Decimal("0"),
            known_at=plan.knowledge_cutoff_at,
        ),
    )


def _next_input(*, weeks: int = 36, predictable: bool = True) -> ForecastInferenceRow:
    return build_next_forecast_inference_row(
        _dataset(weeks=weeks, predictable=predictable)
    )


def _policy(**changes: Any) -> ForecastModelPolicy:
    values = {
        "initial_training_weeks": 8,
        "final_test_weeks": 4,
        "minimum_training_weeks": 8,
        "minimum_relative_mae_improvement": 0.05,
        "maximum_relative_rmse_regression": 0,
        "maximum_absolute_bias_increase": Decimal("1"),
        "maximum_iterations": 30,
        "learning_rate": 0.1,
        "maximum_leaf_nodes": 10,
        "minimum_samples_leaf": 2,
        "random_seed": 7,
        **changes,
    }
    return ForecastModelPolicy(**values)


def test_gradient_boosting_beats_baselines_and_predicts_non_negative_values() -> None:
    dataset = _dataset()
    trained = train_primary_forecaster(dataset, policy=_policy())
    comparison = trained.comparison
    assert comparison.model_name is ForecastModelName.HIST_GRADIENT_BOOSTING
    assert comparison.selected is True
    assert comparison.selected_model is ForecastModelName.HIST_GRADIENT_BOOSTING
    assert comparison.policy == _policy()
    assert comparison.knowledge_cutoff_at == dataset.plan.knowledge_cutoff_at
    assert comparison.training_sample_count == len(dataset.feature_rows)
    assert comparison.final_test is not None
    assert comparison.expanding_validation is not None
    assert comparison.best_baseline_final_mae is not None
    assert comparison.best_baseline_expanding_mae is not None
    assert comparison.final_test.mae < comparison.best_baseline_final_mae
    assert comparison.expanding_validation.mae < comparison.best_baseline_expanding_mae
    assert len(comparison.horizon_performance) == 1
    assert comparison.horizon_performance[0].horizon_weeks == 1
    assert comparison.horizon_performance[0].sample_count == 4
    assert len(comparison.feature_importance) == 10
    assert comparison.feature_importance[0].feature_name == "lag_1"
    prediction = predict_discretionary_spending(trained, _next_input())
    assert prediction.week_start == _next_input().week_start
    assert prediction.discretionary_spending >= 0
    assert prediction.forecast_origin_at == _next_input().forecast_origin_at
    assert prediction.selected_model is ForecastModelName.HIST_GRADIENT_BOOSTING
    assert prediction.advanced_model_selected is True
    assert prediction.training_knowledge_cutoff_at == dataset.plan.knowledge_cutoff_at


def test_constant_series_keeps_and_executes_zero_baseline_instead_of_tie() -> None:
    trained = train_primary_forecaster(_dataset(predictable=False), policy=_policy())
    assert trained.comparison.selected is False
    assert trained.comparison.selected_model is ForecastBaselineName.HISTORICAL_MEAN
    assert trained.estimator is None
    prediction = predict_discretionary_spending(trained, _next_input(predictable=False))
    assert prediction.discretionary_spending == Decimal("0.00")


def test_low_data_returns_explicit_recent_mean_fallback() -> None:
    trained = train_primary_forecaster(_dataset(weeks=12), policy=_policy())
    comparison = trained.comparison
    assert comparison.model_name is ForecastModelName.LOW_DATA_FALLBACK
    assert comparison.selected is False
    assert comparison.selected_model is ForecastBaselineName.RECENT_ROLLING_MEAN
    assert comparison.final_test is None
    assert comparison.feature_importance == ()
    assert comparison.best_baseline_final_mae is None
    assert comparison.best_baseline_expanding_mae is None
    assert comparison.training_sample_count == len(_dataset(weeks=12).feature_rows)
    prediction = predict_discretionary_spending(trained, _next_input(weeks=12))
    assert prediction.discretionary_spending == _next_input(weeks=12).rolling_mean_4


@pytest.mark.parametrize(
    ("baseline", "expected"),
    [
        (ForecastBaselineName.SEASONAL_NAIVE, Decimal("180.00")),
        (ForecastBaselineName.RECURRING_ONLY, Decimal("0.00")),
        (ForecastBaselineName.ZERO_DISCRETIONARY, Decimal("0.00")),
    ],
)
def test_every_selected_baseline_has_executable_inference(
    baseline: ForecastBaselineName, expected: Decimal
) -> None:
    dataset = _dataset(weeks=60)
    trained = train_primary_forecaster(dataset, policy=_policy())
    comparison = trained.comparison.model_copy(
        update={"selected": False, "selected_model": baseline}
    )
    fallback = model_module.TrainedPrimaryForecaster(
        estimator=None,
        comparison=comparison,
        latest_observed_week=trained.latest_observed_week,
        latest_observed_known_at=trained.latest_observed_known_at,
        target_history=trained.target_history,
    )
    prediction = predict_discretionary_spending(fallback, _next_input(weeks=60))
    assert prediction.discretionary_spending == expected


def test_model_training_is_reproducible_and_never_mutates_dataset() -> None:
    dataset = _dataset()
    before = dataset.model_dump(mode="json")
    first = train_primary_forecaster(dataset, policy=_policy())
    second = train_primary_forecaster(dataset, policy=_policy())
    assert first.comparison == second.comparison
    assert dataset.model_dump(mode="json") == before


def test_model_uses_the_same_point_in_time_training_rows_as_baselines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _dataset(weeks=48)
    targets = list(original.weekly_targets)
    late_week = targets[8].week_start
    final_test_origin = original.feature_rows[-4].forecast_origin_at
    targets[8] = targets[8].model_copy(
        update={"known_at": final_test_origin + timedelta(days=1)}
    )
    dataset = ForecastDataset(
        plan=original.plan,
        daily_calendar=(),
        weekly_targets=tuple(targets),
        feature_rows=build_forecast_feature_rows(
            tuple(targets), original.plan.payday_days
        ),
    )
    policy = _policy()
    evaluation = evaluate_forecast_baselines(
        dataset,
        initial_training_weeks=policy.initial_training_weeks,
        final_test_weeks=policy.final_test_weeks,
    )
    original_fit = model_module._fit
    fitted_weeks: list[tuple[date, ...]] = []

    def capture_fit(
        fit_policy: ForecastModelPolicy, rows: tuple[ForecastFeatureRow, ...]
    ) -> Any:
        fitted_weeks.append(tuple(row.week_start for row in rows))
        return original_fit(fit_policy, rows)

    monkeypatch.setattr(model_module, "_fit", capture_fit)
    trained = train_primary_forecaster(dataset, policy=policy)

    assert trained.comparison.training_week_starts == (
        evaluation.final_training_week_starts
    )
    assert late_week not in trained.comparison.training_week_starts
    assert evaluation.final_training_week_starts in fitted_weeks
    assert all(
        fold.training_week_starts in fitted_weeks for fold in evaluation.expanding_folds
    )


def test_contracts_reject_incoherent_policy_metrics_and_selection() -> None:
    with pytest.raises(ValidationError):
        _policy(initial_training_weeks=5, minimum_training_weeks=10)
    with pytest.raises(ValidationError):
        RegressionMetrics(
            mae=Decimal("1"),
            rmse=Decimal("1"),
            bias=Decimal("0"),
            actuals=(Decimal("1"),),
            predictions=(Decimal("1"), Decimal("2")),
        )
    metrics = RegressionMetrics(
        mae=Decimal("0"),
        rmse=Decimal("0"),
        bias=Decimal("0"),
        actuals=(Decimal("1"),),
        predictions=(Decimal("1"),),
    )
    with pytest.raises(ValidationError):
        HorizonPerformance(horizon_weeks=1, sample_count=2, metrics=metrics)
    with pytest.raises(ValidationError):
        ForecastInferenceRow.model_validate(
            {**_next_input().model_dump(), "target": Decimal("10")}
        )
    with pytest.raises(ValidationError):
        ForecastInferenceRow.model_validate(
            {
                **_next_input().model_dump(),
                "week_start": _next_input().week_start + timedelta(days=1),
            }
        )
    with pytest.raises(ValidationError):
        ForecastInferenceRow.model_validate({**_next_input().model_dump(), "month": 1})
    with pytest.raises(ValidationError):
        ForecastInferenceRow.model_validate(
            {
                **_next_input().model_dump(),
                "forecast_origin_at": datetime(2026, 1, 1),
            }
        )
    with pytest.raises(ValidationError):
        ForecastInferenceRow.model_validate(
            {
                **_next_input().model_dump(),
                "recurring_outflow_known_at": _next_input().forecast_origin_at,
            }
        )
    prediction = predict_discretionary_spending(
        train_primary_forecaster(_dataset(), policy=_policy()), _next_input()
    )
    for update in (
        {"forecast_origin_at": prediction.forecast_origin_at + timedelta(days=1)},
        {"training_knowledge_cutoff_at": datetime(2026, 1, 1)},
        {"advanced_model_selected": False},
    ):
        with pytest.raises(ValidationError):
            ForecastPrediction.model_validate({**prediction.model_dump(), **update})
    base = train_primary_forecaster(_dataset(weeks=12), policy=_policy()).comparison
    with pytest.raises(ValidationError):
        ForecastModelComparison(
            **base.model_dump(
                exclude={"model_name", "final_test", "expanding_validation"}
            ),
            model_name=ForecastModelName.LOW_DATA_FALLBACK,
            final_test=RegressionMetrics(
                mae=Decimal("0"),
                rmse=Decimal("0"),
                bias=Decimal("0"),
                actuals=(Decimal("1"),),
                predictions=(Decimal("1"),),
            ),
            expanding_validation=RegressionMetrics(
                mae=Decimal("0"),
                rmse=Decimal("0"),
                bias=Decimal("0"),
                actuals=(Decimal("1"),),
                predictions=(Decimal("1"),),
            ),
        )
    with pytest.raises(ValidationError):
        ForecastModelComparison(
            **base.model_dump(exclude={"model_name"}),
            model_name=ForecastModelName.HIST_GRADIENT_BOOSTING,
        )
    with pytest.raises(ValidationError):
        ForecastModelComparison(
            **base.model_dump(exclude={"selected"}),
            selected=True,
        )


def test_model_comparison_rejects_partial_and_misaligned_evidence() -> None:
    evaluated = train_primary_forecaster(_dataset(), policy=_policy()).comparison
    payload = evaluated.model_dump()
    invalid_updates: tuple[dict[str, object], ...] = (
        {"final_test": None},
        {"best_baseline_final_rmse": None},
        {
            "feature_importance": (
                *evaluated.feature_importance,
                ForecastFeatureImportance(
                    feature_name=evaluated.feature_importance[0].feature_name,
                    mae_increase=Decimal("0"),
                ),
            )
        },
        {"training_week_starts": evaluated.training_week_starts[::-1]},
        {"final_test_week_starts": (evaluated.training_week_starts[-1],)},
        {"final_test_week_starts": evaluated.final_test_week_starts[:1]},
        {
            "selected": False,
            "selected_model": ForecastModelName.LOW_DATA_FALLBACK,
        },
    )
    for update in invalid_updates:
        with pytest.raises(ValidationError):
            ForecastModelComparison.model_validate({**payload, **update})


def test_negative_raw_model_output_is_clipped(monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = _dataset()
    trained = train_primary_forecaster(dataset, policy=_policy())
    monkeypatch.setattr(
        model_module,
        "_predict",
        lambda *_args: (-10.0,),
    )
    result = predict_discretionary_spending(trained, _next_input())
    assert result.discretionary_spending == Decimal("0.0")


def test_training_rejects_unordered_and_inconsistent_rows() -> None:
    dataset = _dataset()
    unordered_targets = dataset.model_copy(
        update={"weekly_targets": dataset.weekly_targets[::-1]}
    )
    with pytest.raises(ForecastingDataError) as exc_info:
        train_primary_forecaster(unordered_targets, policy=_policy())
    assert exc_info.value.code is ForecastingDataErrorCode.INVALID_EVALUATION_POLICY

    unordered = dataset.model_copy(update={"feature_rows": dataset.feature_rows[::-1]})
    with pytest.raises(ForecastingDataError) as exc_info:
        train_primary_forecaster(unordered, policy=_policy())
    assert exc_info.value.code is ForecastingDataErrorCode.INVALID_EVALUATION_POLICY

    changed = dataset.feature_rows[0].model_copy(update={"target": Decimal("999")})
    inconsistent = dataset.model_copy(
        update={"feature_rows": (changed, *dataset.feature_rows[1:])}
    )
    with pytest.raises(ForecastingDataError) as exc_info:
        train_primary_forecaster(inconsistent, policy=_policy())
    assert exc_info.value.code is ForecastingDataErrorCode.INVALID_EVALUATION_POLICY

    changed_lag = dataset.feature_rows[0].model_copy(update={"lag_1": Decimal("999")})
    derived_incorrectly = dataset.model_copy(
        update={"feature_rows": (changed_lag, *dataset.feature_rows[1:])}
    )
    with pytest.raises(ForecastingDataError) as exc_info:
        train_primary_forecaster(derived_incorrectly, policy=_policy())
    assert exc_info.value.code is ForecastingDataErrorCode.INVALID_EVALUATION_POLICY


def test_prediction_rejects_an_observed_or_skipped_week() -> None:
    trained = train_primary_forecaster(_dataset(), policy=_policy())
    future = _next_input()
    for offset in (-1, 1):
        invalid = future.model_copy(
            update={"week_start": future.week_start + timedelta(weeks=offset)}
        )
        with pytest.raises(ForecastingDataError) as exc_info:
            predict_discretionary_spending(trained, invalid)
        assert exc_info.value.code is ForecastingDataErrorCode.INVALID_EVALUATION_POLICY


def test_prediction_rejects_any_history_learned_at_or_after_its_origin() -> None:
    original = _dataset()
    origin = datetime.combine(
        original.weekly_targets[-1].week_start + timedelta(weeks=1),
        time.min,
        tzinfo=UTC,
    )
    targets = (
        original.weekly_targets[0].model_copy(update={"known_at": origin}),
        *original.weekly_targets[1:],
    )
    plan = original.plan.model_copy(
        update={"knowledge_cutoff_at": origin + timedelta(days=1)}
    )
    dataset = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets,
        feature_rows=build_forecast_feature_rows(targets, plan.payday_days),
        next_recurring_outflow=original.next_recurring_outflow,
    )
    trained = train_primary_forecaster(dataset, policy=_policy())
    row = build_next_forecast_inference_row(dataset)
    with pytest.raises(ForecastingDataError) as exc_info:
        predict_discretionary_spending(trained, row)
    assert exc_info.value.code is ForecastingDataErrorCode.INVALID_EVALUATION_POLICY


def test_signed_permutation_importance_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    importances = [-1.25, *range(1, 10)]
    monkeypatch.setattr(
        model_module,
        "permutation_importance",
        lambda *_args, **_kwargs: SimpleNamespace(importances_mean=importances),
    )
    trained = train_primary_forecaster(_dataset(), policy=_policy())
    by_name = {
        item.feature_name: item.mae_increase
        for item in trained.comparison.feature_importance
    }
    assert by_name["lag_1"] == Decimal("-1.25")


def test_manual_model_demo_selected_fallback_and_parameter_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["forecast-model-demo", "--weeks", "36", "--test-weeks", "4"],
    )
    model_demo.main()
    selected_output = capsys.readouterr().out
    assert "CashFlow AI synthetic primary-forecast check" in selected_output
    assert "advanced selected: true" in selected_output
    assert "top feature: lag_1" in selected_output

    monkeypatch.setattr(
        "sys.argv",
        [
            "forecast-model-demo",
            "--weeks",
            "36",
            "--test-weeks",
            "4",
            "--flat",
        ],
    )
    model_demo.main()
    fallback_output = capsys.readouterr().out
    assert "advanced selected: false" in fallback_output
    assert "selected model: historical_mean" in fallback_output

    monkeypatch.setattr(
        "sys.argv",
        ["forecast-model-demo", "--weeks", "22", "--test-weeks", "6"],
    )
    model_demo.main()
    low_data_output = capsys.readouterr().out
    assert "recent-mean fallback" in low_data_output

    monkeypatch.setattr("sys.argv", ["forecast-model-demo", "--weeks", "21"])
    with pytest.raises(SystemExit):
        model_demo.main()

    monkeypatch.setattr("sys.argv", ["forecast-model-demo", "--test-weeks", "0"])
    with pytest.raises(SystemExit):
        model_demo.main()

    monkeypatch.setattr(
        "sys.argv", ["forecast-model-demo", "--minimum-improvement", "1.1"]
    )
    with pytest.raises(SystemExit):
        model_demo.main()
