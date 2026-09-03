"""Pure request and display projections for recurring and forecasting UI."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from cashflow_ai.schemas.api_decisions import (
    BalanceForecastRequest,
    RecurrenceDetectionRequest,
)
from cashflow_ai.schemas.forecast_models import ForecastModelPolicy
from cashflow_ai.schemas.forecast_paths import (
    BalanceForecastPath,
    ForecastPathPlan,
    ForecastPathPolicy,
)
from cashflow_ai.schemas.forecasting import ForecastDatasetPlan
from cashflow_ai.schemas.freshness import FreshnessPolicy
from cashflow_ai.schemas.recurrence import RecurrenceDetectionPolicy
from cashflow_ai.schemas.statements import DateRange


def next_monday(after_date: date) -> date:
    """Return the first Monday strictly after an evidence date."""
    return after_date + timedelta(days=7 - after_date.weekday())


def complete_day_cutoff(evidence_date: date) -> datetime:
    """Represent when a selected UTC calendar day has fully completed."""
    return datetime.combine(evidence_date + timedelta(days=1), time.min, tzinfo=UTC)


def forecast_monday_after(cutoff: datetime) -> date:
    """Return a Monday whose origin is strictly after the knowledge cutoff."""
    candidate = cutoff.date() + timedelta(days=(-cutoff.date().weekday()) % 7)
    if datetime.combine(candidate, time.min, tzinfo=UTC) <= cutoff:
        candidate += timedelta(days=7)
    return candidate


def recurrence_request(
    *, profile_id: str, as_of_date: date
) -> RecurrenceDetectionRequest:
    """Build conservative, visible defaults for recurring-series detection."""
    return RecurrenceDetectionRequest(
        user_profile_id=profile_id,
        as_of_date=as_of_date,
        knowledge_cutoff_at=complete_day_cutoff(as_of_date),
        policy=RecurrenceDetectionPolicy(
            minimum_occurrences=3,
            maximum_amount_variation=Decimal("5.00"),
            maximum_interval_variation_days=4,
            maximum_skipped_occurrences=1,
            minimum_confidence=0.65,
        ),
    )


def forecast_request(
    *,
    profile_id: str,
    account_id: str,
    as_of_date: date,
    horizon_days: int,
    payday_days: tuple[int, ...],
) -> BalanceForecastRequest:
    """Build one leakage-safe forecast request from user-understandable controls."""
    cutoff = complete_day_cutoff(as_of_date)
    dataset = ForecastDatasetPlan(
        user_profile_id=profile_id,
        account_ids=(account_id,),
        period=DateRange(
            start_date=as_of_date - timedelta(days=364),
            end_date=as_of_date,
        ),
        knowledge_cutoff_at=cutoff,
        payday_days=payday_days,
    )
    return BalanceForecastRequest(
        dataset_plan=dataset,
        model_policy=ForecastModelPolicy(
            initial_training_weeks=8,
            final_test_weeks=4,
            minimum_training_weeks=8,
            minimum_relative_mae_improvement=0.05,
            maximum_relative_rmse_regression=0,
            maximum_absolute_bias_increase=Decimal("1.00"),
            maximum_iterations=100,
            learning_rate=0.05,
            maximum_leaf_nodes=15,
            minimum_samples_leaf=3,
            random_seed=42,
        ),
        path_plan=ForecastPathPlan(
            user_profile_id=profile_id,
            account_id=account_id,
            forecast_start=forecast_monday_after(cutoff),
            horizon_days=horizon_days,
            knowledge_cutoff_at=cutoff,
            policy=ForecastPathPolicy(
                interval_probability=Decimal("0.80"),
                simulation_count=500,
                minimum_residual_samples=3,
                minimum_weekly_uncertainty=Decimal("20.00"),
                low_confidence_multiplier=Decimal("1.50"),
                stale_data_multiplier=Decimal("2.00"),
                random_seed=42,
                freshness=FreshnessPolicy(
                    max_transaction_age_days=45,
                    max_balance_age_days=45,
                    max_coverage_age_days=45,
                    minimum_contiguous_coverage_days=60,
                ),
            ),
        ),
    )


def forecast_chart(path: BalanceForecastPath) -> dict[str, Any]:
    """Return expected balance plus an uncertainty band as Vega-Lite layers."""
    values = [
        {
            "date": point.forecast_date.isoformat(),
            "expected": float(point.expected_balance),
            "lower": float(point.lower_balance),
            "upper": float(point.upper_balance),
        }
        for point in path.daily_balances
    ]
    return {
        "data": {"values": values},
        "layer": [
            {
                "mark": {"type": "area", "opacity": 0.2},
                "encoding": {
                    "x": {"field": "date", "type": "temporal", "title": "Date"},
                    "y": {"field": "lower", "type": "quantitative", "title": "Balance"},
                    "y2": {"field": "upper"},
                },
            },
            {
                "mark": {"type": "line", "point": False},
                "encoding": {
                    "x": {"field": "date", "type": "temporal"},
                    "y": {"field": "expected", "type": "quantitative"},
                    "tooltip": ["date", "expected", "lower", "upper"],
                },
            },
        ],
    }


__all__ = [
    "complete_day_cutoff",
    "forecast_chart",
    "forecast_monday_after",
    "forecast_request",
    "next_monday",
    "recurrence_request",
]
