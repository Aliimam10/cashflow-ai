"""Human-readable synthetic demonstration of Commit 23 model selection."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from cashflow_ai.forecasting.model import (
    predict_discretionary_spending,
    train_primary_forecaster,
)
from cashflow_ai.forecasting.service import (
    build_forecast_feature_rows,
    build_next_forecast_inference_row,
)
from cashflow_ai.schemas.forecast_models import ForecastModelPolicy
from cashflow_ai.schemas.forecasting import (
    ForecastDataset,
    ForecastDatasetPlan,
    RecurringOutflowProjection,
    WeeklyForecastTarget,
)
from cashflow_ai.schemas.statements import DateRange


def main() -> None:
    """Train on fictional weekly values and print comparison evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weeks", type=int, default=36)
    parser.add_argument("--test-weeks", type=int, default=4)
    parser.add_argument("--minimum-improvement", type=float, default=0.05)
    parser.add_argument("--flat", action="store_true")
    args = parser.parse_args()
    if args.weeks < 22:
        parser.error("--weeks must be at least 22")
    if args.test_weeks < 1 or args.test_weeks >= args.weeks - 8:
        parser.error("--test-weeks must leave at least one feature row for training")
    if not 0 <= args.minimum_improvement <= 1:
        parser.error("--minimum-improvement must be from 0 through 1")
    first = date(2024, 1, 1)
    values = tuple(
        Decimal("0") if args.flat else Decimal(30 if index % 2 else 180)
        for index in range(args.weeks)
    )
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=values[index],
            known_recurring_outflow=Decimal("10") if index % 4 == 0 else Decimal("0"),
            known_at=datetime.combine(
                first + timedelta(weeks=index, days=6), time.max, tzinfo=UTC
            ),
        )
        for index in range(args.weeks)
    )
    features = build_forecast_feature_rows(targets, (1, 15))
    period_end = first + timedelta(weeks=args.weeks, days=-1)
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic-profile",
        account_ids=("synthetic-account",),
        period=DateRange(
            start_date=first,
            end_date=period_end,
        ),
        knowledge_cutoff_at=datetime.combine(period_end, time.max, tzinfo=UTC),
        payday_days=(1, 15),
    )
    dataset = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets,
        feature_rows=features,
        next_recurring_outflow=RecurringOutflowProjection(
            week_start=first + timedelta(weeks=args.weeks),
            amount=(
                Decimal("0")
                if args.flat
                else Decimal("10")
                if args.weeks % 4 == 0
                else Decimal("0")
            ),
            known_at=plan.knowledge_cutoff_at,
        ),
    )
    result = train_primary_forecaster(
        dataset,
        policy=ForecastModelPolicy(
            initial_training_weeks=8,
            final_test_weeks=args.test_weeks,
            minimum_training_weeks=8,
            minimum_relative_mae_improvement=args.minimum_improvement,
            maximum_relative_rmse_regression=0,
            maximum_absolute_bias_increase=Decimal("1"),
            maximum_iterations=30,
            learning_rate=0.1,
            maximum_leaf_nodes=10,
            minimum_samples_leaf=2,
            random_seed=7,
        ),
    )
    comparison = result.comparison
    next_input = build_next_forecast_inference_row(dataset)
    prediction = predict_discretionary_spending(result, next_input)
    print("CashFlow AI synthetic primary-forecast check")
    print(f"feature rows: {len(features)}")
    print(f"selected model: {comparison.selected_model.value}")
    print(f"advanced selected: {str(comparison.selected).lower()}")
    if comparison.final_test is not None:
        assert comparison.best_baseline_final_mae is not None
        print(f"model final MAE: {comparison.final_test.mae.quantize(Decimal('0.01'))}")
        print(
            "best baseline final MAE: "
            f"{comparison.best_baseline_final_mae.quantize(Decimal('0.01'))}"
        )
        print(f"top feature: {comparison.feature_importance[0].feature_name}")
    print(f"reason: {comparison.selection_reason}")
    print(f"next forecast week: {prediction.week_start.isoformat()}")
    print(f"predicted discretionary spending: {prediction.discretionary_spending}")
