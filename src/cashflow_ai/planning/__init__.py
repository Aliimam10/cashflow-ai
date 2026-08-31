"""Coverage-aware budgets, financial goals, and safe-spending estimates."""

from cashflow_ai.planning.adapters import projection_from_balance_forecast
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
    "create_budget",
    "create_financial_goal",
    "evaluate_financial_plan",
    "projection_from_balance_forecast",
]
