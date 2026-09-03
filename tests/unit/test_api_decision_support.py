"""Tests for decision-support HTTP adapters and orchestration boundaries."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.api import decision_routes as routes
from cashflow_ai.api import decision_services as services
from cashflow_ai.api.dependencies import get_pagination
from cashflow_ai.api.errors import (
    _api_service_status,
    _domain_status,
    register_exception_handlers,
)
from cashflow_ai.api.services import ApiServiceError, ApiServiceErrorCode
from cashflow_ai.categorisation import (
    CategorisationServiceError,
    CategorisationServiceErrorCode,
)
from cashflow_ai.persistence import (
    Base,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import AccountRecord, UserProfileRecord
from cashflow_ai.schemas.api import Page, Pagination
from cashflow_ai.schemas.api_decisions import (
    BalanceForecastRequest,
    PlanningApiRequest,
    ScenarioApiRequest,
)
from cashflow_ai.schemas.invalidation import DerivedOutputType
from cashflow_ai.schemas.model_registry import ModelTask

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _factory() -> sessionmaker[Session]:
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        session.add(
            UserProfileRecord(
                id="synthetic-profile",
                display_name="Fictional User",
                base_currency="GBP",
                timezone="Europe/London",
            )
        )
        session.flush()
        session.add_all(
            [
                AccountRecord(
                    id="account-1",
                    user_profile_id="synthetic-profile",
                    name="Fictional Current",
                    account_type="current",
                    currency="GBP",
                ),
                AccountRecord(
                    id="account-2",
                    user_profile_id="synthetic-profile",
                    name="Fictional Savings",
                    account_type="savings",
                    currency="GBP",
                ),
            ]
        )
    return factory


def test_all_decision_routes_delegate_to_application_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory: Any = object()
    request: Any = object()
    pagination = Pagination(limit=10, offset=0)
    as_of_date = date(2026, 8, 14)
    result: Any = object()
    service_names = (
        "calculate_cash_flow",
        "calculate_coverage",
        "calculate_financial_data_freshness",
        "financial_revision",
        "page_derived_freshness",
        "refresh_recurring_payments",
        "page_recurring_payments",
        "review_recurrence",
        "page_categories",
        "page_category_reviews",
        "correct_category",
        "suggest_financial_roles",
        "page_financial_role_reviews",
        "confirm_role_suggestion",
        "reject_role_suggestion",
        "review_transaction_role",
        "page_financial_role_audits",
        "evaluate_forecast_model",
        "calculate_balance_forecast",
        "create_planning_budget",
        "page_budgets",
        "create_planning_goal",
        "page_financial_goals",
        "calculate_financial_plan",
        "calculate_scenario",
        "calculate_anomalies",
        "page_models",
        "active_model",
    )
    for name in service_names:
        monkeypatch.setattr(routes, name, MagicMock(return_value=result))

    delegated = (
        routes.cash_flow_route(request, factory),
        routes.coverage_route(request, factory),
        routes.financial_data_freshness_route(request, factory),
        routes.financial_revision_route("account", factory),
        routes.derived_freshness_route("account", factory, pagination),
        routes.detect_recurring_route(request, factory, pagination),
        routes.recurring_candidates_route("profile", factory, pagination),
        routes.review_recurring_route(request, factory),
        routes.categories_route(factory, pagination),
        routes.category_reviews_route("profile", factory, pagination),
        routes.category_feedback_route(request, factory),
        routes.financial_role_suggestions_route(request, factory, pagination),
        routes.financial_role_reviews_route("profile", factory, pagination),
        routes.confirm_financial_role_route("suggestion", request, factory),
        routes.reject_financial_role_route("suggestion", request, factory),
        routes.transaction_financial_role_route("transaction", request, factory),
        routes.financial_role_audits_route("transaction", factory, pagination),
        routes.evaluate_forecast_route(request, factory),
        routes.balance_forecast_route(request, factory),
        routes.create_budget_route(request, factory),
        routes.budgets_route("profile", as_of_date, factory, pagination),
        routes.create_goal_route(request, factory),
        routes.goals_route("profile", factory, pagination),
        routes.planning_route(request, factory),
        routes.scenario_route(request, factory),
        routes.anomalies_route(request, factory),
        routes.models_route(factory, pagination, ModelTask.TRANSACTION_CATEGORISATION),
        routes.active_model_route(ModelTask.TRANSACTION_CATEGORISATION, factory),
    )
    assert all(value is result for value in delegated)


def _run_compute(_factory: Any, **values: Any) -> Any:
    return values["compute"]()


def _assert_same(value: object, expected: object) -> None:
    assert value is expected


def test_decision_services_delegate_and_preserve_revision_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory()
    pagination = Pagination(limit=1, offset=0)
    result: Any = object()
    begin = MagicMock(side_effect=("token-1", "token-2"))
    complete = MagicMock(return_value=(object(), object()))
    monkeypatch.setattr(services, "begin_derived_computation", begin)
    monkeypatch.setattr(services, "complete_derived_computations", complete)

    _assert_same(
        services._refresh_for_accounts(
            factory,
            account_ids=("account-1", "account-2"),
            output_type=DerivedOutputType.ANALYTICS,
            compute=lambda: result,
        ),
        result,
    )
    assert begin.call_count == 2
    complete.assert_called_once_with(factory, tokens=("token-1", "token-2"))
    assert services._profile_account_ids(factory, "synthetic-profile") == (
        "account-1",
        "account-2",
    )
    services._require_present_cutoff(NOW)
    with pytest.raises(ApiServiceError) as future_error:
        services._require_present_cutoff(datetime.now(UTC) + timedelta(days=1))
    assert future_error.value.code is ApiServiceErrorCode.INVALID_KNOWLEDGE_CUTOFF

    request: Any = SimpleNamespace(
        account_id="account-1",
        user_profile_id="synthetic-profile",
        as_of_date=date(2026, 8, 14),
        knowledge_cutoff_at=NOW,
        policy=object(),
        reviewed_at=NOW,
        changed_at=NOW,
        action=object(),
    )
    scope: Any = SimpleNamespace(account_ids=("account-1",))
    monkeypatch.setattr(services, "_refresh_for_accounts", _run_compute)
    monkeypatch.setattr(
        services, "compute_cash_flow_analytics", MagicMock(return_value=result)
    )
    _assert_same(services.calculate_cash_flow(factory, scope), result)
    monkeypatch.setattr(
        services,
        "calculate_cash_flow",
        MagicMock(return_value=SimpleNamespace(coverage=result)),
    )
    _assert_same(services.calculate_coverage(factory, scope), result)
    monkeypatch.setattr(
        services, "assess_financial_data_freshness", MagicMock(return_value=result)
    )
    _assert_same(services.calculate_financial_data_freshness(factory, request), result)

    monkeypatch.setattr(
        services,
        "detect_recurring_payments",
        MagicMock(return_value=("first", "second")),
    )
    recurring = services.refresh_recurring_payments(
        factory, request=request, pagination=pagination
    )
    assert cast(Any, recurring).items == ("first",)
    assert recurring.total == 2
    monkeypatch.setattr(
        services,
        "list_recurring_payment_candidates",
        MagicMock(return_value=(result,)),
    )
    assert services.page_recurring_payments(
        factory,
        user_profile_id="synthetic-profile",
        pagination=pagination,
    ).items == (result,)
    monkeypatch.setattr(
        services, "review_recurring_payment", MagicMock(return_value=result)
    )
    _assert_same(services.review_recurrence(factory, request), result)

    monkeypatch.setattr(services, "list_categories", MagicMock(return_value=(result,)))
    assert services.page_categories(factory, pagination).items == (result,)
    monkeypatch.setattr(
        services, "list_low_confidence_reviews", MagicMock(return_value=(result,))
    )
    assert services.page_category_reviews(
        factory,
        user_profile_id="synthetic-profile",
        pagination=pagination,
    ).items == (result,)
    monkeypatch.setattr(
        services, "apply_category_feedback", MagicMock(return_value=result)
    )
    _assert_same(services.correct_category(factory, request), result)

    monkeypatch.setattr(
        services,
        "generate_financial_role_suggestions",
        MagicMock(return_value=(result,)),
    )
    assert services.suggest_financial_roles(factory, request, pagination).items == (
        result,
    )
    monkeypatch.setattr(
        services,
        "list_financial_role_review_queue",
        MagicMock(return_value=(result,)),
    )
    assert services.page_financial_role_reviews(
        factory,
        user_profile_id="synthetic-profile",
        pagination=pagination,
    ).items == (result,)
    monkeypatch.setattr(
        services, "confirm_financial_role_suggestion", MagicMock(return_value=result)
    )
    _assert_same(
        services.confirm_role_suggestion(
            factory, suggestion_id="suggestion", request=request
        ),
        result,
    )
    monkeypatch.setattr(
        services, "reject_financial_role_suggestion", MagicMock(return_value=result)
    )
    _assert_same(
        services.reject_role_suggestion(
            factory, suggestion_id="suggestion", request=request
        ),
        result,
    )
    monkeypatch.setattr(
        services, "apply_transaction_review_action", MagicMock(return_value=result)
    )
    _assert_same(
        services.review_transaction_role(
            factory, transaction_id="transaction", request=request
        ),
        result,
    )
    monkeypatch.setattr(
        services, "list_financial_role_audits", MagicMock(return_value=(result,))
    )
    assert services.page_financial_role_audits(
        factory,
        transaction_id="transaction",
        pagination=pagination,
    ).items == (result,)

    dataset = object()
    trained = SimpleNamespace(comparison=object())
    monkeypatch.setattr(
        services, "build_forecast_dataset", MagicMock(return_value=dataset)
    )
    monkeypatch.setattr(
        services, "train_primary_forecaster", MagicMock(return_value=trained)
    )

    class TrainingResult:
        def __init__(self, *, comparison: object) -> None:
            self.comparison = comparison

    monkeypatch.setattr(services, "ForecastTrainingResult", TrainingResult)
    forecast_request: Any = SimpleNamespace(
        dataset_plan=SimpleNamespace(
            account_ids=("account-1",), knowledge_cutoff_at=NOW
        ),
        model_policy=object(),
        path_plan=SimpleNamespace(
            account_id="account-1",
            knowledge_cutoff_at=NOW,
        ),
    )
    evaluated = services.evaluate_forecast_model(factory, forecast_request)
    assert cast(Any, evaluated).comparison is trained.comparison
    monkeypatch.setattr(
        services, "build_balance_forecast_path", MagicMock(return_value=result)
    )
    _assert_same(services._build_balance_forecast(factory, forecast_request), result)
    _assert_same(services.calculate_balance_forecast(factory, forecast_request), result)

    monkeypatch.setattr(services, "create_budget", MagicMock(return_value=result))
    _assert_same(services.create_planning_budget(factory, request), result)
    monkeypatch.setattr(services, "list_budgets", MagicMock(return_value=(result,)))
    assert services.page_budgets(
        factory,
        user_profile_id="synthetic-profile",
        as_of_date=date(2026, 8, 14),
        pagination=pagination,
    ).items == (result,)
    monkeypatch.setattr(
        services, "create_financial_goal", MagicMock(return_value=result)
    )
    _assert_same(services.create_planning_goal(factory, request), result)
    monkeypatch.setattr(
        services, "list_financial_goals", MagicMock(return_value=(result,))
    )
    assert services.page_financial_goals(
        factory,
        user_profile_id="synthetic-profile",
        pagination=pagination,
    ).items == (result,)

    forecast_two = SimpleNamespace(
        path_plan=SimpleNamespace(account_id="account-2", knowledge_cutoff_at=NOW)
    )
    planning_request: Any = SimpleNamespace(
        forecasts=(forecast_request, forecast_two),
        plan=SimpleNamespace(account_ids=("account-1", "account-2")),
    )
    monkeypatch.setattr(
        services, "_build_balance_forecast", MagicMock(side_effect=("path-1", "path-2"))
    )
    monkeypatch.setattr(
        services,
        "projection_from_balance_forecast",
        MagicMock(side_effect=lambda value: f"projection-{value}"),
    )
    monkeypatch.setattr(
        services, "evaluate_financial_plan", MagicMock(return_value=result)
    )
    _assert_same(services.calculate_financial_plan(factory, planning_request), result)

    scenario_request: Any = SimpleNamespace(
        forecast=forecast_request,
        planning_plan=object(),
        scenario=object(),
    )
    monkeypatch.setattr(
        services, "evaluate_financial_scenario", MagicMock(return_value=result)
    )
    _assert_same(services.calculate_scenario(factory, scenario_request), result)
    anomaly_plan: Any = SimpleNamespace(
        account_ids=("account-1",), knowledge_cutoff_at=NOW
    )
    monkeypatch.setattr(
        services, "detect_unusual_transactions", MagicMock(return_value=result)
    )
    _assert_same(services.calculate_anomalies(factory, anomaly_plan), result)

    monkeypatch.setattr(
        services, "list_registered_models", MagicMock(return_value=(result,))
    )
    assert services.page_models(
        factory, task=ModelTask.TRANSACTION_CATEGORISATION, pagination=pagination
    ).items == (result,)
    monkeypatch.setattr(services, "get_active_model", MagicMock(return_value=result))
    _assert_same(
        services.active_model(factory, task=ModelTask.TRANSACTION_CATEGORISATION),
        result,
    )
    monkeypatch.setattr(services, "get_active_model", MagicMock(return_value=None))
    with pytest.raises(ApiServiceError) as inactive:
        services.active_model(factory, task=ModelTask.TRANSACTION_CATEGORISATION)
    assert inactive.value.code is ApiServiceErrorCode.MODEL_NOT_ACTIVE
    monkeypatch.setattr(
        services, "get_financial_data_revision", MagicMock(return_value=result)
    )
    _assert_same(services.financial_revision(factory, account_id="account-1"), result)
    monkeypatch.setattr(
        services, "list_derived_result_freshness", MagicMock(return_value=(result,))
    )
    assert services.page_derived_freshness(
        factory, account_id="account-1", pagination=pagination
    ).items == (result,)


def _forecast_contracts() -> tuple[Any, Any, Any]:
    dataset = SimpleNamespace(
        user_profile_id="profile",
        account_ids=("account",),
        knowledge_cutoff_at=NOW,
    )
    path = SimpleNamespace(
        user_profile_id="profile",
        account_id="account",
        knowledge_cutoff_at=NOW,
        forecast_start=date(2026, 8, 17),
    )
    forecast = BalanceForecastRequest.model_construct(
        dataset_plan=dataset,
        model_policy=object(),
        path_plan=path,
    )
    plan = SimpleNamespace(
        user_profile_id="profile",
        account_ids=("account",),
        as_of_date=date(2026, 8, 14),
    )
    scenario = SimpleNamespace(user_profile_id="profile", account_id="account")
    return forecast, plan, scenario


def test_decision_request_contracts_reject_cross_scope_inputs() -> None:
    forecast, plan, scenario = _forecast_contracts()
    assert cast(Any, forecast).validate_alignment() is forecast
    planning = PlanningApiRequest.model_construct(plan=plan, forecasts=(forecast,))
    assert cast(Any, planning).validate_scope().forecasts == (forecast,)
    scenario_request = ScenarioApiRequest.model_construct(
        forecast=forecast,
        planning_plan=plan,
        scenario=scenario,
    )
    assert cast(Any, scenario_request).validate_scope().scenario is scenario

    for changed in (
        {"user_profile_id": "other"},
        {"account_ids": ("other",)},
        {"knowledge_cutoff_at": NOW - timedelta(seconds=1)},
    ):
        bad_dataset = SimpleNamespace(**(vars(forecast.dataset_plan) | changed))
        invalid = BalanceForecastRequest.model_construct(
            dataset_plan=bad_dataset,
            model_policy=object(),
            path_plan=forecast.path_plan,
        )
        with pytest.raises(ValueError, match="share one profile"):
            cast(Any, invalid).validate_alignment()

    wrong_accounts = SimpleNamespace(**(vars(plan) | {"account_ids": ("other",)}))
    invalid_planning = PlanningApiRequest.model_construct(
        plan=wrong_accounts, forecasts=(forecast,)
    )
    with pytest.raises(ValueError, match="ordered profile account"):
        cast(Any, invalid_planning).validate_scope()
    wrong_profile_path = SimpleNamespace(
        **(vars(forecast.path_plan) | {"user_profile_id": "other"})
    )
    wrong_profile_forecast = BalanceForecastRequest.model_construct(
        dataset_plan=forecast.dataset_plan,
        model_policy=object(),
        path_plan=wrong_profile_path,
    )
    invalid_planning = PlanningApiRequest.model_construct(
        plan=plan, forecasts=(wrong_profile_forecast,)
    )
    with pytest.raises(ValueError, match="ordered profile account"):
        cast(Any, invalid_planning).validate_scope()
    late_plan = SimpleNamespace(**(vars(plan) | {"as_of_date": date(2026, 8, 17)}))
    invalid_planning = PlanningApiRequest.model_construct(
        plan=late_plan, forecasts=(forecast,)
    )
    with pytest.raises(ValueError, match="start after"):
        cast(Any, invalid_planning).validate_scope()

    scenario_mismatches = (
        (SimpleNamespace(**(vars(plan) | {"user_profile_id": "other"})), scenario),
        (SimpleNamespace(**(vars(plan) | {"account_ids": ("other",)})), scenario),
        (plan, SimpleNamespace(user_profile_id="other", account_id="account")),
        (plan, SimpleNamespace(user_profile_id="profile", account_id="other")),
    )
    for bad_plan, bad_scenario in scenario_mismatches:
        invalid_scenario = ScenarioApiRequest.model_construct(
            forecast=forecast,
            planning_plan=bad_plan,
            scenario=bad_scenario,
        )
        with pytest.raises(ValueError, match="one profile account"):
            cast(Any, invalid_scenario).validate_scope()


def test_pagination_and_domain_error_translation_are_bounded_and_private() -> None:
    assert get_pagination(limit=2, offset=1) == Pagination(limit=2, offset=1)
    assert Page[str](items=("b",), limit=1, offset=1, total=2).items == ("b",)
    with pytest.raises(ValidationError, match="declared result window"):
        Page[str](items=("a", "b"), limit=1, offset=0, total=2)
    with pytest.raises(ValidationError, match="declared result window"):
        Page[str](items=("a",), limit=1, offset=2, total=2)

    assert _api_service_status(ApiServiceErrorCode.INVALID_KNOWLEDGE_CUTOFF) == 400
    assert _domain_status("invalid_stored_metadata") == 500
    assert _domain_status("profile_not_found") == 404
    assert _domain_status("history_unavailable") == 404
    for code in (
        "already_reviewed",
        "state_conflict",
        "duplicate_goal",
        "scope_mismatch",
        "model_not_active",
        "result_not_current",
        "model_not_eligible",
        "stale_revision",
        "source_changed_during_computation",
    ):
        assert _domain_status(code) == 409
    assert _domain_status("invalid_scope") == 400


def test_controlled_and_unexpected_errors_use_safe_http_problems() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/controlled")
    def controlled() -> None:
        raise CategorisationServiceError(
            CategorisationServiceErrorCode.CATEGORY_NOT_FOUND,
            "category does not exist",
        )

    @app.get("/unexpected")
    def unexpected() -> None:
        raise RuntimeError("private synthetic detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        controlled_response = client.get("/controlled")
        unexpected_response = client.get("/unexpected")
    assert controlled_response.status_code == 404
    assert controlled_response.json() == {
        "code": "category_not_found",
        "message": "category does not exist",
        "page_numbers": [],
        "validation_issues": [],
    }
    assert unexpected_response.status_code == 500
    assert "private synthetic detail" not in unexpected_response.text
