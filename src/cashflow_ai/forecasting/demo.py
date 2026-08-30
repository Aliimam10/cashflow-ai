"""Human-readable synthetic demonstration for Commit 22."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from cashflow_ai.forecasting.service import (
    ForecastingDataError,
    build_forecast_feature_rows,
    evaluate_forecast_baselines,
)
from cashflow_ai.schemas.forecasting import (
    ForecastDataset,
    ForecastDatasetPlan,
    WeeklyForecastTarget,
)
from cashflow_ai.schemas.statements import DateRange


def main() -> None:
    """Print coverage-safe features and baseline errors from fictional weeks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weeks", type=int, default=20)
    parser.add_argument("--test-weeks", type=int, default=3)
    parser.add_argument("--gap-week", type=int)
    args = parser.parse_args()
    if args.weeks < 13:
        parser.error("--weeks must be at least 13")
    if args.test_weeks < 1 or args.test_weeks > args.weeks - 10:
        parser.error(
            "--test-weeks must leave eight lag weeks, training, and validation"
        )
    if args.gap_week is not None and not 0 <= args.gap_week < args.weeks:
        parser.error("--gap-week must identify a generated week")
    first = date(2026, 1, 5)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(70 + (index % 4) * 10),
            known_recurring_outflow=Decimal("15.00")
            if index % 4 == 0
            else Decimal("0.00"),
            known_at=datetime.combine(
                first + timedelta(weeks=index, days=6), time.max, tzinfo=UTC
            ),
        )
        for index in range(args.weeks)
        if index != args.gap_week
    )
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic-profile",
        account_ids=("synthetic-account",),
        period=DateRange(
            start_date=first, end_date=first + timedelta(weeks=args.weeks, days=-1)
        ),
        knowledge_cutoff_at=datetime.combine(
            first + timedelta(weeks=args.weeks), time(), tzinfo=UTC
        ),
        payday_days=(1, 15),
    )
    dataset = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets,
        feature_rows=build_forecast_feature_rows(targets, plan.payday_days),
    )
    try:
        evaluation = evaluate_forecast_baselines(
            dataset,
            initial_training_weeks=max(
                1, len(dataset.feature_rows) - args.test_weeks - 1
            ),
            final_test_weeks=args.test_weeks,
        )
    except ForecastingDataError as error:
        parser.error(str(error))
    print("CashFlow AI synthetic forecast-data check")
    print(f"weekly targets: {len(targets)}")
    print(f"leakage-safe feature rows: {len(dataset.feature_rows)}")
    print(f"final test weeks: {len(evaluation.final_test_week_starts)}")
    for item in evaluation.metrics:
        print(f"{item.baseline.value}: MAE={item.mae.quantize(Decimal('0.01'))}")
    if args.gap_week is not None:
        print("gap retained: lag features restart after eight consecutive known weeks")
