"""Tests for the recurring-payment and balance-forecast Streamlit page."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

import cashflow_ai.frontend.forecast_page as page
from cashflow_ai.frontend.client import ApiClientError, ApiClientErrorCode
from cashflow_ai.frontend.forecast_workflow import forecast_request
from cashflow_ai.frontend.navigation import PageId
from cashflow_ai.frontend.session import FrontendSessionState
from cashflow_ai.schemas.accounts import AccountType
from cashflow_ai.schemas.api import AccountResponse, Page, UserProfileResponse
from cashflow_ai.schemas.forecast_models import ForecastModelName
from cashflow_ai.schemas.forecast_paths import (
    BalanceForecastPath,
    DailyBalancePathPoint,
    ForecastIntervalMethod,
    ForecastOpeningBalance,
    ForecastPathWarningCode,
    ForecastScenario,
    RecurringForecastOccurrence,
    WeeklySpendingPath,
)
from cashflow_ai.schemas.forecasting import ForecastBaselineName
from cashflow_ai.schemas.freshness import FreshnessWarningCode
from cashflow_ai.schemas.recurrence import (
    RecurrenceFrequency,
    RecurrenceStatus,
    RecurringPaymentCandidate,
)
from cashflow_ai.schemas.statements import BalanceSnapshotSource
from cashflow_ai.schemas.transactions import Currency, Direction, FinancialRole

NOW = datetime(2026, 8, 30, 20, tzinfo=UTC)


def _profile() -> UserProfileResponse:
    return UserProfileResponse(
        profile_id="synthetic-profile",
        display_name="Synthetic User",
        base_currency=Currency.GBP,
        timezone="UTC",
        created_at=NOW,
        updated_at=NOW,
    )


def _account(identifier: str = "synthetic-account") -> AccountResponse:
    return AccountResponse(
        account_id=identifier,
        user_profile_id="synthetic-profile",
        name=f"Account {identifier}",
        account_type=AccountType.CURRENT,
        currency=Currency.GBP,
        institution_label="Example Bank",
        is_active=True,
        created_at=NOW,
    )


def _candidate(status: RecurrenceStatus) -> RecurringPaymentCandidate:
    return RecurringPaymentCandidate(
        candidate_id=f"candidate-{status.value}",
        account_id="synthetic-account",
        merchant_group="Synthetic subscription",
        currency=Currency.GBP,
        direction=Direction.OUTFLOW,
        financial_role=FinancialRole.EXPENSE,
        expected_amount=Decimal("12.00"),
        frequency=RecurrenceFrequency.MONTHLY,
        interval_days=30,
        occurrence_dates=(date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)),
        next_expected_date=date(2026, 9, 1),
        confidence=0.9,
        covered_missed_count=0,
        status=status,
        evidence_as_of_date=date(2026, 8, 30),
        knowledge_cutoff_at=NOW,
    )


def _path(*, occurrences: bool = True) -> BalanceForecastPath:
    request = forecast_request(
        profile_id="synthetic-profile",
        account_id="synthetic-account",
        as_of_date=date(2026, 8, 30),
        horizon_days=1,
        payday_days=(1, 15),
    )
    start = request.path_plan.forecast_start
    point = DailyBalancePathPoint(
        forecast_date=start,
        expected_discretionary_outflow=Decimal("10.00"),
        recurring_net_flow=Decimal("-12.00") if occurrences else Decimal("0"),
        scenario_adjustment=Decimal("0"),
        expected_balance=Decimal("978.00"),
        lower_balance=Decimal("950.00"),
        upper_balance=Decimal("1000.00"),
    )
    recurring = (
        (
            RecurringForecastOccurrence(
                candidate_id="confirmed-series",
                occurrence_date=start,
                signed_amount=Decimal("-12.00"),
                financial_role=FinancialRole.EXPENSE,
                known_at=request.path_plan.knowledge_cutoff_at,
            ),
        )
        if occurrences
        else ()
    )
    return BalanceForecastPath(
        plan=request.path_plan,
        scenario=ForecastScenario(),
        opening_balance=ForecastOpeningBalance(
            balance=Decimal("1000.00"),
            currency=Currency.GBP,
            as_of_date=date(2026, 8, 30),
            recorded_at=request.path_plan.knowledge_cutoff_at,
            source=BalanceSnapshotSource.MANUAL,
        ),
        selected_model=ForecastModelName.HIST_GRADIENT_BOOSTING,
        interval_method=ForecastIntervalMethod.RESIDUAL_BOOTSTRAP,
        widening_multiplier=Decimal("2"),
        warnings=(
            ForecastPathWarningCode.LOW_CONFIDENCE_MODEL,
            ForecastPathWarningCode.STALE_DATA,
            ForecastPathWarningCode.LIMITED_RESIDUAL_HISTORY,
        ),
        freshness_warnings=(FreshnessWarningCode.BALANCE_STALE,),
        recurring_occurrences=recurring,
        weekly_spending=(
            WeeklySpendingPath(
                week_start=start,
                week_end=start + timedelta(days=6),
                expected_discretionary_spending=Decimal("70"),
                lower_discretionary_spending=Decimal("50"),
                upper_discretionary_spending=Decimal("90"),
            ),
        ),
        daily_balances=(point,),
        interval_performance=None,
        expected_final_balance=point.expected_balance,
        lower_final_balance=point.lower_balance,
        upper_final_balance=point.upper_balance,
    )


def _ui(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    ui = MagicMock()
    ui.container.return_value = nullcontext()
    ui.tabs.return_value = (nullcontext(), nullcontext())
    monkeypatch.setattr(page, "st", ui)
    monkeypatch.setattr(page, "loading_state", lambda message: nullcontext())
    return ui


def _comparison(*, scored: bool = False) -> MagicMock:
    comparison = MagicMock()
    comparison.selected_model = ForecastBaselineName.RECENT_ROLLING_MEAN
    comparison.best_baseline = ForecastBaselineName.RECENT_ROLLING_MEAN
    comparison.training_sample_count = 6
    comparison.selection_reason = "Synthetic candidate did not clear every gate."
    comparison.final_test = None
    if scored:
        comparison.final_test = MagicMock(
            mae=Decimal("8.00"),
            rmse=Decimal("10.00"),
            bias=Decimal("-1.00"),
        )
    return comparison


def test_recurring_empty_confirm_reject_and_resolved_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    account_names = {"synthetic-account": "Synthetic current"}
    ui.button.return_value = False
    client.list_recurring.return_value = Page[RecurringPaymentCandidate](
        items=(), limit=100, offset=0, total=0
    )
    empty = MagicMock()
    monkeypatch.setattr(page, "render_empty_state", empty)
    page._render_recurring(
        client,
        profile_id="synthetic-profile",
        account_names=account_names,
        as_of=date(2026, 8, 30),
    )
    empty.assert_called_once()

    client.detect_recurring.return_value = Page[RecurringPaymentCandidate](
        items=(_candidate(RecurrenceStatus.PENDING),), limit=100, offset=0, total=1
    )
    confirm = MagicMock()
    reject = MagicMock()
    confirm.button.return_value = True
    reject.button.return_value = False
    ui.columns.side_effect = [tuple(MagicMock() for _ in range(3)), (confirm, reject)]
    client.review_recurring.return_value.status = RecurrenceStatus.CONFIRMED
    ui.button.return_value = True
    page._render_recurring(
        client,
        profile_id="synthetic-profile",
        account_names=account_names,
        as_of=date(2026, 8, 30),
    )
    assert client.review_recurring.call_args.args[0].action.value == "confirm"

    confirm = MagicMock()
    reject = MagicMock()
    confirm.button.return_value = False
    reject.button.return_value = True
    ui.columns.side_effect = [tuple(MagicMock() for _ in range(3)), (confirm, reject)]
    ui.button.return_value = False
    client.list_recurring.return_value = client.detect_recurring.return_value
    page._render_recurring(
        client,
        profile_id="synthetic-profile",
        account_names=account_names,
        as_of=date(2026, 8, 30),
    )
    assert client.review_recurring.call_args.args[0].action.value == "cancel"

    confirm = MagicMock()
    reject = MagicMock()
    confirm.button.return_value = False
    reject.button.return_value = False
    ui.columns.side_effect = [tuple(MagicMock() for _ in range(3)), (confirm, reject)]
    page._render_recurring(
        client,
        profile_id="synthetic-profile",
        account_names=account_names,
        as_of=date(2026, 8, 30),
    )

    client.list_recurring.return_value = Page[RecurringPaymentCandidate](
        items=(
            _candidate(RecurrenceStatus.CONFIRMED),
            _candidate(RecurrenceStatus.CANCELLED),
        ),
        limit=100,
        offset=0,
        total=2,
    )
    ui.columns.side_effect = [
        tuple(MagicMock() for _ in range(3)),
        tuple(MagicMock() for _ in range(3)),
    ]
    page._render_recurring(
        client,
        profile_id="synthetic-profile",
        account_names={},
        as_of=date(2026, 8, 30),
    )


def test_forecast_controls_and_all_result_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    ui.selectbox.return_value = "account-2"
    ui.date_input.return_value = date(2026, 8, 30)
    ui.select_slider.return_value = 14
    ui.multiselect.return_value = [1, 15]
    ui.button.return_value = False
    client = MagicMock()
    accounts = (_account(), _account("account-2"))
    assert (
        page._render_forecast(
            client,
            profile_id="synthetic-profile",
            accounts=accounts,
            default_account_id="account-2",
        )
        == "account-2"
    )
    client.balance_forecast.assert_not_called()

    ui.button.return_value = True
    ui.multiselect.return_value = []
    page._render_forecast(
        client,
        profile_id="synthetic-profile",
        accounts=accounts,
        default_account_id="account-2",
    )
    client.evaluate_forecast.assert_not_called()
    ui.error.assert_called_once()

    ui.button.return_value = True
    ui.multiselect.return_value = [1, 15]
    client.balance_forecast.return_value = _path()
    client.evaluate_forecast.return_value.comparison = _comparison()
    ui.columns.return_value = tuple(MagicMock() for _ in range(3))
    page._render_forecast(
        client,
        profile_id="synthetic-profile",
        accounts=accounts,
        default_account_id="missing",
    )
    assert client.balance_forecast.call_args.args[0].path_plan.horizon_days == 14
    assert client.balance_forecast.call_args.args[0].dataset_plan.payday_days == (1, 15)
    assert client.evaluate_forecast.call_args.args[0].dataset_plan == (
        client.balance_forecast.call_args.args[0].dataset_plan
    )
    ui.warning.assert_called()
    ui.info.assert_called()
    ui.dataframe.assert_called_once()

    ui.reset_mock()
    ui.columns.return_value = tuple(MagicMock() for _ in range(3))
    quiet_path = _path(occurrences=False).model_copy(
        update={"warnings": (), "freshness_warnings": ()}
    )
    page._render_forecast_result(quiet_path, _comparison())
    ui.dataframe.assert_not_called()
    page._render_model_information(_comparison(scored=True))


def test_page_handles_no_accounts_success_and_safe_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    client.current_profile.return_value = _profile()
    client.list_accounts.return_value = Page[AccountResponse](
        items=(), limit=100, offset=0, total=0
    )
    empty = MagicMock()
    monkeypatch.setattr(page, "render_empty_state", empty)
    session = FrontendSessionState(selected_page=PageId.FORECAST_AND_PLANNING)
    assert page.render_forecast_page(client, session) == session
    empty.assert_called_once()

    client.list_accounts.return_value = Page[AccountResponse](
        items=(_account(),), limit=100, offset=0, total=1
    )
    ui.date_input.return_value = date(2026, 8, 30)
    monkeypatch.setattr(page, "_render_recurring", MagicMock())
    monkeypatch.setattr(
        page, "_render_forecast", MagicMock(return_value="synthetic-account")
    )
    state = page.render_forecast_page(client, session)
    assert state.account_id == "synthetic-account"
    assert state.privacy_notice_seen

    failure = ApiClientError(ApiClientErrorCode.CONNECTION_FAILED, "safe failure")
    client.current_profile.side_effect = failure
    display_error = MagicMock()
    monkeypatch.setattr(page, "render_error", display_error)
    assert page.render_forecast_page(client, session) == session
    display_error.assert_called_once_with(failure)
