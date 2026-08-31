"""Isolated baseline-versus-scenario financial comparisons."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.forecasting.model import TrainedPrimaryForecaster
from cashflow_ai.forecasting.paths import build_balance_forecast_path
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.repositories import PlanningRepository
from cashflow_ai.planning.adapters import projection_from_balance_forecast
from cashflow_ai.planning.service import evaluate_financial_plan
from cashflow_ai.schemas.analytics import AnalyticsCoverageStatus
from cashflow_ai.schemas.forecast_paths import (
    BalanceForecastPath,
    ForecastPathPlan,
    ForecastScenario,
    ForecastScenarioAdjustment,
    ScenarioAdjustmentKind,
)
from cashflow_ai.schemas.forecasting import ForecastDataset
from cashflow_ai.schemas.money import MONEY_QUANTUM
from cashflow_ai.schemas.planning import (
    BudgetProgress,
    BudgetType,
    FinancialGoalProgress,
    FinancialGoalType,
    FinancialPlanningResult,
    PlanningEvaluationPlan,
    PlanningWarningCode,
)
from cashflow_ai.schemas.recurrence import RecurrenceFrequency
from cashflow_ai.schemas.scenarios import (
    FinancialScenario,
    FinancialScenarioComparison,
    FinancialScenarioType,
    ScenarioBalanceEffect,
    ScenarioBudgetEffect,
    ScenarioComparisonWarningCode,
    ScenarioGoalEffect,
    ScenarioSafeSpendingEffect,
    ScenarioUncertainty,
)
from cashflow_ai.schemas.transactions import FinancialRole

_ZERO = Decimal("0.00")
_EXPENSE_INCREASE_TYPES = {
    FinancialScenarioType.ONE_OFF_PURCHASE,
    FinancialScenarioType.TRAVEL_EXPENSE,
    FinancialScenarioType.NEW_SUBSCRIPTION,
    FinancialScenarioType.RENT_INCREASE,
}
_EXPENSE_REDUCTION_TYPES = {
    FinancialScenarioType.CANCELLED_SUBSCRIPTION,
    FinancialScenarioType.CATEGORY_SPENDING_REDUCTION,
}
_OUTFLOW_TYPES = _EXPENSE_INCREASE_TYPES | {
    FinancialScenarioType.INCOME_REDUCTION,
    FinancialScenarioType.NEW_SAVINGS_TRANSFER,
}
_NON_RECURRING_BUDGET_TYPES = {
    FinancialScenarioType.ONE_OFF_PURCHASE,
    FinancialScenarioType.TRAVEL_EXPENSE,
    FinancialScenarioType.CATEGORY_SPENDING_REDUCTION,
}


class ScenarioPlanningErrorCode(StrEnum):
    """Stable failures that do not expose private financial descriptions."""

    PLAN_SCOPE_MISMATCH = "plan_scope_mismatch"
    SCENARIO_OUTSIDE_HORIZON = "scenario_outside_horizon"
    CATEGORY_NOT_FOUND = "category_not_found"
    CATEGORY_INACTIVE = "category_inactive"
    RECURRING_PAYMENT_NOT_FOUND = "recurring_payment_not_found"
    UNCERTAINTY_INHERITANCE_FAILED = "uncertainty_inheritance_failed"


class ScenarioPlanningError(ValueError):
    """Controlled scenario-planning failure with a privacy-safe code."""

    def __init__(self, code: ScenarioPlanningErrorCode, message: str) -> None:
        """Store the stable failure code and safe explanation."""
        super().__init__(message)
        self.code = code


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _advance(value: date, frequency: RecurrenceFrequency) -> date:
    if frequency is RecurrenceFrequency.WEEKLY:
        return value + timedelta(days=7)
    if frequency is RecurrenceFrequency.FORTNIGHTLY:
        return value + timedelta(days=14)
    months = {
        RecurrenceFrequency.MONTHLY: 1,
        RecurrenceFrequency.QUARTERLY: 3,
        RecurrenceFrequency.ANNUAL: 12,
    }[frequency]
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    old_month_end = calendar.monthrange(value.year, value.month)[1]
    new_month_end = calendar.monthrange(year, month)[1]
    day = new_month_end if value.day == old_month_end else min(value.day, new_month_end)
    return date(year, month, day)


def _validate_scope(
    scenario: FinancialScenario,
    forecast_plan: ForecastPathPlan,
    planning_plan: PlanningEvaluationPlan,
) -> date:
    if (
        scenario.user_profile_id != forecast_plan.user_profile_id
        or scenario.account_id != forecast_plan.account_id
        or planning_plan.user_profile_id != forecast_plan.user_profile_id
        or planning_plan.account_ids != (forecast_plan.account_id,)
        or planning_plan.as_of_date != forecast_plan.forecast_start - timedelta(days=1)
    ):
        raise ScenarioPlanningError(
            ScenarioPlanningErrorCode.PLAN_SCOPE_MISMATCH,
            "scenario, forecast, and planning scopes must describe one aligned account",
        )
    horizon_end = forecast_plan.forecast_start + timedelta(
        days=forecast_plan.horizon_days - 1
    )
    if (
        scenario.start_date < forecast_plan.forecast_start
        or scenario.start_date > horizon_end
        or (scenario.end_date is not None and scenario.end_date > horizon_end)
    ):
        raise ScenarioPlanningError(
            ScenarioPlanningErrorCode.SCENARIO_OUTSIDE_HORIZON,
            "scenario dates must fall inside the forecast horizon",
        )
    return horizon_end


def _validate_category(
    factory: sessionmaker[Session], scenario: FinancialScenario
) -> None:
    if scenario.category_id is None:
        return
    with session_scope(factory) as session:
        category = PlanningRepository(session).get_category(scenario.category_id)
        if category is None:
            raise ScenarioPlanningError(
                ScenarioPlanningErrorCode.CATEGORY_NOT_FOUND,
                "scenario category does not exist",
            )
        if not category.is_active:
            raise ScenarioPlanningError(
                ScenarioPlanningErrorCode.CATEGORY_INACTIVE,
                "scenario category is inactive",
            )


def _new_adjustment(
    scenario: FinancialScenario, *, index: int, adjustment_date: date, amount: Decimal
) -> ForecastScenarioAdjustment:
    return ForecastScenarioAdjustment(
        adjustment_id=f"{scenario.scenario_id}-change-{index}",
        adjustment_date=adjustment_date,
        kind=(
            ScenarioAdjustmentKind.INFLOW
            if amount > 0
            else ScenarioAdjustmentKind.OUTFLOW
        ),
        amount=_money(amount),
    )


def _compile_overlay(
    scenario: FinancialScenario,
    *,
    baseline: BalanceForecastPath,
    horizon_end: date,
) -> ForecastScenario:
    if scenario.scenario_type is FinancialScenarioType.CANCELLED_SUBSCRIPTION:
        matching = tuple(
            item
            for item in baseline.recurring_occurrences
            if item.candidate_id == scenario.recurring_payment_id
            and item.financial_role is FinancialRole.EXPENSE
            and item.signed_amount < 0
            and scenario.start_date <= item.occurrence_date
            and (scenario.end_date is None or item.occurrence_date <= scenario.end_date)
        )
        if not matching:
            raise ScenarioPlanningError(
                ScenarioPlanningErrorCode.RECURRING_PAYMENT_NOT_FOUND,
                "no confirmed recurring occurrence matches this cancellation",
            )
        adjustments = tuple(
            _new_adjustment(
                scenario,
                index=index,
                adjustment_date=item.occurrence_date,
                amount=-item.signed_amount,
            )
            for index, item in enumerate(matching, start=1)
        )
    else:
        assert scenario.amount is not None
        signed_amount = (
            -scenario.amount
            if scenario.scenario_type in _OUTFLOW_TYPES
            else scenario.amount
        )
        dates: tuple[date, ...]
        if scenario.frequency is None:
            dates = (scenario.start_date,)
        else:
            end_date = scenario.end_date or horizon_end
            generated: list[date] = []
            occurrence = scenario.start_date
            while occurrence <= end_date:
                generated.append(occurrence)
                occurrence = _advance(occurrence, scenario.frequency)
            dates = tuple(generated)
        adjustments = tuple(
            _new_adjustment(
                scenario,
                index=index,
                adjustment_date=adjustment_date,
                amount=signed_amount,
            )
            for index, adjustment_date in enumerate(dates, start=1)
        )
    return ForecastScenario(
        scenario_id=scenario.scenario_id,
        adjustments=adjustments,
    )


def _budget_delta(
    scenario: FinancialScenario,
    overlay: ForecastScenario,
    progress: BudgetProgress,
) -> Decimal:
    budget = progress.budget
    if budget.budget_type is BudgetType.MONTHLY_CATEGORY:
        if (
            scenario.scenario_type
            not in _EXPENSE_INCREASE_TYPES | _EXPENSE_REDUCTION_TYPES
            or scenario.category_id != budget.category_id
        ):
            return _ZERO
    elif scenario.scenario_type not in _NON_RECURRING_BUDGET_TYPES:
        return _ZERO
    relevant = (
        item.amount
        for item in overlay.adjustments
        if budget.period.start_date <= item.adjustment_date <= budget.period.end_date
    )
    return _money(-sum(relevant, start=_ZERO))


def _budget_effects(
    scenario: FinancialScenario,
    overlay: ForecastScenario,
    baseline: FinancialPlanningResult,
) -> tuple[ScenarioBudgetEffect, ...]:
    effects: list[ScenarioBudgetEffect] = []
    for progress in baseline.budgets:
        projected = progress.projected_use
        if projected is None:
            scenario_use = None
            difference = None
            scenario_overrun = None
        else:
            scenario_use = max(
                _ZERO,
                _money(projected + _budget_delta(scenario, overlay, progress)),
            )
            difference = _money(scenario_use - projected)
            scenario_overrun = max(
                _ZERO, _money(scenario_use - progress.budget.amount_limit)
            )
        effects.append(
            ScenarioBudgetEffect(
                budget_id=progress.budget.budget_id,
                coverage_status=progress.coverage.status,
                budget_limit=progress.budget.amount_limit,
                baseline_projected_use=projected,
                scenario_projected_use=scenario_use,
                projected_use_difference=difference,
                baseline_projected_overrun=progress.projected_overrun,
                scenario_projected_overrun=scenario_overrun,
            )
        )
    return tuple(effects)


def _savings_at_risk(result: FinancialPlanningResult) -> bool:
    return any(
        item.code is PlanningWarningCode.SAVINGS_CONTRIBUTION_SHORTFALL
        for item in result.warnings
    )


def _goal_effect(
    baseline: FinancialGoalProgress,
    scenario: FinancialGoalProgress,
    *,
    baseline_savings_at_risk: bool,
    scenario_savings_at_risk: bool,
) -> ScenarioGoalEffect:
    if baseline.goal.goal_type is FinancialGoalType.MINIMUM_BALANCE:
        assert baseline.projected_shortfall is not None
        assert scenario.projected_shortfall is not None
        return ScenarioGoalEffect(
            goal_id=baseline.goal.goal_id,
            goal_type=baseline.goal.goal_type,
            required_monthly_contribution=None,
            baseline_projected_shortfall=baseline.projected_shortfall,
            scenario_projected_shortfall=scenario.projected_shortfall,
            projected_shortfall_difference=_money(
                scenario.projected_shortfall - baseline.projected_shortfall
            ),
            baseline_at_risk=baseline.projected_shortfall > 0,
            scenario_at_risk=scenario.projected_shortfall > 0,
        )
    return ScenarioGoalEffect(
        goal_id=baseline.goal.goal_id,
        goal_type=baseline.goal.goal_type,
        required_monthly_contribution=baseline.required_monthly_contribution,
        baseline_projected_shortfall=None,
        scenario_projected_shortfall=None,
        projected_shortfall_difference=None,
        baseline_at_risk=baseline_savings_at_risk,
        scenario_at_risk=scenario_savings_at_risk,
    )


def _validate_uncertainty_inheritance(
    baseline: BalanceForecastPath, scenario: BalanceForecastPath
) -> None:
    aligned = (
        baseline.selected_model == scenario.selected_model
        and baseline.interval_method is scenario.interval_method
        and baseline.widening_multiplier == scenario.widening_multiplier
        and baseline.warnings == scenario.warnings
        and baseline.freshness_warnings == scenario.freshness_warnings
        and baseline.interval_performance == scenario.interval_performance
        and baseline.recurring_occurrences == scenario.recurring_occurrences
        and all(
            baseline_day.upper_balance - baseline_day.lower_balance
            == scenario_day.upper_balance - scenario_day.lower_balance
            for baseline_day, scenario_day in zip(
                baseline.daily_balances, scenario.daily_balances, strict=True
            )
        )
    )
    if not aligned:
        raise ScenarioPlanningError(
            ScenarioPlanningErrorCode.UNCERTAINTY_INHERITANCE_FAILED,
            "scenario path did not preserve baseline uncertainty evidence",
        )


def evaluate_financial_scenario(
    factory: sessionmaker[Session],
    *,
    dataset: ForecastDataset,
    trained: TrainedPrimaryForecaster,
    forecast_plan: ForecastPathPlan,
    planning_plan: PlanningEvaluationPlan,
    scenario: FinancialScenario,
) -> FinancialScenarioComparison:
    """Compare a read-only baseline with one isolated hypothetical overlay."""
    horizon_end = _validate_scope(scenario, forecast_plan, planning_plan)
    _validate_category(factory, scenario)
    baseline_forecast = build_balance_forecast_path(
        factory,
        dataset=dataset,
        trained=trained,
        plan=forecast_plan,
    )
    overlay = _compile_overlay(
        scenario,
        baseline=baseline_forecast,
        horizon_end=horizon_end,
    )
    scenario_forecast = build_balance_forecast_path(
        factory,
        dataset=dataset,
        trained=trained,
        plan=forecast_plan,
        scenario=overlay,
    )
    _validate_uncertainty_inheritance(baseline_forecast, scenario_forecast)
    baseline_plan = evaluate_financial_plan(
        factory,
        plan=planning_plan,
        balance_projections=(projection_from_balance_forecast(baseline_forecast),),
    )
    scenario_plan = evaluate_financial_plan(
        factory,
        plan=planning_plan,
        balance_projections=(projection_from_balance_forecast(scenario_forecast),),
    )
    baseline_low = min(item.lower_balance for item in baseline_forecast.daily_balances)
    scenario_low = min(item.lower_balance for item in scenario_forecast.daily_balances)
    baseline_goals = {item.goal.goal_id: item for item in baseline_plan.goals}
    scenario_goals = {item.goal.goal_id: item for item in scenario_plan.goals}
    goal_effects = tuple(
        _goal_effect(
            baseline_goals[goal_id],
            scenario_goals[goal_id],
            baseline_savings_at_risk=_savings_at_risk(baseline_plan),
            scenario_savings_at_risk=_savings_at_risk(scenario_plan),
        )
        for goal_id in baseline_goals
    )
    warnings: list[ScenarioComparisonWarningCode] = []
    if baseline_forecast.warnings:
        warnings.append(ScenarioComparisonWarningCode.BASELINE_FORECAST_LIMITATION)
    if any(
        item.coverage.status is not AnalyticsCoverageStatus.COMPLETE
        for item in baseline_plan.budgets
    ):
        warnings.append(ScenarioComparisonWarningCode.INCOMPLETE_BASELINE_COVERAGE)
    return FinancialScenarioComparison(
        scenario=scenario,
        overlay=overlay,
        baseline_forecast=baseline_forecast,
        scenario_forecast=scenario_forecast,
        baseline_plan=baseline_plan,
        scenario_plan=scenario_plan,
        balance_effect=ScenarioBalanceEffect(
            currency=baseline_forecast.opening_balance.currency,
            baseline_end_balance=baseline_forecast.expected_final_balance,
            scenario_end_balance=scenario_forecast.expected_final_balance,
            end_balance_difference=_money(
                scenario_forecast.expected_final_balance
                - baseline_forecast.expected_final_balance
            ),
            baseline_lowest_lower_balance=baseline_low,
            scenario_lowest_lower_balance=scenario_low,
            lowest_balance_difference=_money(scenario_low - baseline_low),
        ),
        budget_effects=_budget_effects(scenario, overlay, baseline_plan),
        goal_effects=goal_effects,
        safe_spending_effect=ScenarioSafeSpendingEffect(
            currency=baseline_plan.currency,
            baseline_safe_weekly_spending=(
                baseline_plan.safe_spending.safe_weekly_spending
            ),
            scenario_safe_weekly_spending=(
                scenario_plan.safe_spending.safe_weekly_spending
            ),
            difference=_money(
                scenario_plan.safe_spending.safe_weekly_spending
                - baseline_plan.safe_spending.safe_weekly_spending
            ),
        ),
        uncertainty=ScenarioUncertainty(
            inherited=True,
            interval_method=baseline_forecast.interval_method,
            interval_probability=forecast_plan.policy.interval_probability,
            widening_multiplier=baseline_forecast.widening_multiplier,
            forecast_warnings=baseline_forecast.warnings,
        ),
        warnings=tuple(warnings),
    )


__all__ = [
    "ScenarioPlanningError",
    "ScenarioPlanningErrorCode",
    "evaluate_financial_scenario",
]
