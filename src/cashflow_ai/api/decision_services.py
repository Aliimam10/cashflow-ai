"""API orchestration for analytics, ML, reviews, and financial planning."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.analytics import compute_cash_flow_analytics
from cashflow_ai.anomalies import detect_unusual_transactions
from cashflow_ai.api.services import ApiServiceError, ApiServiceErrorCode, page_items
from cashflow_ai.balances import assess_financial_data_freshness
from cashflow_ai.categorisation import (
    apply_category_feedback,
    list_categories,
    list_low_confidence_reviews,
)
from cashflow_ai.financial_roles import (
    apply_transaction_review_action,
    confirm_financial_role_suggestion,
    generate_financial_role_suggestions,
    list_financial_role_audits,
    list_financial_role_review_queue,
    reject_financial_role_suggestion,
)
from cashflow_ai.forecasting import (
    build_balance_forecast_path,
    build_forecast_dataset,
    train_primary_forecaster,
)
from cashflow_ai.invalidation import (
    begin_derived_computation,
    complete_derived_computations,
    get_financial_data_revision,
    list_derived_result_freshness,
)
from cashflow_ai.model_registry import get_active_model, list_registered_models
from cashflow_ai.persistence.base import utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.repositories import AccountRepository
from cashflow_ai.planning import (
    create_budget,
    create_financial_goal,
    evaluate_financial_plan,
    evaluate_financial_scenario,
    list_budgets,
    list_financial_goals,
    projection_from_balance_forecast,
)
from cashflow_ai.recurrence import detect_recurring_payments, review_recurring_payment
from cashflow_ai.schemas.analytics import (
    AnalyticsScope,
    CashFlowAnalytics,
    DataCoverageIndicator,
)
from cashflow_ai.schemas.anomalies import AnomalyDetectionPlan, AnomalyDetectionResult
from cashflow_ai.schemas.api import Page, Pagination
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
    DerivedOutputType,
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


def _refresh_for_accounts[PayloadT](
    factory: sessionmaker[Session],
    *,
    account_ids: tuple[str, ...],
    output_type: DerivedOutputType,
    compute: Callable[[], PayloadT],
) -> PayloadT:
    """Return a calculation only when every captured source revision stayed stable."""
    tokens = tuple(
        begin_derived_computation(
            factory,
            account_id=account_id,
            output_type=output_type,
        )
        for account_id in account_ids
    )
    payload = compute()
    complete_derived_computations(factory, tokens=tokens)
    return payload


def _profile_account_ids(
    factory: sessionmaker[Session], user_profile_id: str
) -> tuple[str, ...]:
    with session_scope(factory) as session:
        return tuple(
            item.id
            for item in AccountRepository(session).list_for_user(user_profile_id)
        )


def _require_present_cutoff(value: datetime) -> None:
    """Reject future knowledge claims at the external API trust boundary."""
    if value.astimezone(UTC) > utc_now():
        raise ApiServiceError(
            ApiServiceErrorCode.INVALID_KNOWLEDGE_CUTOFF,
            "knowledge cutoff cannot be in the future",
        )


def calculate_cash_flow(
    factory: sessionmaker[Session], scope: AnalyticsScope
) -> CashFlowAnalytics:
    """Compute analytics and record revision-safe freshness for selected accounts."""
    return _refresh_for_accounts(
        factory,
        account_ids=scope.account_ids,
        output_type=DerivedOutputType.ANALYTICS,
        compute=lambda: compute_cash_flow_analytics(factory, scope),
    )


def calculate_coverage(
    factory: sessionmaker[Session], scope: AnalyticsScope
) -> DataCoverageIndicator:
    """Return the exact coverage evidence used by cash-flow analytics."""
    return calculate_cash_flow(factory, scope).coverage


def calculate_financial_data_freshness(
    factory: sessionmaker[Session], request: FinancialDataFreshnessRequest
) -> FinancialDataFreshness:
    """Assess transaction, balance, and coverage ages under explicit limits."""
    return assess_financial_data_freshness(
        factory,
        account_id=request.account_id,
        as_of_date=request.as_of_date,
        policy=request.policy,
    )


def refresh_recurring_payments(
    factory: sessionmaker[Session],
    *,
    request: RecurrenceDetectionRequest,
    pagination: Pagination,
) -> Page[RecurringPaymentCandidate]:
    """Detect recurring candidates and bind the result to account revisions."""
    _require_present_cutoff(request.knowledge_cutoff_at)
    account_ids = _profile_account_ids(factory, request.user_profile_id)
    candidates = _refresh_for_accounts(
        factory,
        account_ids=account_ids,
        output_type=DerivedOutputType.RECURRING_SERIES,
        compute=lambda: detect_recurring_payments(
            factory,
            user_profile_id=request.user_profile_id,
            as_of_date=request.as_of_date,
            knowledge_cutoff_at=request.knowledge_cutoff_at,
            policy=request.policy,
        ),
    )
    return page_items(candidates, pagination)


def review_recurrence(
    factory: sessionmaker[Session], review: RecurrenceReview
) -> RecurrenceReviewResult:
    """Apply one explicit recurring-series confirmation or cancellation."""
    return review_recurring_payment(factory, review=review)


def page_categories(
    factory: sessionmaker[Session], pagination: Pagination
) -> Page[CategorySummary]:
    """Return one bounded taxonomy page."""
    return page_items(list_categories(factory), pagination)


def page_category_reviews(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
    pagination: Pagination,
) -> Page[LowConfidenceReviewItem]:
    """Return pending low-confidence ML decisions without raw text."""
    return page_items(
        list_low_confidence_reviews(factory, user_profile_id=user_profile_id),
        pagination,
    )


def correct_category(
    factory: sessionmaker[Session], feedback: CategoryFeedback
) -> CategoryFeedbackResult:
    """Apply one explicit transaction category correction."""
    return apply_category_feedback(factory, feedback=feedback)


def suggest_financial_roles(
    factory: sessionmaker[Session],
    request: FinancialRoleSuggestionRequest,
    pagination: Pagination,
) -> Page[FinancialRoleSuggestion]:
    """Generate advisory transfer/refund/reimbursement suggestions."""
    return page_items(
        generate_financial_role_suggestions(
            factory,
            user_profile_id=request.user_profile_id,
            generated_at=utc_now(),
        ),
        pagination,
    )


def page_financial_role_reviews(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
    pagination: Pagination,
) -> Page[RoleReviewItem]:
    """Return one bounded explicit financial-role review queue."""
    return page_items(
        list_financial_role_review_queue(factory, user_profile_id=user_profile_id),
        pagination,
    )


def confirm_role_suggestion(
    factory: sessionmaker[Session],
    *,
    suggestion_id: str,
    request: RoleDecisionRequest,
) -> RoleDecisionResult:
    """Confirm one suggestion using the service's authoritative receipt time."""
    return confirm_financial_role_suggestion(
        factory,
        suggestion_id=suggestion_id,
        reviewed_at=request.reviewed_at,
    )


