"""Pure request and display projections for planning, scenarios, and anomalies."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any

from cashflow_ai.frontend.forecast_workflow import (
    complete_day_cutoff,
    forecast_request,
)
from cashflow_ai.schemas.anomalies import (
    AnomalyDetectionPlan,
    AnomalyDetectionPolicy,
    AnomalySignal,
)
from cashflow_ai.schemas.api_decisions import PlanningApiRequest, ScenarioApiRequest
from cashflow_ai.schemas.planning import PlanningEvaluationPlan
from cashflow_ai.schemas.scenarios import (
    FinancialScenario,
    FinancialScenarioComparison,
)
from cashflow_ai.schemas.statements import DateRange


def calendar_month(value: date) -> DateRange:
    """Return the complete calendar month containing a selected date."""
    last_day = calendar.monthrange(value.year, value.month)[1]
    return DateRange(
        start_date=value.replace(day=1),
        end_date=value.replace(day=last_day),
    )


def monday_week(value: date) -> DateRange:
    """Return the Monday-to-Sunday week containing a selected date."""
    monday = value - timedelta(days=value.weekday())
    return DateRange(start_date=monday, end_date=monday + timedelta(days=6))


def planning_request(
    *,
    profile_id: str,
    account_ids: tuple[str, ...],
    as_of_date: date,
    horizon_days: int,
    payday_days: tuple[int, ...],
) -> PlanningApiRequest:
    """Build aligned server-side forecasts for one planning calculation."""
    return PlanningApiRequest(
        plan=PlanningEvaluationPlan(
            user_profile_id=profile_id,
            account_ids=account_ids,
            as_of_date=as_of_date,
        ),
        forecasts=tuple(
            forecast_request(
                profile_id=profile_id,
                account_id=account_id,
                as_of_date=as_of_date,
                horizon_days=horizon_days,
                payday_days=payday_days,
            )
            for account_id in account_ids
        ),
    )


def scenario_request(
    *,
    profile_id: str,
    account_id: str,
    as_of_date: date,
    horizon_days: int,
    payday_days: tuple[int, ...],
    scenario: FinancialScenario,
) -> ScenarioApiRequest:
    """Build one aligned, explicitly hypothetical scenario request."""
    forecast = forecast_request(
        profile_id=profile_id,
        account_id=account_id,
        as_of_date=as_of_date,
        horizon_days=horizon_days,
        payday_days=payday_days,
    )
    return ScenarioApiRequest(
        forecast=forecast,
        planning_plan=PlanningEvaluationPlan(
            user_profile_id=profile_id,
            account_ids=(account_id,),
            as_of_date=as_of_date,
        ),
        scenario=scenario,
    )


def anomaly_request(
    *, profile_id: str, account_ids: tuple[str, ...], as_of_date: date
) -> AnomalyDetectionPlan:
    """Build one conservative point-in-time anomaly review scan."""
    return AnomalyDetectionPlan(
        user_profile_id=profile_id,
        account_ids=account_ids,
        as_of_date=as_of_date,
        knowledge_cutoff_at=complete_day_cutoff(as_of_date),
        policy=AnomalyDetectionPolicy(),
    )


def scenario_balance_chart(comparison: FinancialScenarioComparison) -> dict[str, Any]:
    """Overlay baseline and hypothetical expected balances without hiding either."""
    values = [
        {
            "date": point.forecast_date.isoformat(),
            "balance": float(point.expected_balance),
            "path": label,
        }
        for label, path in (
            ("Baseline", comparison.baseline_forecast),
            ("Scenario", comparison.scenario_forecast),
        )
        for point in path.daily_balances
    ]
    return {
        "data": {"values": values},
        "mark": {"type": "line", "point": False},
        "encoding": {
            "x": {"field": "date", "type": "temporal", "title": "Date"},
            "y": {
                "field": "balance",
                "type": "quantitative",
                "title": "Expected balance",
                "scale": {"zero": False},
            },
            "color": {"field": "path", "type": "nominal"},
            "tooltip": ["date", "path", "balance"],
        },
    }


def signal_explanation(signal: AnomalySignal) -> str:
    """Translate a controlled signal into careful non-fraud wording."""
    label = signal.code.value.replace("_", " ").capitalize()
    details: list[str] = [f"{label} (strength {signal.score:.0%})"]
    if signal.observed_amount is not None:
        details.append(f"observed {signal.observed_amount:.2f}")
    if signal.reference_amount is not None:
        details.append(f"reference {signal.reference_amount:.2f}")
    return " · ".join(details)


__all__ = [
    "anomaly_request",
    "calendar_month",
    "monday_week",
    "planning_request",
    "scenario_balance_chart",
    "scenario_request",
    "signal_explanation",
]
