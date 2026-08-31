"""Data-minimising adapter from balance forecasts into financial planning."""

from __future__ import annotations

from decimal import Decimal

from cashflow_ai.schemas.forecast_paths import BalanceForecastPath
from cashflow_ai.schemas.planning import PlanningBalanceProjection
from cashflow_ai.schemas.statements import DateRange


def projection_from_balance_forecast(
    path: BalanceForecastPath,
) -> PlanningBalanceProjection:
    """Keep aggregate balance and spending evidence without simulation paths."""
    return PlanningBalanceProjection(
        account_id=path.plan.account_id,
        currency=path.opening_balance.currency,
        period=DateRange(
            start_date=path.daily_balances[0].forecast_date,
            end_date=path.daily_balances[-1].forecast_date,
        ),
        lowest_lower_balance=min(item.lower_balance for item in path.daily_balances),
        expected_end_balance=path.expected_final_balance,
        lower_end_balance=path.lower_final_balance,
        expected_discretionary_spending=sum(
            (item.expected_discretionary_outflow for item in path.daily_balances),
            start=Decimal("0.00"),
        ),
        forecast_warnings=path.warnings,
    )


__all__ = ["projection_from_balance_forecast"]
