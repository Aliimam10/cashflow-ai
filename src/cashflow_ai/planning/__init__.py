"""Coverage-aware budgets, financial goals, and safe-spending estimates."""

from cashflow_ai.planning.adapters import projection_from_balance_forecast
from cashflow_ai.planning.scenarios import (
    ScenarioPlanningError,
    ScenarioPlanningErrorCode,
    evaluate_financial_scenario,
)
from cashflow_ai.planning.service import (
    PlanningServiceError,
    PlanningServiceErrorCode,
    create_budget,
    create_financial_goal,
    evaluate_financial_plan,
)

__all__ = [
    "PlanningServiceError",
    "PlanningServiceErrorCode",
    "ScenarioPlanningError",
    "ScenarioPlanningErrorCode",
    "create_budget",
    "create_financial_goal",
    "evaluate_financial_plan",
    "evaluate_financial_scenario",
    "projection_from_balance_forecast",
]
