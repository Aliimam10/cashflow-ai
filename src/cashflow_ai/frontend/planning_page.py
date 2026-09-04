"""Streamlit budgets, goals, scenarios, anomaly review, and model information."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

import streamlit as st

from cashflow_ai.frontend.components import loading_state, render_empty_state
from cashflow_ai.frontend.forecast_workflow import (
    complete_day_cutoff,
    forecast_monday_after,
)
from cashflow_ai.frontend.planning_workflow import (
    anomaly_request,
    calendar_month,
    monday_week,
    planning_request,
    scenario_balance_chart,
    scenario_request,
    signal_explanation,
)
from cashflow_ai.frontend.transaction_workflow import money_text
from cashflow_ai.schemas.anomalies import (
    AnomalyDetectionPlan,
    AnomalyDetectionResult,
    AnomalyFeedbackAction,
    AnomalyFeedbackRequest,
    AnomalyFeedbackResult,
)
from cashflow_ai.schemas.api import AccountResponse, Page
from cashflow_ai.schemas.api_decisions import PlanningApiRequest, ScenarioApiRequest
from cashflow_ai.schemas.categories import CategorySummary
from cashflow_ai.schemas.model_registry import ModelTask, RegisteredModel
from cashflow_ai.schemas.planning import (
    Budget,
    BudgetCreate,
    BudgetType,
    FinancialGoal,
    FinancialGoalCreate,
    FinancialGoalType,
    FinancialPlanningResult,
)
from cashflow_ai.schemas.recurrence import (
    RecurrenceFrequency,
    RecurrenceStatus,
    RecurringPaymentCandidate,
)
from cashflow_ai.schemas.scenarios import (
    FinancialScenario,
    FinancialScenarioComparison,
    FinancialScenarioType,
)
from cashflow_ai.schemas.transactions import Currency

_ONE_OFF_SCENARIOS = {
    FinancialScenarioType.ONE_OFF_PURCHASE,
    FinancialScenarioType.TRAVEL_EXPENSE,
}


class PlanningApi(Protocol):
    """Typed API surface consumed by the Commit 36 interface."""

    def create_budget(self, request: BudgetCreate) -> Budget:
        """Create a budget."""
        ...

    def list_budgets(self, profile_id: str, *, as_of_date: date) -> Page[Budget]:
        """List active budgets."""
        ...

    def create_goal(self, request: FinancialGoalCreate) -> FinancialGoal:
        """Create a financial goal."""
        ...

    def list_goals(self, profile_id: str) -> Page[FinancialGoal]:
        """List financial goals."""
        ...

    def evaluate_planning(self, request: PlanningApiRequest) -> FinancialPlanningResult:
        """Evaluate budgets, goals, and safe spending."""
        ...

    def evaluate_scenario(
        self, request: ScenarioApiRequest
    ) -> FinancialScenarioComparison:
        """Evaluate one isolated scenario."""
        ...

    def list_recurring(self, profile_id: str) -> Page[RecurringPaymentCandidate]:
        """List recurring candidates for cancellation scenarios."""
        ...

    def detect_anomalies(self, request: AnomalyDetectionPlan) -> AnomalyDetectionResult:
        """Run a review-only anomaly scan."""
        ...

    def review_anomaly(self, request: AnomalyFeedbackRequest) -> AnomalyFeedbackResult:
        """Save one explicit anomaly interpretation."""
        ...

    def list_models(self, task: ModelTask | None = None) -> Page[RegisteredModel]:
        """List aggregate model metadata."""
        ...

    def list_categories(self) -> Page[CategorySummary]:
        """List the controlled transaction-category taxonomy."""
        ...


def _amount(value: int | float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _render_budget_setup(
    client: PlanningApi,
    *,
    profile_id: str,
    currency: Currency,
    categories: tuple[CategorySummary, ...],
    as_of: date,
) -> None:
    st.markdown("**Create a budget**")
    budget_type = st.selectbox(
        "Budget type",
        options=tuple(BudgetType),
        format_func=lambda value: value.value.replace("_", " ").title(),
        key="budget-type",
    )
    amount = _amount(
        st.number_input(
            "Budget limit",
            min_value=0.0,
            value=100.0,
            step=10.0,
            key="budget-limit",
        )
    )
    period_date = st.date_input("Budget period", value=as_of, key="budget-period")
    category_id: str | None = None
    if budget_type is BudgetType.MONTHLY_CATEGORY:
        category_names = {item.id: item.name for item in categories if item.is_active}
        if not category_names:
            st.warning("No active transaction categories are available.")
        else:
            category_id = st.selectbox(
                "Budget category",
                options=tuple(category_names),
                format_func=category_names.__getitem__,
                key="budget-category",
            )
        period = calendar_month(period_date)
    else:
        period = monday_week(period_date)
    st.caption(f"Saved period: {period.start_date} to {period.end_date}")
    if st.button("Save budget", key="save-budget"):
        if budget_type is BudgetType.MONTHLY_CATEGORY and category_id is None:
            st.error("Choose an active category before saving this budget.")
            return
        created = client.create_budget(
            BudgetCreate(
                user_profile_id=profile_id,
                budget_type=budget_type,
                category_id=category_id,
                period=period,
                amount_limit=amount,
                currency=currency,
            )
        )
        st.success(f"Budget saved: {money_text(created.amount_limit, currency.value)}.")


def _render_goal_setup(
    client: PlanningApi,
    *,
    profile_id: str,
    accounts: tuple[AccountResponse, ...],
    as_of: date,
) -> None:
    st.markdown("**Create a goal**")
    account_names = {item.account_id: item.name for item in accounts}
    goal_type = st.selectbox(
        "Goal type",
        options=tuple(FinancialGoalType),
        format_func=lambda value: value.value.replace("_", " ").title(),
        key="goal-type",
    )
    account_id = st.selectbox(
        "Goal account",
        options=tuple(account_names),
        format_func=account_names.__getitem__,
        key="goal-account",
    )
    name = st.text_input("Goal name", value="Fictional goal", key="goal-name").strip()
    target = _amount(
        st.number_input(
            "Target amount",
            min_value=0.01,
            value=500.0,
            step=10.0,
            key="goal-target",
        )
    )
    current = Decimal("0.00")
    target_date: date | None = None
    if goal_type is FinancialGoalType.SAVINGS_TARGET:
        current = _amount(
            st.number_input(
                "Already saved",
                min_value=0.0,
                value=0.0,
                step=10.0,
                key="goal-current",
            )
        )
        target_date = st.date_input(
            "Target date",
            value=as_of + timedelta(days=90),
            min_value=as_of + timedelta(days=1),
            key="goal-date",
        )
    if st.button("Save goal", key="save-goal"):
        if not name:
            st.error("Enter a goal name before saving.")
            return
        created = client.create_goal(
            FinancialGoalCreate(
                user_profile_id=profile_id,
                account_id=account_id,
                goal_type=goal_type,
                name=name,
                target_amount=target,
                current_amount=current,
                target_date=target_date,
                as_of_date=as_of,
            )
        )
        st.success(f"Goal saved: {created.name}.")


def _planning_result(result: FinancialPlanningResult) -> None:
    safe = result.safe_spending
    st.metric(
        "Estimated safe weekly spending",
        money_text(safe.safe_weekly_spending, safe.currency.value),
        help=f"Limited by {safe.limiting_factor.value.replace('_', ' ')}.",
    )
    st.caption(
        "This estimate uses the cautious forecast balance, active budgets, and goal "
        "requirements. It is not financial advice."
    )
    if result.budgets:
        st.markdown("**Budget progress**")
        st.dataframe(
            [
                {
                    "Type": item.budget.budget_type.value.replace("_", " "),
                    "Limit": money_text(
                        item.budget.amount_limit, result.currency.value
                    ),
                    "Used": (
                        "Unavailable"
                        if item.amount_used is None
                        else money_text(item.amount_used, result.currency.value)
                    ),
                    "Remaining": (
                        "Unavailable"
                        if item.amount_remaining is None
                        else money_text(item.amount_remaining, result.currency.value)
                    ),
                    "Projected use": (
                        "Unavailable"
                        if item.projected_use is None
                        else money_text(item.projected_use, result.currency.value)
                    ),
                }
                for item in result.budgets
            ],
            hide_index=True,
            use_container_width=True,
        )
    if result.goals:
        st.markdown("**Goal progress**")
        st.dataframe(
            [
                {
                    "Goal": item.goal.name,
                    "Type": item.goal.goal_type.value.replace("_", " "),
                    "Remaining": money_text(
                        item.remaining_amount, result.currency.value
                    ),
                    "Required monthly": (
                        "Not applicable"
                        if item.required_monthly_contribution is None
                        else money_text(
                            item.required_monthly_contribution, result.currency.value
                        )
                    ),
                    "Projected shortfall": (
                        "Not applicable"
                        if item.projected_shortfall is None
                        else money_text(item.projected_shortfall, result.currency.value)
                    ),
                }
                for item in result.goals
            ],
            hide_index=True,
            use_container_width=True,
        )
    for warning in result.warnings:
        st.warning(warning.code.value.replace("_", " ").capitalize())


def render_budgets_and_goals(
    client: PlanningApi,
    *,
    profile_id: str,
    currency: Currency,
    accounts: tuple[AccountResponse, ...],
    categories: tuple[CategorySummary, ...],
    as_of: date,
) -> None:
    """Render persisted setup plus on-demand planning progress."""
    setup_budget, setup_goal = st.columns(2)
    with setup_budget:
        _render_budget_setup(
            client,
            profile_id=profile_id,
            currency=currency,
            categories=categories,
            as_of=as_of,
        )
    with setup_goal:
        _render_goal_setup(
            client, profile_id=profile_id, accounts=accounts, as_of=as_of
        )

    budgets = client.list_budgets(profile_id, as_of_date=as_of).items
    goals = client.list_goals(profile_id).items
    st.caption(f"Active budgets: {len(budgets)} · Saved goals: {len(goals)}")
    account_names = {item.account_id: item.name for item in accounts}
    selected = tuple(
        st.multiselect(
            "Planning accounts",
            options=tuple(account_names),
            default=tuple(account_names),
            format_func=account_names.__getitem__,
            key="planning-accounts",
        )
    )
    horizon = st.select_slider(
        "Planning horizon",
        options=(30, 60, 90),
        value=90,
        format_func=lambda value: f"{value} days",
        key="planning-horizon",
    )
    paydays = tuple(
        st.multiselect(
            "Planning income days",
            options=tuple(range(1, 29)),
            default=(1, 15),
            key="planning-paydays",
        )
    )
    if st.button("Evaluate plan", type="primary", key="evaluate-plan"):
        if not selected or not paydays:
            st.error("Choose at least one account and one income day.")
            return
        with loading_state("Rebuilding forecasts and evaluating the plan…"):
            result = client.evaluate_planning(
                planning_request(
                    profile_id=profile_id,
                    account_ids=selected,
                    as_of_date=as_of,
                    horizon_days=horizon,
                    payday_days=paydays,
                )
            )
        _planning_result(result)


def _scenario_definition(
    *,
    profile_id: str,
    account_id: str,
    scenario_type: FinancialScenarioType,
    name: str,
    start: date,
    end: date | None,
    amount: Decimal | None,
    frequency: RecurrenceFrequency | None,
    category_id: str | None,
    recurring_payment_id: str | None,
) -> FinancialScenario:
    return FinancialScenario(
        scenario_id="temporary-ui-scenario",
        user_profile_id=profile_id,
        account_id=account_id,
        scenario_type=scenario_type,
        name=name,
        start_date=start,
        end_date=end,
        amount=amount,
        frequency=frequency,
        category_id=category_id,
        recurring_payment_id=recurring_payment_id,
    )


def _scenario_result(result: FinancialScenarioComparison) -> None:
    currency = result.balance_effect.currency.value
    st.success("Hypothetical comparison only — no transactions or plans were changed.")
    st.vega_lite_chart(scenario_balance_chart(result), use_container_width=True)
    balance, safe = st.columns(2)
    balance.metric(
        "Ending-balance difference",
        money_text(result.balance_effect.end_balance_difference, currency),
    )
    safe.metric(
        "Safe-weekly-spending difference",
        money_text(result.safe_spending_effect.difference, currency),
    )
    for warning in result.warnings:
        st.warning(warning.value.replace("_", " ").capitalize())


def render_scenarios(
    client: PlanningApi,
    *,
    profile_id: str,
    accounts: tuple[AccountResponse, ...],
    categories: tuple[CategorySummary, ...],
    as_of: date,
) -> None:
    """Render all supported non-persistent scenario shapes."""
    st.caption("Scenarios are private experiments and never change your transactions.")
    account_names = {item.account_id: item.name for item in accounts}
    account_id = st.selectbox(
        "Scenario account",
        options=tuple(account_names),
        format_func=account_names.__getitem__,
        key="scenario-account",
    )
    scenario_type = st.selectbox(
        "Scenario type",
        options=tuple(FinancialScenarioType),
        format_func=lambda value: value.value.replace("_", " ").title(),
        key="scenario-type",
    )
    name = st.text_input(
        "Scenario name", value="Fictional what-if", key="scenario-name"
    )
    horizon = st.select_slider(
        "Scenario horizon",
        options=(30, 60, 90),
        value=90,
        key="scenario-horizon",
    )
    start_default = forecast_monday_after(complete_day_cutoff(as_of))
    start = st.date_input("Scenario start", value=start_default, key="scenario-start")
    paydays = tuple(
        st.multiselect(
            "Scenario income days",
            options=tuple(range(1, 29)),
            default=(1, 15),
            key="scenario-paydays",
        )
    )
    amount: Decimal | None = None
    frequency: RecurrenceFrequency | None = None
    category_id: str | None = None
    recurring_payment_id: str | None = None
    end: date | None = None
    confirmed = tuple(
        item
        for item in client.list_recurring(profile_id).items
        if item.status is RecurrenceStatus.CONFIRMED and item.account_id == account_id
    )
    if scenario_type is FinancialScenarioType.CANCELLED_SUBSCRIPTION:
        labels = {item.candidate_id: item.merchant_group for item in confirmed}
        if labels:
            recurring_payment_id = st.selectbox(
                "Confirmed subscription",
                options=tuple(labels),
                format_func=labels.__getitem__,
                key="scenario-recurring",
            )
        else:
            st.warning("No confirmed recurring payment is available for this account.")
    else:
        amount = _amount(
            st.number_input(
                "Scenario amount",
                min_value=0.01,
                value=100.0,
                step=10.0,
                key="scenario-amount",
            )
        )
        if scenario_type not in _ONE_OFF_SCENARIOS:
            frequency = st.selectbox(
                "Scenario frequency",
                options=tuple(RecurrenceFrequency),
                format_func=lambda value: value.value.title(),
                key="scenario-frequency",
            )
            if st.checkbox("Set a scenario end date", key="scenario-has-end"):
                end = st.date_input(
                    "Scenario end", value=start + timedelta(days=28), key="scenario-end"
                )
        if scenario_type is FinancialScenarioType.CATEGORY_SPENDING_REDUCTION:
            category_names = {
                item.id: item.name for item in categories if item.is_active
            }
            if category_names:
                category_id = st.selectbox(
                    "Reduced category",
                    options=tuple(category_names),
                    format_func=category_names.__getitem__,
                    key="scenario-category",
                )
            else:
                st.warning("No active category is available for this scenario.")
    if st.button("Compare scenario", type="primary", key="compare-scenario"):
        if not name.strip() or not paydays:
            st.error("Enter a scenario name and at least one income day.")
            return
        if (
            scenario_type is FinancialScenarioType.CANCELLED_SUBSCRIPTION
            and recurring_payment_id is None
        ) or (
            scenario_type is FinancialScenarioType.CATEGORY_SPENDING_REDUCTION
            and category_id is None
        ):
            st.error("Review the related payment before using this scenario.")
            return
        scenario = _scenario_definition(
            profile_id=profile_id,
            account_id=account_id,
            scenario_type=scenario_type,
            name=name.strip(),
            start=start,
            end=end,
            amount=amount,
            frequency=frequency,
            category_id=category_id,
            recurring_payment_id=recurring_payment_id,
        )
        with loading_state("Building matched baseline and hypothetical paths…"):
            result = client.evaluate_scenario(
                scenario_request(
                    profile_id=profile_id,
                    account_id=account_id,
                    as_of_date=as_of,
                    horizon_days=horizon,
                    payday_days=paydays,
                    scenario=scenario,
                )
            )
        _scenario_result(result)


def render_anomalies(
    client: PlanningApi,
    *,
    profile_id: str,
    accounts: tuple[AccountResponse, ...],
    as_of: date,
) -> None:
    """Render a careful, feedback-enabled anomaly review queue."""
    st.caption(
        "These are unusual-pattern suggestions, not fraud findings. Confirmed "
        "feedback does not change transactions or train a model automatically."
    )
    account_names = {item.account_id: item.name for item in accounts}
    selected = tuple(
        st.multiselect(
            "Anomaly accounts",
            options=tuple(account_names),
            default=tuple(account_names),
            format_func=account_names.__getitem__,
            key="anomaly-accounts",
        )
    )
    if not st.toggle("Run anomaly scan", key="anomaly-scan"):
        render_empty_state(
            "Anomaly scan is off",
            "Turn it on when you want CashFlow AI to check your transaction history.",
        )
        return
    if not selected:
        st.error("Choose at least one account for the anomaly scan.")
        return
    plan = anomaly_request(
        profile_id=profile_id,
        account_ids=selected,
        as_of_date=as_of,
    )
    with loading_state("Checking transactions for unusual activity…"):
        result = client.detect_anomalies(plan)
    st.caption(
        f"Mode: {result.mode.value.replace('_', ' ')} · "
        f"alerts: {len(result.alerts)} · reference rows: "
        f"{result.reference_transaction_count}"
    )
    for warning in result.warnings:
        st.warning(warning.value.replace("_", " ").capitalize())
    if not result.alerts:
        render_empty_state("No current review suggestions", "No anomaly rule fired.")
        return
    for alert in result.alerts:
        with st.container(border=True):
            st.markdown(f"**{alert.label.value} · {alert.transaction_date}**")
            st.caption(
                f"{account_names.get(alert.account_id, 'Unknown account')} · "
                f"review strength {alert.score:.0%}"
            )
            for signal in alert.signals:
                st.write(signal_explanation(signal))
            if alert.review_status is not None:
                st.success(
                    f"Saved review: {alert.review_status.value.replace('_', ' ')}."
                )
                continue
            expected, unusual = st.columns(2)
            action: AnomalyFeedbackAction | None = None
            if expected.button(
                "This was expected", key=f"anomaly-expected-{alert.transaction_id}"
            ):
                action = AnomalyFeedbackAction.EXPECTED_ACTIVITY
            if unusual.button(
                "Keep as unusual", key=f"anomaly-unusual-{alert.transaction_id}"
            ):
                action = AnomalyFeedbackAction.CONFIRMED_UNUSUAL
            if action is not None:
                saved = client.review_anomaly(
                    AnomalyFeedbackRequest(
                        plan=plan,
                        transaction_id=alert.transaction_id,
                        action=action,
                    )
                )
                st.success(f"Feedback saved as {saved.status.value}.")


def render_models(client: PlanningApi) -> None:
    """Render aggregate model evaluation and activation metadata only."""
    task = st.selectbox(
        "Model task",
        options=(None, *tuple(ModelTask)),
        format_func=lambda value: (
            "All tasks" if value is None else value.value.replace("_", " ").title()
        ),
        key="model-task",
    )
    models = client.list_models(task).items
    if not models:
        render_empty_state(
            "No registered model metadata",
            "Run and explicitly register an eligible local model first.",
        )
        return
    for model in models:
        with st.container(border=True):
            st.markdown(f"**{model.model_name} · {model.model_version}**")
            st.caption(
                f"{model.task.value.replace('_', ' ')} · {model.model_type} · "
                f"training {model.training_start_date} to {model.training_end_date}"
            )
            status = "Active" if model.is_active else "Inactive"
            st.metric("Status", status, help=f"Eligible: {model.activation_eligible}")
            if model.metrics:
                st.dataframe(
                    [
                        {
                            "Metric": metric.name,
                            "Slice": metric.evaluation_slice,
                            "Value": str(metric.value),
                            "Unit": metric.unit.value,
                        }
                        for metric in model.metrics
                    ],
                    hide_index=True,
                    use_container_width=True,
                )


__all__ = [
    "PlanningApi",
    "render_anomalies",
    "render_budgets_and_goals",
    "render_models",
    "render_scenarios",
]
