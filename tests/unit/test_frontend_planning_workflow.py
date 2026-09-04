"""Tests for pure planning, scenario, and anomaly frontend projections."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from cashflow_ai.frontend.planning_workflow import (
    anomaly_request,
    calendar_month,
    monday_week,
    planning_request,
    scenario_balance_chart,
    scenario_request,
    signal_explanation,
)
from cashflow_ai.schemas.anomalies import AnomalySignal, AnomalySignalCode
from cashflow_ai.schemas.scenarios import FinancialScenario, FinancialScenarioType


def test_period_and_request_builders_keep_user_scope_and_cutoffs_aligned() -> None:
    as_of = date(2026, 8, 30)
    month = calendar_month(date(2026, 2, 12))
    week = monday_week(date(2026, 9, 2))
    planning = planning_request(
        profile_id="synthetic-profile",
        account_ids=("account-1", "account-2"),
        as_of_date=as_of,
        horizon_days=60,
        payday_days=(1, 15),
    )
    scenario = FinancialScenario(
        scenario_id="synthetic-scenario",
        user_profile_id="synthetic-profile",
        account_id="account-1",
        scenario_type=FinancialScenarioType.ONE_OFF_PURCHASE,
        name="Fictional purchase",
        start_date=date(2026, 9, 7),
        amount=Decimal("100.00"),
    )
    comparison = scenario_request(
        profile_id="synthetic-profile",
        account_id="account-1",
        as_of_date=as_of,
        horizon_days=30,
        payday_days=(1, 15),
        scenario=scenario,
    )
    anomaly = anomaly_request(
        profile_id="synthetic-profile",
        account_ids=("account-1",),
        as_of_date=as_of,
    )

    assert (month.start_date, month.end_date) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )
    assert (week.start_date, week.end_date) == (
        date(2026, 8, 31),
        date(2026, 9, 6),
    )
    assert tuple(item.path_plan.account_id for item in planning.forecasts) == (
        "account-1",
        "account-2",
    )
    assert all(item.path_plan.horizon_days == 60 for item in planning.forecasts)
    assert comparison.scenario is scenario
    assert comparison.planning_plan.account_ids == ("account-1",)
    assert comparison.forecast.path_plan.horizon_days == 30
    assert anomaly.account_ids == ("account-1",)
    assert anomaly.knowledge_cutoff_at.date() == date(2026, 8, 31)


def test_chart_and_signal_explanations_are_data_minimised() -> None:
    point = SimpleNamespace(
        forecast_date=date(2026, 9, 1), expected_balance=Decimal("123.45")
    )
    comparison = SimpleNamespace(
        baseline_forecast=SimpleNamespace(daily_balances=(point,)),
        scenario_forecast=SimpleNamespace(
            daily_balances=(
                SimpleNamespace(
                    forecast_date=date(2026, 9, 1),
                    expected_balance=Decimal("100.00"),
                ),
            )
        ),
    )
    chart = scenario_balance_chart(comparison)  # type: ignore[arg-type]
    short = signal_explanation(
        AnomalySignal(
            code=AnomalySignalCode.NEGATIVE_BALANCE_EVENT,
            score=Decimal("0.750000"),
        )
    )
    detailed = signal_explanation(
        AnomalySignal(
            code=AnomalySignalCode.UNUSUALLY_LARGE_TRANSACTION,
            score=Decimal("0.800000"),
            observed_amount=Decimal("500.00"),
            reference_amount=Decimal("50.00"),
        )
    )

    assert chart["data"]["values"] == [
        {"date": "2026-09-01", "balance": 123.45, "path": "Baseline"},
        {"date": "2026-09-01", "balance": 100.0, "path": "Scenario"},
    ]
    assert chart["encoding"]["color"]["field"] == "path"
    assert short == "Negative balance event (strength 75%)"
    assert detailed.endswith("observed 500.00 · reference 50.00")
