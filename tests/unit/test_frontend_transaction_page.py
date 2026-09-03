"""Tests for the staged Streamlit transaction workspace."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import cashflow_ai.frontend.transaction_page as page
from cashflow_ai.frontend.client import ApiClientError, ApiClientErrorCode
from cashflow_ai.frontend.navigation import PageId
from cashflow_ai.frontend.session import FrontendSessionState
from cashflow_ai.schemas.accounts import AccountType
from cashflow_ai.schemas.analytics import AnalyticsView
from cashflow_ai.schemas.api import (
    AccountResponse,
    Page,
    TransactionResponse,
    UserProfileResponse,
)
from cashflow_ai.schemas.categories import CategorySummary
from cashflow_ai.schemas.duplicates import (
    DuplicateReason,
    DuplicateTransactionSummary,
    ProbableDuplicateReviewItem,
)
from cashflow_ai.schemas.financial_roles import (
    RoleSuggestionKind,
    RoleSuggestionReason,
    TransactionReviewAction,
)
from cashflow_ai.schemas.freshness import FinancialDataMode, FreshnessWarningCode
from cashflow_ai.schemas.transactions import Currency, Direction, FinancialRole

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _profile() -> UserProfileResponse:
    return UserProfileResponse(
        profile_id="profile-1",
        display_name="Synthetic User",
        base_currency=Currency.GBP,
        timezone="UTC",
        created_at=NOW,
        updated_at=NOW,
    )


def _account(identifier: str = "account-1") -> AccountResponse:
    return AccountResponse(
        account_id=identifier,
        user_profile_id="profile-1",
        name=f"Synthetic {identifier}",
        account_type=AccountType.CURRENT,
        currency=Currency.GBP,
        institution_label="Example Bank",
        is_active=True,
        created_at=NOW,
    )


def _category() -> CategorySummary:
    return CategorySummary(
        id="food",
        name="Food",
        parent_id=None,
        taxonomy_version="1.0",
        is_active=True,
    )


def _transaction() -> TransactionResponse:
    return TransactionResponse(
        transaction_id="transaction-1",
        account_id="account-1",
        transaction_date=date(2026, 8, 1),
        posting_date=None,
        description="Synthetic shop",
        merchant="Synthetic shop",
        amount=Decimal("-10.00"),
        balance_after=Decimal("990.00"),
        currency=Currency.GBP,
        external_id=None,
        transaction_type=None,
        direction=Direction.OUTFLOW,
        category_id="food",
        financial_role=FinancialRole.EXPENSE,
        verified_at=NOW,
    )


def _duplicate(*, ready: bool) -> ProbableDuplicateReviewItem:
    summary = DuplicateTransactionSummary(
        account_id="account-1",
        transaction_date=date(2026, 8, 1),
        description="Synthetic shop",
        amount=Decimal("-10.00"),
        currency=Currency.GBP,
    )
    return ProbableDuplicateReviewItem(
        raw_transaction_id="raw-1",
        import_batch_id="batch-1",
        account_id="account-1",
        source_row_number=None,
        original_date_text="01/08/2026",
        original_description="Synthetic shop",
        original_amount_text="-10.00",
        candidate=summary if ready else None,
        existing_transaction=summary if ready else None,
        score=0.9,
        reasons=(DuplicateReason.SAME_AMOUNT, DuplicateReason.CLOSE_DATE),
        can_keep=ready,
    )


def _ui(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    ui = MagicMock()
    monkeypatch.setattr(page, "st", ui)
    monkeypatch.setattr(page, "loading_state", lambda message: nullcontext())
    return ui


def test_transaction_table_applies_every_filter_and_handles_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    item = _transaction()
    client.search_transactions.return_value = Page[TransactionResponse](
        items=(item,), limit=100, offset=0, total=2
    )
    ui.multiselect.side_effect = [
        ["account-1"],
        ["food"],
        [FinancialRole.EXPENSE],
    ]
    ui.text_input.return_value = " synthetic "
    ui.checkbox.return_value = True
    ui.date_input.side_effect = [date(2026, 8, 1), date(2026, 8, 31)]

    result = page._render_transaction_table(
        client,
        profile_id="profile-1",
        accounts=(_account(),),
        categories=(_category(),),
    )

    assert result == (item,)
    request = client.search_transactions.call_args.args[0]
    assert request.search_text == "synthetic"
    assert request.start_date == date(2026, 8, 1)
    assert request.category_ids == ("food",)
    ui.info.assert_called_once()
    ui.dataframe.assert_called_once()

    empty = MagicMock()
    empty.multiselect.side_effect = [[], [], []]
    empty.text_input.return_value = ""
    empty.checkbox.return_value = False
    monkeypatch.setattr(page, "st", empty)
    monkeypatch.setattr(page, "render_empty_state", MagicMock())
    client.search_transactions.return_value = Page[TransactionResponse](
        items=(), limit=100, offset=0, total=0
    )
    assert (
        page._render_transaction_table(
            client,
            profile_id="profile-1",
            accounts=(_account(),),
            categories=(_category(),),
        )
        == ()
    )


def test_correction_controls_require_a_row_and_record_explicit_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    page._render_corrections(
        client, profile_id="profile-1", transactions=(), categories=(_category(),)
    )
    client.correct_category.assert_not_called()

    ui.reset_mock()
    ui.selectbox.side_effect = [
        "transaction-1",
        "food",
        TransactionReviewAction.EXPENSE,
    ]
    ui.button.side_effect = [True, True]
    page._render_corrections(
        client,
        profile_id="profile-1",
        transactions=(_transaction(),),
        categories=(_category(),),
    )
    category_request = client.correct_category.call_args.args[0]
    role_id, role_request = client.correct_financial_role.call_args.args
    assert category_request.category_id == "food"
    assert role_id == "transaction-1"
    assert role_request.action is TransactionReviewAction.EXPENSE
    assert ui.success.call_count == 2
    assert "Synthetic shop" in page._transaction_label(_transaction())

    ui.selectbox.side_effect = [
        "transaction-1",
        "food",
        TransactionReviewAction.EXPENSE,
    ]
    ui.button.side_effect = [False, False]
    page._render_corrections(
        client,
        profile_id="profile-1",
        transactions=(_transaction(),),
        categories=(_category(),),
    )


def test_role_reviews_refresh_confirm_reject_and_show_empty_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    suggestion = SimpleNamespace(
        suggestion_id="suggestion-1",
        kind=RoleSuggestionKind.REFUND,
        suggested_role=FinancialRole.REFUND,
        confidence=0.91,
        reasons=(RoleSuggestionReason.REFUND_LANGUAGE,),
    )
    review = SimpleNamespace(
        suggestion=suggestion,
        transaction_date=date(2026, 8, 1),
        description="Synthetic refund",
        amount=Decimal("10.00"),
        current_role=FinancialRole.UNKNOWN,
        statement_flags=("contains_refunds",),
        statement_note="Reference only",
    )
    client.generate_role_suggestions.return_value = SimpleNamespace(total=1)
    client.list_role_reviews.return_value = SimpleNamespace(items=(review,))
    ui.button.return_value = True
    ui.selectbox.return_value = "suggestion-1"
    confirm, reject = MagicMock(), MagicMock()
    confirm.button.return_value = True
    reject.button.return_value = True
    ui.columns.return_value = (confirm, reject)

    page._render_role_reviews(client, profile_id="profile-1")

    assert client.decide_role_suggestion.call_count == 2
    assert client.decide_role_suggestion.call_args_list[0].kwargs == {"confirm": True}
    assert client.decide_role_suggestion.call_args_list[1].kwargs == {"confirm": False}
    assert ui.success.call_count == 3

    client.list_role_reviews.return_value = SimpleNamespace(items=())
    ui.button.return_value = False
    page._render_role_reviews(client, profile_id="profile-1")
    assert ui.caption.call_args_list[-1].args == (
        "No financial-role suggestions currently need review.",
    )

    client.list_role_reviews.return_value = SimpleNamespace(items=(review,))
    ui.selectbox.return_value = "suggestion-1"
    ui.button.return_value = False
    confirm.button.return_value = False
    reject.button.return_value = False
    page._render_role_reviews(client, profile_id="profile-1")


def test_duplicate_reviews_keep_reject_and_explain_legacy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    client.list_duplicate_reviews.return_value = SimpleNamespace(
        items=(_duplicate(ready=True),)
    )
    ui.selectbox.return_value = "raw-1"
    keep, reject = MagicMock(), MagicMock()
    keep.button.return_value = True
    reject.button.return_value = True
    ui.columns.side_effect = [(MagicMock(), MagicMock()), (keep, reject)]

    page._render_duplicate_reviews(client, profile_id="profile-1")

    assert client.decide_duplicate.call_count == 2
    assert client.decide_duplicate.call_args_list[0].args[2].decision.value == "keep"
    assert client.decide_duplicate.call_args_list[1].args[2].decision.value == "reject"

    legacy_ui = _ui(monkeypatch)
    client.list_duplicate_reviews.return_value = SimpleNamespace(
        items=(_duplicate(ready=False),)
    )
    legacy_ui.selectbox.return_value = "raw-1"
    incoming, existing, keep, reject = (MagicMock() for _ in range(4))
    keep.button.return_value = False
    reject.button.return_value = False
    legacy_ui.columns.side_effect = [
        (incoming, existing),
        (keep, reject),
    ]
    page._render_duplicate_reviews(client, profile_id="profile-1")
    legacy_ui.warning.assert_called_once()

    client.list_duplicate_reviews.return_value = SimpleNamespace(items=())
    page._render_duplicate_reviews(client, profile_id="profile-1")
    assert legacy_ui.caption.call_args_list[-1].args == (
        "No probable imported duplicates currently need review.",
    )


def _analytics(*, totals: bool = True) -> MagicMock:
    result = MagicMock()
    result.currency.value = "GBP"
    result.coverage = MagicMock()
    result.coverage.fully_covered_periods = ()
    result.coverage.partially_covered_periods = ()
    result.coverage.missing_periods = ()
    result.balance_history = (MagicMock(),) if totals else ()
    result.category_spending = ()
    result.spending_cadence = None
    result.monthly_cash_flow = ()
    result.largest_transactions = (
        SimpleNamespace(
            transaction_date=date(2026, 8, 1),
            description="Synthetic rent",
            amount=Decimal("-400.00"),
            currency=Currency.GBP,
            financial_role=FinancialRole.EXPENSE,
        ),
    )
    result.totals = (
        SimpleNamespace(
            total_income=Decimal("1000.00"),
            total_expenses=Decimal("400.00"),
            net_cash_flow=Decimal("600.00"),
        )
        if totals
        else None
    )
    result.savings_rate = SimpleNamespace(
        rate_percent=Decimal("60.00") if totals else None,
        unavailable_reason=None,
    )
    return result


def test_dashboard_validates_scope_and_renders_freshness_and_charts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    monkeypatch.setattr(page, "coverage_chart", MagicMock(return_value={}))
    monkeypatch.setattr(page, "monthly_cash_flow_chart", MagicMock(return_value={}))
    monkeypatch.setattr(page, "category_chart", MagicMock(return_value={}))
    monkeypatch.setattr(page, "cadence_chart", MagicMock(return_value={}))
    monkeypatch.setattr(page, "balance_chart", MagicMock(return_value={}))
    accounts = (_account(), _account("account-2"))

    ui.multiselect.return_value = []
    ui.date_input.return_value = (date(2026, 8, 1), date(2026, 8, 31))
    page._render_dashboard(
        client,
        profile_id="profile-1",
        accounts=accounts,
        transactions=(_transaction(),),
    )
    client.cash_flow.assert_not_called()

    ui.multiselect.return_value = ["account-1"]
    ui.date_input.return_value = date(2026, 8, 1)
    page._render_dashboard(
        client,
        profile_id="profile-1",
        accounts=accounts,
        transactions=(),
    )
    ui.date_input.return_value = (date(2026, 8, 2), date(2026, 8, 1))
    page._render_dashboard(
        client,
        profile_id="profile-1",
        accounts=accounts,
        transactions=(),
    )

    ui.multiselect.return_value = ["account-1", "account-2"]
    ui.date_input.return_value = (date(2026, 8, 1), date(2026, 8, 31))
    client.cash_flow.return_value = _analytics()
    client.freshness.side_effect = [
        SimpleNamespace(mode=FinancialDataMode.ACTIVE_FORECASTING, warnings=()),
        SimpleNamespace(
            mode=FinancialDataMode.ARCHIVE,
            warnings=(FreshnessWarningCode.TRANSACTIONS_STALE,),
        ),
    ]
    columns = [MagicMock() for _ in range(10)]

    def column_groups(count: int) -> tuple[MagicMock, ...]:
        return tuple(columns[:count])

    ui.columns.side_effect = column_groups
    page._render_dashboard(
        client,
        profile_id="profile-1",
        accounts=accounts,
        transactions=(_transaction(),),
    )
    scope = client.cash_flow.call_args.args[0]
    assert scope.view is AnalyticsView.CONSOLIDATED
    assert client.freshness.call_count == 2
    assert ui.vega_lite_chart.call_count == 3
    ui.dataframe.assert_called_once()

    client.reset_mock()
    client.cash_flow.return_value = _analytics(totals=False)
    client.freshness.return_value = SimpleNamespace(
        mode=FinancialDataMode.ARCHIVE,
        warnings=(FreshnessWarningCode.NO_VERIFIED_BALANCE,),
    )
    client.freshness.side_effect = None
    ui.multiselect.return_value = ["account-1"]
    page._render_dashboard(
        client,
        profile_id="profile-1",
        accounts=accounts,
        transactions=(),
    )
    assert client.cash_flow.call_args.args[0].view is AnalyticsView.ACCOUNT

    no_optional = _analytics()
    no_optional.balance_history = ()
    no_optional.largest_transactions = ()
    client.cash_flow.return_value = no_optional
    client.freshness.return_value = SimpleNamespace(
        mode=FinancialDataMode.ACTIVE_FORECASTING,
        warnings=(),
    )
    page._render_dashboard(
        client,
        profile_id="profile-1",
        accounts=accounts,
        transactions=(),
    )


def test_dashboard_range_and_page_orchestration_are_data_minimised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert page._dashboard_range((_transaction(),)) == (
        date(2026, 8, 1),
        date(2026, 8, 1),
    )
    today_start, today_end = page._dashboard_range(())
    assert today_start == today_end == date.today()

    ui = _ui(monkeypatch)
    ui.tabs.return_value = (nullcontext(), nullcontext(), nullcontext())
    client = MagicMock()
    client.current_profile.return_value = _profile()
    client.list_accounts.return_value = Page[AccountResponse](
        items=(_account(),), limit=100, offset=0, total=1
    )
    client.list_categories.return_value = Page[CategorySummary](
        items=(_category(),), limit=100, offset=0, total=1
    )
    monkeypatch.setattr(page, "_render_transaction_table", MagicMock(return_value=()))
    monkeypatch.setattr(page, "_render_corrections", MagicMock())
    monkeypatch.setattr(page, "_render_role_reviews", MagicMock())
    monkeypatch.setattr(page, "_render_duplicate_reviews", MagicMock())
    monkeypatch.setattr(page, "_render_dashboard", MagicMock())
    session = FrontendSessionState(selected_page=PageId.TRANSACTIONS)

    updated = page.render_transaction_page(client, session)

    assert updated.user_profile_id == "profile-1"
    assert updated.account_id == "account-1"
    assert updated.model_dump().keys() == session.model_dump().keys()

    client.list_accounts.return_value = Page[AccountResponse](
        items=(), limit=100, offset=0, total=0
    )
    empty_state = MagicMock()
    monkeypatch.setattr(page, "render_empty_state", empty_state)
    assert page.render_transaction_page(client, session) == session
    empty_state.assert_called_once()

    failure = ApiClientError(
        ApiClientErrorCode.CONNECTION_FAILED,
        "the local API is unavailable; start it and try again",
    )
    client.current_profile.side_effect = failure
    display_error = MagicMock()
    monkeypatch.setattr(page, "render_error", display_error)
    assert page.render_transaction_page(client, session) == session
    display_error.assert_called_once_with(failure)
