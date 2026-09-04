"""Tests for planning, scenario, anomaly, and model Streamlit controls."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import cashflow_ai.frontend.planning_page as page
from cashflow_ai.schemas.accounts import AccountType
from cashflow_ai.schemas.anomalies import (
    AnomalyDetectionMode,
    AnomalyFeedbackAction,
    AnomalyReviewStatus,
    AnomalySignal,
    AnomalySignalCode,
    AnomalyUserLabel,
    AnomalyWarningCode,
)
from cashflow_ai.schemas.api import AccountResponse, Page
from cashflow_ai.schemas.categories import CategorySummary
from cashflow_ai.schemas.model_registry import ModelMetricUnit, ModelTask
from cashflow_ai.schemas.planning import (
    BudgetType,
    FinancialGoalType,
    PlanningWarningCode,
    SafeSpendingLimitingFactor,
)
from cashflow_ai.schemas.recurrence import RecurrenceFrequency, RecurrenceStatus
from cashflow_ai.schemas.scenarios import (
    FinancialScenarioType,
    ScenarioComparisonWarningCode,
)
from cashflow_ai.schemas.transactions import Currency

AS_OF = date(2026, 8, 30)
NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _account(identifier: str = "account-1") -> AccountResponse:
    return AccountResponse(
        account_id=identifier,
        user_profile_id="synthetic-profile",
        name=f"Fictional {identifier}",
        account_type=AccountType.CURRENT,
        currency=Currency.GBP,
        institution_label=None,
        is_active=True,
        created_at=NOW,
    )


def _category(identifier: str = "food", *, active: bool = True) -> CategorySummary:
    return CategorySummary(
        id=identifier,
        name=identifier.title(),
        taxonomy_version="1.0",
        is_active=active,
    )


def _ui(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    ui = MagicMock()
    ui.container.return_value = nullcontext()
    monkeypatch.setattr(page, "st", ui)
    monkeypatch.setattr(page, "loading_state", lambda message: nullcontext())
    return ui


def test_amount_period_and_scenario_definition_helpers() -> None:
    assert page._amount(10.126) == Decimal("10.13")
    one_off = page._scenario_definition(
        profile_id="synthetic-profile",
        account_id="account-1",
        scenario_type=FinancialScenarioType.ONE_OFF_PURCHASE,
        name="Fictional purchase",
        start=date(2026, 9, 7),
        end=None,
        amount=Decimal("10.00"),
        frequency=None,
        category_id=None,
        recurring_payment_id=None,
    )
    assert one_off.scenario_id == "temporary-ui-scenario"
    assert one_off.amount == Decimal("10.00")


def test_budget_setup_handles_missing_category_and_saves_both_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    ui.number_input.return_value = 100.0
    ui.date_input.return_value = AS_OF
    ui.selectbox.return_value = BudgetType.MONTHLY_CATEGORY
    ui.button.return_value = False
    page._render_budget_setup(
        client,
        profile_id="synthetic-profile",
        currency=Currency.GBP,
        categories=(_category("inactive", active=False),),
        as_of=AS_OF,
    )
    ui.warning.assert_called_once()

    ui.button.return_value = True
    page._render_budget_setup(
        client,
        profile_id="synthetic-profile",
        currency=Currency.GBP,
        categories=(),
        as_of=AS_OF,
    )
    ui.error.assert_called_once()
    client.create_budget.assert_not_called()

    ui.selectbox.side_effect = [BudgetType.MONTHLY_CATEGORY, "food"]
    client.create_budget.return_value = SimpleNamespace(amount_limit=Decimal("100"))
    page._render_budget_setup(
        client,
        profile_id="synthetic-profile",
        currency=Currency.GBP,
        categories=(_category(), _category("inactive", active=False)),
        as_of=AS_OF,
    )
    monthly = client.create_budget.call_args.args[0]
    assert monthly.category_id == "food"
    assert monthly.period.start_date == date(2026, 8, 1)

    ui.selectbox.side_effect = None
    ui.selectbox.return_value = BudgetType.WEEKLY_DISCRETIONARY
    page._render_budget_setup(
        client,
        profile_id="synthetic-profile",
        currency=Currency.GBP,
        categories=(),
        as_of=AS_OF,
    )
    weekly = client.create_budget.call_args.args[0]
    assert weekly.category_id is None
    assert weekly.period.start_date == date(2026, 8, 24)


def test_goal_setup_handles_cancel_empty_name_and_both_goal_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    ui.number_input.side_effect = [500.0, 50.0]
    ui.date_input.return_value = date(2026, 11, 30)
    ui.text_input.return_value = "Fictional savings"
    ui.selectbox.side_effect = [FinancialGoalType.SAVINGS_TARGET, "account-1"]
    ui.button.return_value = False
    page._render_goal_setup(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        as_of=AS_OF,
    )
    client.create_goal.assert_not_called()

    ui.selectbox.side_effect = [FinancialGoalType.SAVINGS_TARGET, "account-1"]
    ui.number_input.side_effect = [500.0, 50.0]
    ui.text_input.return_value = "  "
    ui.button.return_value = True
    page._render_goal_setup(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        as_of=AS_OF,
    )
    ui.error.assert_called_once()

    ui.selectbox.side_effect = [FinancialGoalType.SAVINGS_TARGET, "account-1"]
    ui.number_input.side_effect = [500.0, 50.0]
    ui.text_input.return_value = "Fictional savings"
    client.create_goal.return_value = SimpleNamespace(name="Fictional savings")
    page._render_goal_setup(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        as_of=AS_OF,
    )
    savings = client.create_goal.call_args.args[0]
    assert savings.current_amount == Decimal("50.00")
    assert savings.target_date == date(2026, 11, 30)

    ui.selectbox.side_effect = [FinancialGoalType.MINIMUM_BALANCE, "account-1"]
    ui.number_input.side_effect = [250.0]
    ui.text_input.return_value = "Fictional floor"
    client.create_goal.return_value = SimpleNamespace(name="Fictional floor")
    page._render_goal_setup(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        as_of=AS_OF,
    )
    floor = client.create_goal.call_args.args[0]
    assert floor.current_amount == 0
    assert floor.target_date is None


def test_planning_result_displays_available_and_unknown_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    safe = SimpleNamespace(
        safe_weekly_spending=Decimal("42.00"),
        currency=Currency.GBP,
        limiting_factor=SafeSpendingLimitingFactor.CASH_HEADROOM,
    )
    budget = SimpleNamespace(
        budget_type=BudgetType.WEEKLY_DISCRETIONARY,
        amount_limit=Decimal("100.00"),
    )
    savings = SimpleNamespace(
        name="Savings", goal_type=FinancialGoalType.SAVINGS_TARGET
    )
    result = SimpleNamespace(
        safe_spending=safe,
        currency=Currency.GBP,
        budgets=(
            SimpleNamespace(
                budget=budget,
                amount_used=None,
                amount_remaining=None,
                projected_use=None,
            ),
            SimpleNamespace(
                budget=budget,
                amount_used=Decimal("25.00"),
                amount_remaining=Decimal("75.00"),
                projected_use=Decimal("50.00"),
            ),
        ),
        goals=(
            SimpleNamespace(
                goal=savings,
                remaining_amount=Decimal("300.00"),
                required_monthly_contribution=None,
                projected_shortfall=None,
            ),
            SimpleNamespace(
                goal=savings,
                remaining_amount=Decimal("200.00"),
                required_monthly_contribution=Decimal("50.00"),
                projected_shortfall=Decimal("10.00"),
            ),
        ),
        warnings=(SimpleNamespace(code=PlanningWarningCode.FORECAST_LIMITATION),),
    )

    page._planning_result(result)  # type: ignore[arg-type]
    assert ui.dataframe.call_count == 2
    ui.warning.assert_called_once()

    quiet = SimpleNamespace(
        safe_spending=safe,
        currency=Currency.GBP,
        budgets=(),
        goals=(),
        warnings=(),
    )
    page._planning_result(quiet)  # type: ignore[arg-type]


def test_planning_controls_validate_scope_and_evaluate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    ui.columns.return_value = (nullcontext(), nullcontext())
    monkeypatch.setattr(page, "_render_budget_setup", MagicMock())
    monkeypatch.setattr(page, "_render_goal_setup", MagicMock())
    result_display = MagicMock()
    monkeypatch.setattr(page, "_planning_result", result_display)
    client = MagicMock()
    client.list_budgets.return_value = Page[object](
        items=(object(),), limit=100, offset=0, total=1
    )
    client.list_goals.return_value = Page[object](
        items=(object(),), limit=100, offset=0, total=1
    )
    ui.select_slider.return_value = 60
    ui.button.return_value = False
    ui.multiselect.side_effect = [["account-1"], [1, 15]]
    page.render_budgets_and_goals(
        client,
        profile_id="synthetic-profile",
        currency=Currency.GBP,
        accounts=(_account(),),
        categories=(_category(),),
        as_of=AS_OF,
    )
    client.evaluate_planning.assert_not_called()

    ui.button.return_value = True
    ui.multiselect.side_effect = [[], [1]]
    page.render_budgets_and_goals(
        client,
        profile_id="synthetic-profile",
        currency=Currency.GBP,
        accounts=(_account(),),
        categories=(_category(),),
        as_of=AS_OF,
    )
    ui.multiselect.side_effect = [["account-1"], []]
    page.render_budgets_and_goals(
        client,
        profile_id="synthetic-profile",
        currency=Currency.GBP,
        accounts=(_account(),),
        categories=(_category(),),
        as_of=AS_OF,
    )

    ui.multiselect.side_effect = [["account-1"], [1, 15]]
    client.evaluate_planning.return_value = object()
    page.render_budgets_and_goals(
        client,
        profile_id="synthetic-profile",
        currency=Currency.GBP,
        accounts=(_account(),),
        categories=(_category(),),
        as_of=AS_OF,
    )
    request = client.evaluate_planning.call_args.args[0]
    assert request.plan.account_ids == ("account-1",)
    assert request.forecasts[0].path_plan.horizon_days == 60
    result_display.assert_called_once_with(client.evaluate_planning.return_value)


def _recurring(
    identifier: str,
    *,
    status: RecurrenceStatus = RecurrenceStatus.CONFIRMED,
    account_id: str = "account-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        candidate_id=identifier,
        merchant_group=identifier.title(),
        status=status,
        account_id=account_id,
    )


def _configure_scenario_ui(
    ui: MagicMock,
    *,
    scenario_type: FinancialScenarioType,
    name: str = "Fictional scenario",
    paydays: list[int] | None = None,
    compare: bool = False,
    has_end: bool = False,
) -> None:
    selected = {
        "Scenario account": "account-1",
        "Scenario type": scenario_type,
        "Scenario frequency": RecurrenceFrequency.MONTHLY,
        "Confirmed subscription": "confirmed",
        "Reduced category": "food",
    }
    ui.selectbox.side_effect = lambda label, **kwargs: selected[label]
    ui.text_input.return_value = name
    ui.select_slider.return_value = 30
    ui.date_input.side_effect = lambda label, **kwargs: (
        date(2026, 10, 5) if label == "Scenario end" else date(2026, 9, 7)
    )
    ui.multiselect.return_value = [1, 15] if paydays is None else paydays
    ui.number_input.return_value = 100.0
    ui.checkbox.return_value = has_end
    ui.button.return_value = compare


def test_scenario_controls_cover_one_off_recurring_end_and_invalid_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    client.list_recurring.return_value = Page[object](
        items=(
            _recurring("pending", status=RecurrenceStatus.PENDING),
            _recurring("other-account", account_id="account-2"),
        ),
        limit=100,
        offset=0,
        total=2,
    )
    _configure_scenario_ui(ui, scenario_type=FinancialScenarioType.ONE_OFF_PURCHASE)
    page.render_scenarios(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        categories=(_category(),),
        as_of=AS_OF,
    )
    client.evaluate_scenario.assert_not_called()

    _configure_scenario_ui(
        ui,
        scenario_type=FinancialScenarioType.NEW_SUBSCRIPTION,
        name=" ",
        compare=True,
        has_end=True,
    )
    page.render_scenarios(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        categories=(_category(),),
        as_of=AS_OF,
    )
    _configure_scenario_ui(
        ui,
        scenario_type=FinancialScenarioType.NEW_SUBSCRIPTION,
        paydays=[],
        compare=True,
    )
    page.render_scenarios(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        categories=(_category(),),
        as_of=AS_OF,
    )
    assert ui.error.call_count == 2


def test_scenario_controls_require_reviewed_evidence_and_compare_valid_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    result_display = MagicMock()
    monkeypatch.setattr(page, "_scenario_result", result_display)
    client.list_recurring.return_value = Page[object](
        items=(), limit=100, offset=0, total=0
    )
    _configure_scenario_ui(
        ui,
        scenario_type=FinancialScenarioType.CANCELLED_SUBSCRIPTION,
        compare=True,
    )
    page.render_scenarios(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        categories=(_category(),),
        as_of=AS_OF,
    )
    ui.warning.assert_called_once()
    ui.error.assert_called_once()

    client.list_recurring.return_value = Page[object](
        items=(_recurring("confirmed"),), limit=100, offset=0, total=1
    )
    client.evaluate_scenario.return_value = object()
    _configure_scenario_ui(
        ui,
        scenario_type=FinancialScenarioType.CANCELLED_SUBSCRIPTION,
        compare=True,
    )
    page.render_scenarios(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        categories=(_category(),),
        as_of=AS_OF,
    )
    cancelled = client.evaluate_scenario.call_args.args[0].scenario
    assert cancelled.recurring_payment_id == "confirmed"
    assert cancelled.amount is None

    _configure_scenario_ui(
        ui,
        scenario_type=FinancialScenarioType.CATEGORY_SPENDING_REDUCTION,
        compare=True,
    )
    page.render_scenarios(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        categories=(),
        as_of=AS_OF,
    )
    assert ui.warning.call_count == 2

    _configure_scenario_ui(
        ui,
        scenario_type=FinancialScenarioType.CATEGORY_SPENDING_REDUCTION,
        compare=True,
    )
    page.render_scenarios(
        client,
        profile_id="synthetic-profile",
        accounts=(_account(),),
        categories=(_category(), _category("inactive", active=False)),
        as_of=AS_OF,
    )
    category = client.evaluate_scenario.call_args.args[0].scenario
    assert category.category_id == "food"
    assert category.frequency is RecurrenceFrequency.MONTHLY
    assert result_display.call_count == 2


def test_scenario_result_preserves_baseline_and_shows_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    ui.columns.return_value = (MagicMock(), MagicMock())
    monkeypatch.setattr(page, "scenario_balance_chart", MagicMock(return_value={}))
    result = SimpleNamespace(
        balance_effect=SimpleNamespace(
            currency=Currency.GBP, end_balance_difference=Decimal("-100.00")
        ),
        safe_spending_effect=SimpleNamespace(difference=Decimal("-10.00")),
        warnings=(ScenarioComparisonWarningCode.BASELINE_FORECAST_LIMITATION,),
    )
    page._scenario_result(result)  # type: ignore[arg-type]
    ui.success.assert_called_once()
    ui.warning.assert_called_once()


def _alert(
    identifier: str,
    *,
    status: AnomalyReviewStatus | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        transaction_id=identifier,
        account_id="account-1",
        transaction_date=AS_OF,
        label=AnomalyUserLabel.NEEDS_REVIEW,
        score=Decimal("0.800000"),
        signals=(
            AnomalySignal(
                code=AnomalySignalCode.UNUSUALLY_LARGE_TRANSACTION,
                score=Decimal("0.800000"),
                observed_amount=Decimal("500.00"),
                reference_amount=Decimal("50.00"),
            ),
        ),
        review_status=status,
    )


def test_anomaly_controls_handle_off_empty_scope_and_no_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    empty = MagicMock()
    monkeypatch.setattr(page, "render_empty_state", empty)
    client = MagicMock()
    ui.multiselect.return_value = ["account-1"]
    ui.toggle.return_value = False
    page.render_anomalies(
        client, profile_id="synthetic-profile", accounts=(_account(),), as_of=AS_OF
    )
    empty.assert_called_once()

    ui.toggle.return_value = True
    ui.multiselect.return_value = []
    page.render_anomalies(
        client, profile_id="synthetic-profile", accounts=(_account(),), as_of=AS_OF
    )
    ui.error.assert_called_once()

    ui.multiselect.return_value = ["account-1"]
    client.detect_anomalies.return_value = SimpleNamespace(
        mode=AnomalyDetectionMode.RULES_ONLY,
        alerts=(),
        reference_transaction_count=0,
        warnings=(AnomalyWarningCode.INSUFFICIENT_HISTORY,),
    )
    page.render_anomalies(
        client, profile_id="synthetic-profile", accounts=(_account(),), as_of=AS_OF
    )
    assert empty.call_count == 2
    ui.warning.assert_called_once()


@pytest.mark.parametrize(
    ("expected_clicked", "unusual_clicked", "expected_action"),
    [
        (False, False, None),
        (True, False, AnomalyFeedbackAction.EXPECTED_ACTIVITY),
        (False, True, AnomalyFeedbackAction.CONFIRMED_UNUSUAL),
    ],
)
def test_anomaly_queue_explains_and_records_explicit_feedback(
    monkeypatch: pytest.MonkeyPatch,
    expected_clicked: bool,
    unusual_clicked: bool,
    expected_action: AnomalyFeedbackAction | None,
) -> None:
    ui = _ui(monkeypatch)
    ui.toggle.return_value = True
    ui.multiselect.return_value = ["account-1"]
    expected = MagicMock()
    expected.button.return_value = expected_clicked
    unusual = MagicMock()
    unusual.button.return_value = unusual_clicked
    ui.columns.return_value = (expected, unusual)
    client = MagicMock()
    client.detect_anomalies.return_value = SimpleNamespace(
        mode=AnomalyDetectionMode.RULES_AND_MODEL,
        alerts=(
            _alert("reviewed", status=AnomalyReviewStatus.REVIEWED),
            _alert("new"),
        ),
        reference_transaction_count=20,
        warnings=(),
    )
    client.review_anomaly.return_value = SimpleNamespace(
        status=AnomalyReviewStatus.DISMISSED
    )

    page.render_anomalies(
        client, profile_id="synthetic-profile", accounts=(_account(),), as_of=AS_OF
    )

    if expected_action is None:
        client.review_anomaly.assert_not_called()
    else:
        request = client.review_anomaly.call_args.args[0]
        assert request.transaction_id == "new"
        assert request.action is expected_action
    assert ui.write.call_count == 2
    ui.success.assert_called()


def test_model_information_handles_empty_active_and_inactive_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    empty = MagicMock()
    monkeypatch.setattr(page, "render_empty_state", empty)
    client = MagicMock()
    ui.selectbox.return_value = None
    client.list_models.return_value = Page[object](
        items=(), limit=100, offset=0, total=0
    )
    page.render_models(client)
    empty.assert_called_once()

    ui.selectbox.return_value = ModelTask.CASH_FLOW_FORECASTING
    metric = SimpleNamespace(
        name="mae",
        evaluation_slice="chronological_holdout",
        value=Decimal("5.00"),
        unit=ModelMetricUnit.GBP,
    )
    common = {
        "model_name": "synthetic-model",
        "model_version": "1.0",
        "task": ModelTask.CASH_FLOW_FORECASTING,
        "model_type": "synthetic",
        "training_start_date": date(2026, 1, 1),
        "training_end_date": date(2026, 8, 1),
        "activation_eligible": True,
    }
    client.list_models.return_value = Page[object](
        items=(
            SimpleNamespace(**common, is_active=True, metrics=(metric,)),
            SimpleNamespace(**common, is_active=False, metrics=()),
        ),
        limit=100,
        offset=0,
        total=2,
    )
    ui.container.return_value = nullcontext()
    page.render_models(client)
    client.list_models.assert_called_with(ModelTask.CASH_FLOW_FORECASTING)
    assert ui.metric.call_count == 2
    ui.dataframe.assert_called_once()
