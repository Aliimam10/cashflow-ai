"""Coverage-aware budgets, goals, and deterministic safe-spending calculations."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.analytics import compute_cash_flow_analytics
from cashflow_ai.persistence.base import utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import BudgetRecord, SavingsGoalRecord
from cashflow_ai.persistence.repositories import (
    AccountRepository,
    PlanningRepository,
    UserProfileRepository,
)
from cashflow_ai.schemas.analytics import (
    AnalyticsCoverageStatus,
    AnalyticsScope,
    AnalyticsView,
    CashFlowAnalytics,
)
from cashflow_ai.schemas.money import MONEY_QUANTUM
from cashflow_ai.schemas.planning import (
    Budget,
    BudgetCreate,
    BudgetProgress,
    BudgetType,
    FinancialGoal,
    FinancialGoalCreate,
    FinancialGoalProgress,
    FinancialGoalType,
    FinancialPlanningResult,
    PlanningBalanceProjection,
    PlanningEvaluationPlan,
    PlanningWarning,
    PlanningWarningCode,
    SafeSpendingLimitingFactor,
    SafeWeeklySpending,
)
from cashflow_ai.schemas.statements import DateRange
from cashflow_ai.schemas.transactions import Currency

_ZERO = Decimal("0.00")
_WEEKS_PER_YEAR = Decimal("52")
_MONTHS_PER_YEAR = Decimal("12")


class PlanningServiceErrorCode(StrEnum):
    """Stable failures that disclose no private financial values."""

    PROFILE_NOT_FOUND = "profile_not_found"
    ACCOUNT_NOT_FOUND = "account_not_found"
    ACCOUNT_INACTIVE = "account_inactive"
    CATEGORY_NOT_FOUND = "category_not_found"
    CATEGORY_INACTIVE = "category_inactive"
    CURRENCY_MISMATCH = "currency_mismatch"
    DUPLICATE_BUDGET = "duplicate_budget"
    DUPLICATE_GOAL = "duplicate_goal"
    PROJECTION_SCOPE_MISMATCH = "projection_scope_mismatch"
    PROJECTION_PERIOD_MISMATCH = "projection_period_mismatch"


class PlanningServiceError(ValueError):
    """Controlled planning failure with a stable privacy-safe code."""

    def __init__(self, code: PlanningServiceErrorCode, message: str) -> None:
        """Store a safe error code and message."""
        super().__init__(message)
        self.code = code


def _budget_contract(record: BudgetRecord) -> Budget:
    return Budget(
        budget_id=record.id,
        user_profile_id=record.user_profile_id,
        budget_type=BudgetType(record.budget_type),
        category_id=record.category_id,
        period=DateRange(
            start_date=record.period_start,
            end_date=record.period_end,
        ),
        amount_limit=record.amount_limit,
        currency=Currency(record.currency),
    )


def _goal_contract(record: SavingsGoalRecord, *, user_profile_id: str) -> FinancialGoal:
    return FinancialGoal(
        goal_id=record.id,
        user_profile_id=user_profile_id,
        account_id=record.account_id,
        goal_type=FinancialGoalType(record.goal_type),
        name=record.name,
        target_amount=record.target_amount,
        current_amount=record.current_amount,
        target_date=record.target_date,
        created_at=record.created_at,
    )


def create_budget(
    factory: sessionmaker[Session],
    *,
    request: BudgetCreate,
) -> Budget:
    """Persist one validated category or weekly budget without replacing another."""
    try:
        with session_scope(factory) as session:
            if UserProfileRepository(session).get(request.user_profile_id) is None:
                raise PlanningServiceError(
                    PlanningServiceErrorCode.PROFILE_NOT_FOUND,
                    "local user profile does not exist",
                )
            if request.budget_type is BudgetType.MONTHLY_CATEGORY:
                assert request.category_id is not None
                category = PlanningRepository(session).get_category(request.category_id)
                if category is None:
                    raise PlanningServiceError(
                        PlanningServiceErrorCode.CATEGORY_NOT_FOUND,
                        "budget category does not exist",
                    )
                if not category.is_active:
                    raise PlanningServiceError(
                        PlanningServiceErrorCode.CATEGORY_INACTIVE,
                        "budget category is inactive",
                    )
            record = PlanningRepository(session).add_budget(
                BudgetRecord(
                    user_profile_id=request.user_profile_id,
                    budget_type=request.budget_type.value,
                    category_id=request.category_id,
                    period_start=request.period.start_date,
                    period_end=request.period.end_date,
                    amount_limit=request.amount_limit,
                    currency=request.currency.value,
                )
            )
            return _budget_contract(record)
    except IntegrityError as exc:
        raise PlanningServiceError(
            PlanningServiceErrorCode.DUPLICATE_BUDGET,
            "a budget already exists for this scope and period",
        ) from exc


def create_financial_goal(
    factory: sessionmaker[Session],
    *,
    request: FinancialGoalCreate,
) -> FinancialGoal:
    """Persist one validated savings target or minimum-balance goal."""
    try:
        with session_scope(factory) as session:
            if UserProfileRepository(session).get(request.user_profile_id) is None:
                raise PlanningServiceError(
                    PlanningServiceErrorCode.PROFILE_NOT_FOUND,
                    "local user profile does not exist",
                )
            account = AccountRepository(session).get(request.account_id)
            if account is None or account.user_profile_id != request.user_profile_id:
                raise PlanningServiceError(
                    PlanningServiceErrorCode.ACCOUNT_NOT_FOUND,
                    "planning account does not exist for this profile",
                )
            if not account.is_active:
                raise PlanningServiceError(
                    PlanningServiceErrorCode.ACCOUNT_INACTIVE,
                    "planning account is inactive",
                )
            if account.currency != Currency.GBP.value:
                raise PlanningServiceError(
                    PlanningServiceErrorCode.CURRENCY_MISMATCH,
                    "planning account must use the supported currency",
                )
            record = PlanningRepository(session).add_goal(
                SavingsGoalRecord(
                    account_id=request.account_id,
                    goal_type=request.goal_type.value,
                    name=request.name,
                    target_amount=request.target_amount,
                    current_amount=request.current_amount,
                    target_date=request.target_date,
                    created_at=utc_now(),
                )
            )
            return _goal_contract(record, user_profile_id=request.user_profile_id)
    except IntegrityError as exc:
        raise PlanningServiceError(
            PlanningServiceErrorCode.DUPLICATE_GOAL,
            "a conflicting financial goal already exists",
        ) from exc


def list_budgets(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
    as_of_date: date,
) -> tuple[Budget, ...]:
    """Return budgets active on one date for an existing local profile."""
    with session_scope(factory) as session:
        if UserProfileRepository(session).get(user_profile_id) is None:
            raise PlanningServiceError(
                PlanningServiceErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        return tuple(
            _budget_contract(record)
            for record in PlanningRepository(session).list_budgets_on(
                user_profile_id=user_profile_id,
                as_of_date=as_of_date,
            )
        )


def list_financial_goals(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
) -> tuple[FinancialGoal, ...]:
    """Return every financial goal owned by one existing local profile."""
    with session_scope(factory) as session:
        if UserProfileRepository(session).get(user_profile_id) is None:
            raise PlanningServiceError(
                PlanningServiceErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        return tuple(
            _goal_contract(record, user_profile_id=account.user_profile_id)
            for record, account in PlanningRepository(session).list_goals_for_profile(
                user_profile_id
            )
        )


def _money(value: Decimal, *, rounding: str = ROUND_HALF_UP) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=rounding)


def _analytics_for_budget(
    factory: sessionmaker[Session],
    *,
    plan: PlanningEvaluationPlan,
    budget: Budget,
) -> CashFlowAnalytics:
    return compute_cash_flow_analytics(
        factory,
        AnalyticsScope(
            user_profile_id=plan.user_profile_id,
            account_ids=plan.account_ids,
            period=DateRange(
                start_date=budget.period.start_date,
                end_date=plan.as_of_date,
            ),
            view=(
                AnalyticsView.ACCOUNT
                if len(plan.account_ids) == 1
                else AnalyticsView.CONSOLIDATED
            ),
        ),
    )


def _amount_used(budget: Budget, analytics: CashFlowAnalytics) -> Decimal | None:
    if budget.budget_type is BudgetType.MONTHLY_CATEGORY:
        if analytics.category_spending is None:
            return None
        return next(
            (
                item.amount
                for item in analytics.category_spending
                if item.category_id == budget.category_id
            ),
            _ZERO,
        )
    if analytics.spending_cadence is None:
        return None
    return (
        analytics.spending_cadence.discretionary
        + analytics.spending_cadence.unclassified
    )


def _budget_progress(
    factory: sessionmaker[Session],
    *,
    plan: PlanningEvaluationPlan,
    budget: Budget,
) -> BudgetProgress:
    analytics = _analytics_for_budget(factory, plan=plan, budget=budget)
    amount_used = _amount_used(budget, analytics)
    if amount_used is None:
        return BudgetProgress(
            budget=budget,
            observation_period=analytics.coverage.requested_period,
            coverage=analytics.coverage,
            amount_used=None,
            amount_remaining=None,
            projected_use=None,
            projected_overrun=None,
        )
    remaining = max(_ZERO, budget.amount_limit - amount_used)
    projected: Decimal | None = None
    overrun: Decimal | None = None
    if analytics.coverage.status is AnalyticsCoverageStatus.COMPLETE:
        elapsed_days = (plan.as_of_date - budget.period.start_date).days + 1
        total_days = (budget.period.end_date - budget.period.start_date).days + 1
        projected = _money(amount_used * total_days / elapsed_days)
        overrun = max(_ZERO, projected - budget.amount_limit)
    return BudgetProgress(
        budget=budget,
        observation_period=analytics.coverage.requested_period,
        coverage=analytics.coverage,
        amount_used=amount_used,
        amount_remaining=remaining,
        projected_use=projected,
        projected_overrun=overrun,
    )


def _contribution_months(as_of_date: date, target_date: date) -> int:
    if target_date < as_of_date:
        return 0
    return (
        (target_date.year - as_of_date.year) * 12
        + target_date.month
        - as_of_date.month
        + 1
    )


def _goal_progress(
    goal: FinancialGoal,
    *,
    as_of_date: date,
    projection: PlanningBalanceProjection,
) -> tuple[FinancialGoalProgress, PlanningWarning | None]:
    remaining = max(_ZERO, goal.target_amount - goal.current_amount)
    if goal.goal_type is FinancialGoalType.MINIMUM_BALANCE:
        shortfall = max(_ZERO, goal.target_amount - projection.lowest_lower_balance)
        return (
            FinancialGoalProgress(
                goal=goal,
                remaining_amount=_ZERO,
                forecast_lowest_balance=projection.lowest_lower_balance,
                projected_shortfall=shortfall,
            ),
            (
                PlanningWarning(
                    code=PlanningWarningCode.MINIMUM_BALANCE_SHORTFALL,
                    amount=shortfall,
                    goal_id=goal.goal_id,
                )
                if shortfall > 0
                else None
            ),
        )
    if goal.target_date is None:
        months = 0
        warning: PlanningWarning | None = PlanningWarning(
            code=PlanningWarningCode.MISSING_SAVINGS_TARGET_DATE,
            amount=remaining,
            goal_id=goal.goal_id,
        )
    else:
        months = _contribution_months(as_of_date, goal.target_date)
        warning = (
            PlanningWarning(
                code=PlanningWarningCode.OVERDUE_SAVINGS_TARGET,
                amount=remaining,
                goal_id=goal.goal_id,
            )
            if months == 0 and remaining > 0
            else None
        )
    contribution = (
        remaining if months == 0 else _money(remaining / months, rounding=ROUND_CEILING)
    )
    return (
        FinancialGoalProgress(
            goal=goal,
            remaining_amount=remaining,
            contribution_months=months,
            required_monthly_contribution=contribution,
        ),
        warning,
    )


def _validate_projection_scope(
    plan: PlanningEvaluationPlan,
    projections: tuple[PlanningBalanceProjection, ...],
) -> None:
    if tuple(item.account_id for item in projections) != plan.account_ids:
        raise PlanningServiceError(
            PlanningServiceErrorCode.PROJECTION_SCOPE_MISMATCH,
            "balance projections do not match the selected planning accounts",
        )
    first = projections[0]
    if first.period.start_date <= plan.as_of_date or any(
        item.period != first.period or item.currency is not first.currency
        for item in projections
    ):
        raise PlanningServiceError(
            PlanningServiceErrorCode.PROJECTION_PERIOD_MISMATCH,
            "balance projections must share one future period and currency",
        )


def _safe_spending(
    *,
    projections: tuple[PlanningBalanceProjection, ...],
    goals: tuple[FinancialGoalProgress, ...],
    weekly_budget: Budget | None,
) -> SafeWeeklySpending:
    period = projections[0].period
    forecast_weeks = Decimal((period.end_date - period.start_date).days + 1) / Decimal(
        "7"
    )
    expected_weekly = _money(
        sum(
            (item.expected_discretionary_spending for item in projections),
            start=_ZERO,
        )
        / forecast_weeks
    )
    floors = {
        item.goal.account_id: item.goal.target_amount
        for item in goals
        if item.goal.goal_type is FinancialGoalType.MINIMUM_BALANCE
    }
    headroom = sum(
        (
            item.lowest_lower_balance - floors.get(item.account_id, _ZERO)
            for item in projections
        ),
        start=_ZERO,
    )
    monthly_savings = sum(
        (
            item.required_monthly_contribution or _ZERO
            for item in goals
            if item.goal.goal_type is FinancialGoalType.SAVINGS_TARGET
        ),
        start=_ZERO,
    )
    weekly_savings = _money(
        monthly_savings * _MONTHS_PER_YEAR / _WEEKS_PER_YEAR,
        rounding=ROUND_CEILING,
    )
    cash_before_savings = expected_weekly + headroom / forecast_weeks
    cash_limit = _money(
        max(_ZERO, cash_before_savings - weekly_savings),
        rounding=ROUND_FLOOR,
    )
    budget_limit = None if weekly_budget is None else weekly_budget.amount_limit
    safe = cash_limit if budget_limit is None else min(cash_limit, budget_limit)
    if cash_limit == 0:
        limiting_factor = SafeSpendingLimitingFactor.NO_HEADROOM
    elif budget_limit is None or cash_limit < budget_limit:
        limiting_factor = SafeSpendingLimitingFactor.CASH_HEADROOM
    elif budget_limit < cash_limit:
        limiting_factor = SafeSpendingLimitingFactor.WEEKLY_BUDGET
    else:
        limiting_factor = SafeSpendingLimitingFactor.CASH_AND_BUDGET
    return SafeWeeklySpending(
        currency=projections[0].currency,
        projection_period=period,
        forecast_weeks=forecast_weeks,
        expected_forecast_weekly_spending=expected_weekly,
        lower_balance_headroom=_money(headroom),
        required_weekly_savings=weekly_savings,
        cash_based_weekly_limit=cash_limit,
        weekly_budget_limit=budget_limit,
        safe_weekly_spending=safe,
        limiting_factor=limiting_factor,
    )


def evaluate_financial_plan(
    factory: sessionmaker[Session],
    *,
    plan: PlanningEvaluationPlan,
    balance_projections: tuple[PlanningBalanceProjection, ...],
) -> FinancialPlanningResult:
    """Calculate budget progress, goal needs, and conservative weekly spending."""
    _validate_projection_scope(plan, balance_projections)
    with session_scope(factory) as session:
        accounts = tuple(
            AccountRepository(session).get(item) for item in plan.account_ids
        )
        if any(
            account is None or account.user_profile_id != plan.user_profile_id
            for account in accounts
        ):
            raise PlanningServiceError(
                PlanningServiceErrorCode.ACCOUNT_NOT_FOUND,
                "one or more planning accounts are unavailable to this profile",
            )
        if any(not account.is_active for account in accounts if account is not None):
            raise PlanningServiceError(
                PlanningServiceErrorCode.ACCOUNT_INACTIVE,
                "one or more planning accounts are inactive",
            )
        currencies = {account.currency for account in accounts if account is not None}
        if currencies != {Currency.GBP.value} or any(
            item.currency.value not in currencies for item in balance_projections
        ):
            raise PlanningServiceError(
                PlanningServiceErrorCode.CURRENCY_MISMATCH,
                "planning accounts and projections must use the supported currency",
            )
        repository = PlanningRepository(session)
        budget_records = repository.list_budgets_on(
            user_profile_id=plan.user_profile_id,
            as_of_date=plan.as_of_date,
        )
        goal_rows = repository.list_goals_for_accounts(plan.account_ids)
        if any(
            account.user_profile_id != plan.user_profile_id
            for _goal, account in goal_rows
        ):
            raise PlanningServiceError(
                PlanningServiceErrorCode.ACCOUNT_NOT_FOUND,
                "one or more goals are outside the selected profile",
            )
        first_week_end = balance_projections[0].period.start_date + timedelta(days=6)
        weekly_record = repository.get_weekly_budget(
            user_profile_id=plan.user_profile_id,
            period_start=balance_projections[0].period.start_date,
            period_end=first_week_end,
        )

    budgets = tuple(_budget_contract(record) for record in budget_records)
    budget_progress = tuple(
        _budget_progress(factory, plan=plan, budget=budget) for budget in budgets
    )
    projections_by_account = {item.account_id: item for item in balance_projections}
    goals = tuple(
        _goal_contract(record, user_profile_id=account.user_profile_id)
        for record, account in goal_rows
    )
    goal_results = tuple(
        _goal_progress(
            goal,
            as_of_date=plan.as_of_date,
            projection=projections_by_account[goal.account_id],
        )
        for goal in goals
    )
    goal_progress = tuple(item[0] for item in goal_results)
    weekly_budget = None if weekly_record is None else _budget_contract(weekly_record)
    safe_spending = _safe_spending(
        projections=balance_projections,
        goals=goal_progress,
        weekly_budget=weekly_budget,
    )

    warnings = [item[1] for item in goal_results if item[1] is not None]
    for item in budget_progress:
        if item.coverage.status is not AnalyticsCoverageStatus.COMPLETE:
            warnings.append(
                PlanningWarning(
                    code=PlanningWarningCode.INCOMPLETE_TRANSACTION_COVERAGE,
                    budget_id=item.budget.budget_id,
                )
            )
        elif item.projected_overrun and item.projected_overrun > 0:
            warnings.append(
                PlanningWarning(
                    code=(
                        PlanningWarningCode.PROJECTED_CATEGORY_BUDGET_SHORTFALL
                        if item.budget.budget_type is BudgetType.MONTHLY_CATEGORY
                        else PlanningWarningCode.PROJECTED_WEEKLY_BUDGET_SHORTFALL
                    ),
                    amount=item.projected_overrun,
                    budget_id=item.budget.budget_id,
                )
            )
    cash_before_savings = (
        safe_spending.expected_forecast_weekly_spending
        + safe_spending.lower_balance_headroom / safe_spending.forecast_weeks
    )
    if safe_spending.required_weekly_savings > max(_ZERO, cash_before_savings):
        warnings.append(
            PlanningWarning(
                code=PlanningWarningCode.SAVINGS_CONTRIBUTION_SHORTFALL,
                amount=_money(
                    safe_spending.required_weekly_savings
                    - max(_ZERO, cash_before_savings),
                    rounding=ROUND_CEILING,
                ),
            )
        )
    if any(item.forecast_warnings for item in balance_projections):
        warnings.append(PlanningWarning(code=PlanningWarningCode.FORECAST_LIMITATION))

    return FinancialPlanningResult(
        plan=plan,
        currency=Currency.GBP,
        balance_projections=balance_projections,
        budgets=budget_progress,
        goals=goal_progress,
        safe_spending=safe_spending,
        warnings=tuple(warnings),
    )


__all__ = [
    "PlanningServiceError",
    "PlanningServiceErrorCode",
    "create_budget",
    "create_financial_goal",
    "evaluate_financial_plan",
    "list_budgets",
    "list_financial_goals",
]
