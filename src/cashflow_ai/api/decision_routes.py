"""Thin HTTP routes for analytics, reviews, ML, and financial planning."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, status

from cashflow_ai.api.decision_services import (
    active_model,
    calculate_anomalies,
    calculate_balance_forecast,
    calculate_cash_flow,
    calculate_coverage,
    calculate_financial_data_freshness,
    calculate_financial_plan,
    calculate_scenario,
    confirm_role_suggestion,
    correct_category,
    create_planning_budget,
    create_planning_goal,
    evaluate_forecast_model,
    financial_revision,
    page_budgets,
    page_categories,
    page_category_reviews,
    page_derived_freshness,
    page_financial_goals,
    page_financial_role_audits,
    page_financial_role_reviews,
    page_models,
    refresh_recurring_payments,
    reject_role_suggestion,
    review_recurrence,
    review_transaction_role,
    suggest_financial_roles,
)
from cashflow_ai.api.dependencies import PaginationDependency, SessionFactoryDependency
from cashflow_ai.api.routes import ERROR_RESPONSES
from cashflow_ai.schemas.analytics import (
    AnalyticsScope,
    CashFlowAnalytics,
    DataCoverageIndicator,
)
from cashflow_ai.schemas.anomalies import AnomalyDetectionPlan, AnomalyDetectionResult
from cashflow_ai.schemas.api import Page
from cashflow_ai.schemas.api_decisions import (
    BalanceForecastRequest,
    FinancialDataFreshnessRequest,
    FinancialRoleSuggestionRequest,
    ForecastEvaluationRequest,
    PlanningApiRequest,
    RecurrenceDetectionRequest,
    RoleDecisionRequest,
    ScenarioApiRequest,
    TransactionRoleReviewRequest,
)
from cashflow_ai.schemas.categories import CategorySummary
from cashflow_ai.schemas.financial_roles import (
    FinancialRoleAudit,
    FinancialRoleSuggestion,
    RoleDecisionResult,
    RoleReviewItem,
)
from cashflow_ai.schemas.forecast_models import ForecastTrainingResult
from cashflow_ai.schemas.forecast_paths import BalanceForecastPath
from cashflow_ai.schemas.freshness import FinancialDataFreshness
from cashflow_ai.schemas.hybrid_categorisation import (
    CategoryFeedback,
    CategoryFeedbackResult,
    LowConfidenceReviewItem,
)
from cashflow_ai.schemas.invalidation import (
    DerivedResultFreshness,
    FinancialDataRevision,
)
from cashflow_ai.schemas.model_registry import ModelTask, RegisteredModel
from cashflow_ai.schemas.planning import (
    Budget,
    BudgetCreate,
    FinancialGoal,
    FinancialGoalCreate,
    FinancialPlanningResult,
)
from cashflow_ai.schemas.recurrence import (
    RecurrenceReview,
    RecurrenceReviewResult,
    RecurringPaymentCandidate,
)
from cashflow_ai.schemas.scenarios import FinancialScenarioComparison

router = APIRouter(prefix="/api/v1", responses=ERROR_RESPONSES)


@router.post(
    "/analytics/cash-flow",
    response_model=CashFlowAnalytics,
    tags=["analytics"],
    summary="Calculate coverage-aware cash flow",
)
def cash_flow_route(
    request: AnalyticsScope, factory: SessionFactoryDependency
) -> CashFlowAnalytics:
    """Return role-aware analytics from verified, explicitly covered data."""
    return calculate_cash_flow(factory, request)


@router.post(
    "/analytics/coverage",
    response_model=DataCoverageIndicator,
    tags=["analytics"],
    summary="Calculate statement coverage",
)
def coverage_route(
    request: AnalyticsScope, factory: SessionFactoryDependency
) -> DataCoverageIndicator:
    """Return known, partial, and missing periods for an analytics scope."""
    return calculate_coverage(factory, request)


@router.post(
    "/coverage/freshness",
    response_model=FinancialDataFreshness,
    tags=["analytics"],
    summary="Assess financial data freshness",
)
def financial_data_freshness_route(
    request: FinancialDataFreshnessRequest,
    factory: SessionFactoryDependency,
) -> FinancialDataFreshness:
    """Apply caller-visible age and continuity limits to verified evidence."""
    return calculate_financial_data_freshness(factory, request)


@router.get(
    "/accounts/{account_id}/financial-revision",
    response_model=FinancialDataRevision,
    tags=["analytics"],
    summary="Get the account source-data revision",
)
def financial_revision_route(
    account_id: str, factory: SessionFactoryDependency
) -> FinancialDataRevision:
    """Return data-minimised source-change metadata."""
    return financial_revision(factory, account_id=account_id)


@router.get(
    "/accounts/{account_id}/derived-freshness",
    response_model=Page[DerivedResultFreshness],
    tags=["analytics"],
    summary="List derived-result freshness",
)
def derived_freshness_route(
    account_id: str,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[DerivedResultFreshness]:
    """Return current, stale, or unavailable states in a bounded page."""
    return page_derived_freshness(factory, account_id=account_id, pagination=pagination)


@router.post(
    "/recurring/detect",
    response_model=Page[RecurringPaymentCandidate],
    tags=["recurring"],
    summary="Detect recurring payments",
)
def detect_recurring_route(
    request: RecurrenceDetectionRequest,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[RecurringPaymentCandidate]:
    """Detect point-in-time candidates without inferring user confirmation."""
    return refresh_recurring_payments(factory, request=request, pagination=pagination)


@router.post(
    "/recurring/reviews",
    response_model=RecurrenceReviewResult,
    tags=["recurring"],
    summary="Review a recurring candidate",
)
def review_recurring_route(
    request: RecurrenceReview, factory: SessionFactoryDependency
) -> RecurrenceReviewResult:
    """Confirm or cancel one candidate through the recurrence service."""
    return review_recurrence(factory, request)


@router.get(
    "/categories",
    response_model=Page[CategorySummary],
    tags=["categorisation"],
    summary="List transaction categories",
)
def categories_route(
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[CategorySummary]:
    """Return the persisted taxonomy without financial rows."""
    return page_categories(factory, pagination)


@router.get(
    "/profiles/{profile_id}/categorisation/reviews",
    response_model=Page[LowConfidenceReviewItem],
    tags=["categorisation"],
    summary="List low-confidence category reviews",
)
def category_reviews_route(
    profile_id: str,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[LowConfidenceReviewItem]:
    """Return pending ML decisions without transaction description text."""
    return page_category_reviews(
        factory,
        user_profile_id=profile_id,
        pagination=pagination,
    )


@router.post(
    "/categorisation/feedback",
    response_model=CategoryFeedbackResult,
    tags=["categorisation"],
    summary="Correct a transaction category",
)
def category_feedback_route(
    request: CategoryFeedback, factory: SessionFactoryDependency
) -> CategoryFeedbackResult:
    """Apply an explicit correction and only a user-requested personal rule."""
    return correct_category(factory, request)


@router.post(
    "/financial-roles/suggestions",
    response_model=Page[FinancialRoleSuggestion],
    tags=["financial roles"],
    summary="Generate financial-role suggestions",
)
def financial_role_suggestions_route(
    request: FinancialRoleSuggestionRequest,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[FinancialRoleSuggestion]:
    """Suggest transfers, refunds, and reimbursements without applying them."""
    return suggest_financial_roles(factory, request, pagination)


@router.get(
    "/profiles/{profile_id}/financial-roles/reviews",
    response_model=Page[RoleReviewItem],
    tags=["financial roles"],
    summary="List financial-role reviews",
)
def financial_role_reviews_route(
    profile_id: str,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[RoleReviewItem]:
    """Return the bounded explicit role-review queue."""
    return page_financial_role_reviews(
        factory,
        user_profile_id=profile_id,
        pagination=pagination,
    )


@router.post(
    "/financial-role-suggestions/{suggestion_id}/confirm",
    response_model=RoleDecisionResult,
    tags=["financial roles"],
    summary="Confirm a financial-role suggestion",
)
def confirm_financial_role_route(
    suggestion_id: str,
    request: RoleDecisionRequest,
    factory: SessionFactoryDependency,
) -> RoleDecisionResult:
    """Apply the suggested role or paired transfer atomically."""
    return confirm_role_suggestion(
        factory, suggestion_id=suggestion_id, request=request
    )


@router.post(
    "/financial-role-suggestions/{suggestion_id}/reject",
    response_model=RoleDecisionResult,
    tags=["financial roles"],
    summary="Reject a financial-role suggestion",
)
def reject_financial_role_route(
    suggestion_id: str,
    request: RoleDecisionRequest,
    factory: SessionFactoryDependency,
) -> RoleDecisionResult:
    """Reject a suggestion without altering a transaction."""
    return reject_role_suggestion(factory, suggestion_id=suggestion_id, request=request)


@router.post(
    "/transactions/{transaction_id}/financial-role",
    response_model=RoleDecisionResult,
    tags=["financial roles"],
    summary="Apply a transaction financial-role action",
)
def transaction_financial_role_route(
    transaction_id: str,
    request: TransactionRoleReviewRequest,
    factory: SessionFactoryDependency,
) -> RoleDecisionResult:
    """Apply an explicit role override or needs-review flag."""
    return review_transaction_role(
        factory,
        transaction_id=transaction_id,
        request=request,
    )


@router.get(
    "/transactions/{transaction_id}/financial-role-audits",
    response_model=Page[FinancialRoleAudit],
    tags=["financial roles"],
    summary="List transaction financial-role history",
)
def financial_role_audits_route(
    transaction_id: str,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[FinancialRoleAudit]:
    """Return immutable role history without raw import evidence."""
    return page_financial_role_audits(
        factory,
        transaction_id=transaction_id,
        pagination=pagination,
    )


@router.post(
    "/forecasts/evaluate",
    response_model=ForecastTrainingResult,
    tags=["forecasts"],
    summary="Evaluate the primary forecast model",
)
def evaluate_forecast_route(
    request: ForecastEvaluationRequest,
    factory: SessionFactoryDependency,
) -> ForecastTrainingResult:
    """Build past-only features and compare the candidate with baselines."""
    return evaluate_forecast_model(factory, request)


@router.post(
    "/forecasts/balance",
    response_model=BalanceForecastPath,
    tags=["forecasts"],
    summary="Calculate a future balance path",
)
def balance_forecast_route(
    request: BalanceForecastRequest,
    factory: SessionFactoryDependency,
) -> BalanceForecastPath:
    """Train/select locally and return uncertainty-aware daily balances."""
    return calculate_balance_forecast(factory, request)


@router.post(
    "/budgets",
    response_model=Budget,
    status_code=status.HTTP_201_CREATED,
    tags=["planning"],
    summary="Create a budget",
)
def create_budget_route(
    request: BudgetCreate, factory: SessionFactoryDependency
) -> Budget:
    """Create a monthly category or weekly discretionary budget."""
    return create_planning_budget(factory, request)


@router.get(
    "/profiles/{profile_id}/budgets",
    response_model=Page[Budget],
    tags=["planning"],
    summary="List budgets active on a date",
)
def budgets_route(
    profile_id: str,
    as_of_date: Annotated[date, Query()],
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[Budget]:
    """Return a bounded page of budgets containing the selected date."""
    return page_budgets(
        factory,
        user_profile_id=profile_id,
        as_of_date=as_of_date,
        pagination=pagination,
    )


@router.post(
    "/goals",
    response_model=FinancialGoal,
    status_code=status.HTTP_201_CREATED,
    tags=["planning"],
    summary="Create a financial goal",
)
def create_goal_route(
    request: FinancialGoalCreate, factory: SessionFactoryDependency
) -> FinancialGoal:
    """Create a savings target or minimum-balance floor."""
    return create_planning_goal(factory, request)


@router.get(
    "/profiles/{profile_id}/goals",
    response_model=Page[FinancialGoal],
    tags=["planning"],
    summary="List financial goals",
)
def goals_route(
    profile_id: str,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[FinancialGoal]:
    """Return a bounded page of goals owned by one profile."""
    return page_financial_goals(
        factory,
        user_profile_id=profile_id,
        pagination=pagination,
    )


@router.post(
    "/planning/evaluate",
    response_model=FinancialPlanningResult,
    tags=["planning"],
    summary="Evaluate budgets, goals, and safe spending",
)
def planning_route(
    request: PlanningApiRequest,
    factory: SessionFactoryDependency,
) -> FinancialPlanningResult:
    """Build forecasts server-side before deterministic planning calculations."""
    return calculate_financial_plan(factory, request)


@router.post(
    "/scenarios/evaluate",
    response_model=FinancialScenarioComparison,
    tags=["scenarios"],
    summary="Compare one isolated financial scenario",
)
def scenario_route(
    request: ScenarioApiRequest,
    factory: SessionFactoryDependency,
) -> FinancialScenarioComparison:
    """Compare baseline and hypothetical paths without persisting the scenario."""
    return calculate_scenario(factory, request)


@router.post(
    "/anomalies/detect",
    response_model=AnomalyDetectionResult,
    tags=["anomalies"],
    summary="Detect unusual transactions for review",
)
def anomalies_route(
    request: AnomalyDetectionPlan,
    factory: SessionFactoryDependency,
) -> AnomalyDetectionResult:
    """Return rules/model review signals without alleging fraud."""
    return calculate_anomalies(factory, request)


@router.get(
    "/models",
    response_model=Page[RegisteredModel],
    tags=["models"],
    summary="List model information",
)
def models_route(
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
    task: Annotated[ModelTask | None, Query()] = None,
) -> Page[RegisteredModel]:
    """Return aggregate local model metadata without private feature values."""
    return page_models(factory, task=task, pagination=pagination)


@router.get(
    "/models/{task}/active",
    response_model=RegisteredModel,
    tags=["models"],
    summary="Get the active model for a task",
)
def active_model_route(
    task: ModelTask, factory: SessionFactoryDependency
) -> RegisteredModel:
    """Return only a version explicitly activated after its selection gate."""
    return active_model(factory, task=task)


__all__ = ["router"]
