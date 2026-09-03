"""Tests for pure recurring and forecast frontend projections."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from cashflow_ai.frontend.forecast_workflow import (
    complete_day_cutoff,
    forecast_chart,
    forecast_monday_after,
    forecast_request,
    next_monday,
    recurrence_request,
)


def test_request_defaults_are_cutoff_safe_and_user_horizon_is_preserved() -> None:
    as_of = date(2026, 8, 30)
    recurring = recurrence_request(profile_id="synthetic-profile", as_of_date=as_of)
    forecast = forecast_request(
        profile_id="synthetic-profile",
        account_id="synthetic-account",
        as_of_date=as_of,
        horizon_days=60,
        payday_days=(1, 15),
    )

    assert recurring.policy.minimum_occurrences == 3
    assert recurring.knowledge_cutoff_at == datetime(2026, 8, 31, tzinfo=UTC)
    assert forecast.dataset_plan.period.start_date == date(2025, 8, 31)
    assert forecast.dataset_plan.period.end_date == as_of
    assert forecast.path_plan.forecast_start == date(2026, 9, 7)
    assert forecast.path_plan.horizon_days == 60
    assert forecast.dataset_plan.payday_days == (1, 15)
    assert forecast.model_policy.random_seed == forecast.path_plan.policy.random_seed
    assert next_monday(date(2026, 8, 31)) == date(2026, 9, 7)
    tuesday_cutoff = complete_day_cutoff(date(2026, 9, 1))
    assert forecast_monday_after(tuesday_cutoff) == date(2026, 9, 7)


def test_forecast_chart_keeps_expected_and_interval_values_distinct() -> None:
    path = SimpleNamespace(
        daily_balances=(
            SimpleNamespace(
                forecast_date=date(2026, 8, 31),
                expected_balance=Decimal("100.00"),
                lower_balance=Decimal("80.00"),
                upper_balance=Decimal("120.00"),
            ),
        )
    )

    chart = forecast_chart(path)  # type: ignore[arg-type]

    assert chart["data"]["values"] == [
        {"date": "2026-08-31", "expected": 100.0, "lower": 80.0, "upper": 120.0}
    ]
    assert chart["layer"][0]["mark"]["type"] == "area"
    assert chart["layer"][1]["mark"]["type"] == "line"