def reject_role_suggestion(
    factory: sessionmaker[Session],
    *,
    suggestion_id: str,
    request: RoleDecisionRequest,
) -> RoleDecisionResult:
    """Reject one suggestion without changing its transactions."""
    return reject_financial_role_suggestion(
        factory,
        suggestion_id=suggestion_id,
        reviewed_at=request.reviewed_at,
    )


def review_transaction_role(
    factory: sessionmaker[Session],
    *,
    transaction_id: str,
    request: TransactionRoleReviewRequest,
) -> RoleDecisionResult:
    """Apply one explicit transaction-level financial-role action."""
    return apply_transaction_review_action(
        factory,
        transaction_id=transaction_id,
        action=request.action,
        changed_at=request.changed_at,
    )


def page_financial_role_audits(
    factory: sessionmaker[Session],
    *,
    transaction_id: str,
    pagination: Pagination,
) -> Page[FinancialRoleAudit]:
    """Return immutable role history without raw import rows."""
    return page_items(
        list_financial_role_audits(factory, transaction_id=transaction_id),
        pagination,
    )


def evaluate_forecast_model(
    factory: sessionmaker[Session], request: ForecastEvaluationRequest
) -> ForecastTrainingResult:
    """Build cutoff-safe features and evaluate the primary candidate server-side."""
    _require_present_cutoff(request.dataset_plan.knowledge_cutoff_at)

    def compute() -> ForecastTrainingResult:
        dataset = build_forecast_dataset(factory, plan=request.dataset_plan)
        trained = train_primary_forecaster(dataset, policy=request.model_policy)
        return ForecastTrainingResult(comparison=trained.comparison)

    return _refresh_for_accounts(
        factory,
        account_ids=request.dataset_plan.account_ids,
        output_type=DerivedOutputType.MODEL_PERFORMANCE_COMPARISONS,
        compute=compute,
    )


def _build_balance_forecast(
    factory: sessionmaker[Session], request: BalanceForecastRequest
) -> BalanceForecastPath:
    dataset = build_forecast_dataset(factory, plan=request.dataset_plan)
    trained = train_primary_forecaster(dataset, policy=request.model_policy)
    return build_balance_forecast_path(
        factory,
        dataset=dataset,
        trained=trained,
        plan=request.path_plan,
    )


def calculate_balance_forecast(
    factory: sessionmaker[Session], request: BalanceForecastRequest
) -> BalanceForecastPath:
    """Build one revision-safe daily path entirely from local verified evidence."""
    _require_present_cutoff(request.path_plan.knowledge_cutoff_at)
    return _refresh_for_accounts(
        factory,
        account_ids=(request.path_plan.account_id,),
        output_type=DerivedOutputType.FORECASTS,
        compute=lambda: _build_balance_forecast(factory, request),
    )


def create_planning_budget(
    factory: sessionmaker[Session], request: BudgetCreate
) -> Budget:
    """Create one validated budget."""
    return create_budget(factory, request=request)


