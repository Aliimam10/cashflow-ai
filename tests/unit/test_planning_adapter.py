from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from cashflow_ai.planning import projection_from_balance_forecast
from cashflow_ai.schemas import (
    BalanceForecastPath,
    BalanceSnapshotSource,
    Currency,
    DailyBalancePathPoint,
    ForecastBaselineName,
    ForecastIntervalMethod,
    ForecastOpeningBalance,
    ForecastPathPlan,
    ForecastPathPolicy,
    ForecastPathWarningCode,
    ForecastScenario,
    FreshnessPolicy,
    WeeklySpendingPath,
)


def test_balance_forecast_adapter_keeps_only_aggregate_planning_evidence() -> None:
    start = date(2026, 8, 17)
    cutoff = datetime(2026, 8, 16, 23, tzinfo=UTC)
    plan = ForecastPathPlan(
        user_profile_id="synthetic-profile",
        account_id="synthetic-account",
        forecast_start=start,
        horizon_days=7,
        knowledge_cutoff_at=cutoff,
        policy=ForecastPathPolicy(
            interval_probability=Decimal("0.80"),
            simulation_count=100,
            minimum_residual_samples=3,
            minimum_weekly_uncertainty=Decimal("20.00"),
            low_confidence_multiplier=Decimal("1.50"),
            stale_data_multiplier=Decimal("2.00"),
            random_seed=7,
            freshness=FreshnessPolicy(
                max_transaction_age_days=7,
                max_balance_age_days=7,
                max_coverage_age_days=7,
                minimum_contiguous_coverage_days=30,
            ),
        ),
    )
    daily = tuple(
        DailyBalancePathPoint(
            forecast_date=start + timedelta(days=index),
            expected_discretionary_outflow=Decimal("10.00"),
            recurring_net_flow=Decimal("0.00"),
            scenario_adjustment=Decimal("0.00"),
            expected_balance=Decimal(990 - index * 10),
            lower_balance=Decimal(940 - index * 10),
            upper_balance=Decimal(1040 - index * 10),
        )
        for index in range(7)
    )
    path = BalanceForecastPath(
        plan=plan,
        scenario=ForecastScenario(),
        opening_balance=ForecastOpeningBalance(
            balance=Decimal("1000.00"),
            currency=Currency.GBP,
            as_of_date=date(2026, 8, 16),
            recorded_at=cutoff,
            source=BalanceSnapshotSource.MANUAL,
        ),
        selected_model=ForecastBaselineName.RECENT_ROLLING_MEAN,
        interval_method=ForecastIntervalMethod.RESIDUAL_BOOTSTRAP,
        widening_multiplier=Decimal("1.50"),
        warnings=(ForecastPathWarningCode.LOW_CONFIDENCE_MODEL,),
        freshness_warnings=(),
        recurring_occurrences=(),
        weekly_spending=(
            WeeklySpendingPath(
                week_start=start,
                week_end=start + timedelta(days=6),
                expected_discretionary_spending=Decimal("70.00"),
                lower_discretionary_spending=Decimal("50.00"),
                upper_discretionary_spending=Decimal("90.00"),
            ),
        ),
        daily_balances=daily,
        interval_performance=None,
        expected_final_balance=daily[-1].expected_balance,
        lower_final_balance=daily[-1].lower_balance,
        upper_final_balance=daily[-1].upper_balance,
    )

    projection = projection_from_balance_forecast(path)

    assert projection.account_id == "synthetic-account"
    assert projection.period.start_date == start
    assert projection.period.end_date == start + timedelta(days=6)
    assert projection.lowest_lower_balance == Decimal("880.00")
    assert projection.expected_end_balance == Decimal("930.00")
    assert projection.lower_end_balance == Decimal("880.00")
    assert projection.expected_discretionary_spending == Decimal("70.00")
    assert projection.forecast_warnings == (
        ForecastPathWarningCode.LOW_CONFIDENCE_MODEL,
    )
