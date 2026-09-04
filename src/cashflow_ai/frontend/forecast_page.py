"""Streamlit recurring-payment review and balance-forecast interface."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import streamlit as st

from cashflow_ai.frontend.client import ApiClientError
from cashflow_ai.frontend.components import (
    loading_state,
    render_empty_state,
    render_error,
    render_forecast_disclaimer,
)
from cashflow_ai.frontend.forecast_workflow import (
    forecast_chart,
    forecast_request,
    recurrence_request,
)
from cashflow_ai.frontend.planning_page import (
    PlanningApi,
    render_anomalies,
    render_budgets_and_goals,
    render_models,
    render_scenarios,
)
from cashflow_ai.frontend.session import FrontendSessionState
from cashflow_ai.frontend.transaction_workflow import money_text
from cashflow_ai.schemas.api import AccountResponse, Page, UserProfileResponse
from cashflow_ai.schemas.api_decisions import (
    BalanceForecastRequest,
    ForecastEvaluationRequest,
    RecurrenceDetectionRequest,
)
from cashflow_ai.schemas.forecast_models import (
    ForecastModelComparison,
    ForecastTrainingResult,
)
from cashflow_ai.schemas.forecast_paths import (
    BalanceForecastPath,
    ForecastPathWarningCode,
)
from cashflow_ai.schemas.recurrence import (
    RecurrenceReview,
    RecurrenceReviewAction,
    RecurrenceReviewResult,
    RecurrenceStatus,
    RecurringPaymentCandidate,
)


class ForecastApi(PlanningApi, Protocol):
    """Typed local API surface used by recurring and forecast views."""

    def current_profile(self) -> UserProfileResponse:
        """Return the local profile."""
        ...

    def list_accounts(self, profile_id: str) -> Page[AccountResponse]:
        """Return accounts owned by the local profile."""
        ...

    def detect_recurring(
        self, request: RecurrenceDetectionRequest
    ) -> Page[RecurringPaymentCandidate]:
        """Refresh cutoff-safe recurring candidates."""
        ...

    def review_recurring(self, request: RecurrenceReview) -> RecurrenceReviewResult:
        """Apply one explicit recurring-series review."""
        ...

    def balance_forecast(self, request: BalanceForecastRequest) -> BalanceForecastPath:
        """Calculate an uncertainty-aware balance path."""
        ...

    def evaluate_forecast(
        self, request: ForecastEvaluationRequest
    ) -> ForecastTrainingResult:
        """Evaluate the candidate model against chronological baselines."""
        ...


def _render_recurring(
    client: ForecastApi,
    *,
    profile_id: str,
    account_names: dict[str, str],
    as_of: date,
) -> None:
    st.subheader("Recurring payments")
    st.caption(
        "Patterns are suggestions until you confirm them. Rejected patterns are not "
        "used as known future payments."
    )
    if st.button("Refresh recurring patterns"):
        with loading_state("Checking verified history for recurring patterns…"):
            result = client.detect_recurring(
                recurrence_request(
                    profile_id=profile_id,
                    as_of_date=as_of,
                )
            )
    else:
        result = client.list_recurring(profile_id)
    if not result.items:
        render_empty_state(
            "No recurring patterns detected",
            "More verified history or repeated payments may be needed.",
        )
        return

    for item in result.items:
        with st.container(border=True):
            first, second, third = st.columns(3)
            first.markdown(f"**{item.merchant_group}**")
            first.caption(
                f"{account_names.get(item.account_id, 'Unknown account')} · "
                f"{item.financial_role.value.replace('_', ' ')}"
            )
            second.metric(
                item.frequency.value.title(),
                money_text(item.expected_amount, item.currency.value),
            )
            third.metric("Next expected", item.next_expected_date.isoformat())
            st.caption(
                f"Confidence {item.confidence:.0%} · "
                f"{len(item.occurrence_dates)} verified occurrences · "
                f"status {item.status.value}"
            )
            if item.status is not RecurrenceStatus.PENDING:
                continue
            confirm, reject = st.columns(2)
            action: RecurrenceReviewAction | None = None
            if confirm.button("Confirm", key=f"recurrence-confirm-{item.candidate_id}"):
                action = RecurrenceReviewAction.CONFIRM
            if reject.button("Reject", key=f"recurrence-reject-{item.candidate_id}"):
                action = RecurrenceReviewAction.CANCEL
            if action is not None:
                reviewed = client.review_recurring(
                    RecurrenceReview(
                        user_profile_id=profile_id,
                        candidate_id=item.candidate_id,
                        action=action,
                        reviewed_at=datetime.now(UTC),
                    )
                )
                st.success(f"Recurring pattern marked {reviewed.status.value}.")


def _render_model_information(comparison: ForecastModelComparison) -> None:
    st.markdown("**Model information**")
    selected, baseline, samples = st.columns(3)
    selected.metric("Selected", comparison.selected_model.value.replace("_", " "))
    baseline.metric("Best baseline", comparison.best_baseline.value.replace("_", " "))
    samples.metric("Training weeks", comparison.training_sample_count)
    st.caption(comparison.selection_reason)
    if comparison.final_test is None:
        st.caption("No held-out score is claimed because model history is limited.")
        return
    st.caption(
        "Held-out model error · "
        f"MAE {comparison.final_test.mae:.2f} · "
        f"RMSE {comparison.final_test.rmse:.2f} · "
        f"bias {comparison.final_test.bias:.2f}"
    )


def _render_forecast_result(
    path: BalanceForecastPath, comparison: ForecastModelComparison
) -> None:
    currency = path.opening_balance.currency.value
    expected, lower, upper = st.columns(3)
    expected.metric(
        "Expected final balance", money_text(path.expected_final_balance, currency)
    )
    lower.metric("Lower estimate", money_text(path.lower_final_balance, currency))
    upper.metric("Upper estimate", money_text(path.upper_final_balance, currency))
    st.vega_lite_chart(forecast_chart(path), use_container_width=True)
    st.caption(
        "The shaded range is an empirical estimate based on past errors; it is not "
        "a promised minimum or maximum."
    )

    source, cutoff, model = st.columns(3)
    source.metric("Current balance source", path.opening_balance.source.value)
    cutoff.metric("Data cutoff", path.plan.knowledge_cutoff_at.date().isoformat())
    model.metric("Selected model", path.selected_model.value.replace("_", " "))
    st.caption(
        f"Opening balance: {money_text(path.opening_balance.balance, currency)} as of "
        f"{path.opening_balance.as_of_date.isoformat()}."
    )
    _render_model_information(comparison)

    if ForecastPathWarningCode.LOW_CONFIDENCE_MODEL in path.warnings:
        st.warning(
            "Limited or weak history: the safe baseline is being used, or the "
            "uncertainty range has been widened."
        )
    if ForecastPathWarningCode.STALE_DATA in path.warnings:
        reasons = ", ".join(
            warning.value.replace("_", " ") for warning in path.freshness_warnings
        )
        st.warning(f"Stale or incomplete financial evidence: {reasons}.")
    if ForecastPathWarningCode.LIMITED_RESIDUAL_HISTORY in path.warnings:
        st.info("Few past forecast errors are available, so uncertainty is cautious.")

    st.markdown("**Upcoming confirmed flows**")
    if not path.recurring_occurrences:
        st.caption("No confirmed recurring cash flows fall inside this horizon.")
        return
    st.dataframe(
        [
            {
                "Date": item.occurrence_date.isoformat(),
                "Amount": money_text(item.signed_amount, currency),
                "Financial role": item.financial_role.value.replace("_", " "),
            }
            for item in path.recurring_occurrences
        ],
        hide_index=True,
        use_container_width=True,
    )


def _render_forecast(
    client: ForecastApi,
    *,
    profile_id: str,
    accounts: tuple[AccountResponse, ...],
    default_account_id: str | None,
) -> str:
    st.subheader("Balance forecast")
    account_names = {item.account_id: item.name for item in accounts}
    account_ids = tuple(account_names)
    selected_index = (
        account_ids.index(default_account_id)
        if default_account_id in account_ids
        else 0
    )
    account_id = st.selectbox(
        "Account",
        options=account_ids,
        index=selected_index,
        format_func=account_names.__getitem__,
    )
    latest_complete_date = datetime.now(UTC).date() - timedelta(days=1)
    as_of = st.date_input(
        "Use verified data through (completed UTC date)",
        value=latest_complete_date,
        max_value=latest_complete_date,
    )
    horizon = st.select_slider(
        "Forecast horizon",
        options=(14, 30, 60, 90),
        value=30,
        format_func=lambda days: f"{days} days",
    )
    payday_days = tuple(
        st.multiselect(
            "Usual income days",
            options=tuple(range(1, 29)),
            default=(1, 15),
            help=(
                "Used only for payday-distance features. Choose the closest usual "
                "days for irregular income; this does not create expected income."
            ),
        )
    )
    generate = st.button("Generate forecast", type="primary")
    if generate and not payday_days:
        st.error("Choose at least one usual income day before forecasting.")
    elif generate:
        request = forecast_request(
            profile_id=profile_id,
            account_id=account_id,
            as_of_date=as_of,
            horizon_days=horizon,
            payday_days=payday_days,
        )
        with loading_state("Evaluating models and simulating future balances…"):
            evaluation = client.evaluate_forecast(
                ForecastEvaluationRequest(
                    dataset_plan=request.dataset_plan,
                    model_policy=request.model_policy,
                )
            )
            path = client.balance_forecast(request)
        _render_forecast_result(path, evaluation.comparison)
    return account_id


def render_forecast_page(
    client: ForecastApi, session: FrontendSessionState
) -> FrontendSessionState:
    """Render review-gated recurrence and uncertainty-aware forecasts."""
    st.title("📈 Forecast & planning")
    render_forecast_disclaimer()
    try:
        profile = client.current_profile()
        accounts = client.list_accounts(profile.profile_id).items
        if not accounts:
            render_empty_state(
                "No local accounts",
                "Create an account and confirm a statement before forecasting.",
            )
            return session
        categories = client.list_categories().items
        latest_complete_date = datetime.now(UTC).date() - timedelta(days=1)
        as_of = st.date_input(
            "Recurring evidence date (completed UTC date)",
            value=latest_complete_date,
            max_value=latest_complete_date,
        )
        account_names = {item.account_id: item.name for item in accounts}
        recurring, forecasting, planning, scenarios, anomalies, models = st.tabs(
            (
                "Recurring payments",
                "Forecast",
                "Budgets & goals",
                "Scenarios",
                "Anomaly review",
                "Model evaluation",
            )
        )
        with recurring:
            _render_recurring(
                client,
                profile_id=profile.profile_id,
                account_names=account_names,
                as_of=as_of,
            )
        with forecasting:
            selected_account = _render_forecast(
                client,
                profile_id=profile.profile_id,
                accounts=accounts,
                default_account_id=session.account_id,
            )
        with planning:
            render_budgets_and_goals(
                client,
                profile_id=profile.profile_id,
                currency=profile.base_currency,
                accounts=accounts,
                categories=categories,
                as_of=as_of,
            )
        with scenarios:
            render_scenarios(
                client,
                profile_id=profile.profile_id,
                accounts=accounts,
                categories=categories,
                as_of=as_of,
            )
        with anomalies:
            render_anomalies(
                client,
                profile_id=profile.profile_id,
                accounts=accounts,
                as_of=as_of,
            )
        with models:
            render_models(client)
    except ApiClientError as error:
        render_error(error)
        return session
    return FrontendSessionState(
        selected_page=session.selected_page,
        user_profile_id=profile.profile_id,
        account_id=selected_account,
        privacy_notice_seen=True,
    )


__all__ = ["ForecastApi", "render_forecast_page"]