def page_budgets(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
    as_of_date: date,
    pagination: Pagination,
) -> Page[Budget]:
    """Return budgets active on a date in a bounded page."""
    return page_items(
        list_budgets(
            factory,
            user_profile_id=user_profile_id,
            as_of_date=as_of_date,
        ),
        pagination,
    )


def create_planning_goal(
    factory: sessionmaker[Session], request: FinancialGoalCreate
) -> FinancialGoal:
    """Create one validated savings or balance-floor goal."""
    return create_financial_goal(factory, request=request)


def page_financial_goals(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
    pagination: Pagination,
) -> Page[FinancialGoal]:
    """Return a bounded page of one profile's financial goals."""
    return page_items(
        list_financial_goals(factory, user_profile_id=user_profile_id),
        pagination,
    )


def calculate_financial_plan(
    factory: sessionmaker[Session], request: PlanningApiRequest
) -> FinancialPlanningResult:
    """Build trusted forecasts and evaluate planning without client-supplied paths."""
    for forecast in request.forecasts:
        _require_present_cutoff(forecast.path_plan.knowledge_cutoff_at)

    def compute() -> FinancialPlanningResult:
        forecasts = tuple(
            _build_balance_forecast(factory, item) for item in request.forecasts
        )
        return evaluate_financial_plan(
            factory,
            plan=request.plan,
            balance_projections=tuple(
                projection_from_balance_forecast(item) for item in forecasts
            ),
        )

    return _refresh_for_accounts(
        factory,
        account_ids=request.plan.account_ids,
        output_type=DerivedOutputType.BUDGETS,
        compute=compute,
    )


def calculate_scenario(
    factory: sessionmaker[Session], request: ScenarioApiRequest
) -> FinancialScenarioComparison:
    """Compare an isolated scenario using a server-rebuilt baseline."""
    _require_present_cutoff(request.forecast.path_plan.knowledge_cutoff_at)

    def compute() -> FinancialScenarioComparison:
        dataset = build_forecast_dataset(factory, plan=request.forecast.dataset_plan)
        trained = train_primary_forecaster(
            dataset, policy=request.forecast.model_policy
        )
        return evaluate_financial_scenario(
            factory,
            dataset=dataset,
            trained=trained,
            forecast_plan=request.forecast.path_plan,
            planning_plan=request.planning_plan,
            scenario=request.scenario,
        )

    return _refresh_for_accounts(
        factory,
        account_ids=(request.forecast.path_plan.account_id,),
        output_type=DerivedOutputType.SCENARIOS,
        compute=compute,
    )


def calculate_anomalies(
    factory: sessionmaker[Session], plan: AnomalyDetectionPlan
) -> AnomalyDetectionResult:
    """Run the coverage-gated review detector without persisting alerts."""
    _require_present_cutoff(plan.knowledge_cutoff_at)
    return _refresh_for_accounts(
        factory,
        account_ids=plan.account_ids,
        output_type=DerivedOutputType.ANOMALY_ALERTS,
        compute=lambda: detect_unusual_transactions(factory, plan=plan),
    )


def page_models(
    factory: sessionmaker[Session],
    *,
    task: ModelTask | None,
    pagination: Pagination,
) -> Page[RegisteredModel]:
    """Return data-minimised model metadata in a bounded page."""
    return page_items(list_registered_models(factory, task=task), pagination)


def active_model(factory: sessionmaker[Session], *, task: ModelTask) -> RegisteredModel:
    """Return the explicitly active model or a stable absence error."""
    model = get_active_model(factory, task=task)
    if model is None:
        raise ApiServiceError(
            ApiServiceErrorCode.MODEL_NOT_ACTIVE,
            "no eligible model is active for the requested task",
        )
    return model


def financial_revision(
    factory: sessionmaker[Session], *, account_id: str
) -> FinancialDataRevision:
    """Return the monotonic source revision for one account."""
    return get_financial_data_revision(factory, account_id=account_id)


def page_derived_freshness(
    factory: sessionmaker[Session],
    *,
    account_id: str,
    pagination: Pagination,
) -> Page[DerivedResultFreshness]:
    """Return bounded current/stale/unavailable derived-output states."""
    return page_items(
        list_derived_result_freshness(factory, account_id=account_id), pagination
    )


__all__ = [
    "active_model",
    "calculate_anomalies",
    "calculate_balance_forecast",
    "calculate_cash_flow",
    "calculate_coverage",
    "calculate_financial_data_freshness",
    "calculate_financial_plan",
    "calculate_scenario",
    "confirm_role_suggestion",
    "correct_category",
    "create_planning_budget",
    "create_planning_goal",
    "evaluate_forecast_model",
    "financial_revision",
    "page_budgets",
    "page_categories",
    "page_category_reviews",
    "page_derived_freshness",
    "page_financial_goals",
    "page_financial_role_audits",
    "page_financial_role_reviews",
    "page_models",
    "refresh_recurring_payments",
    "reject_role_suggestion",
    "review_recurrence",
    "review_transaction_role",
    "suggest_financial_roles",
]
