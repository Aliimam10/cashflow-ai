"""Streamlit transaction correction, review, and analytics workflow."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from html import escape
from typing import Protocol

import streamlit as st

from cashflow_ai.frontend.client import ApiClientError
from cashflow_ai.frontend.components import (
    loading_state,
    render_empty_state,
    render_error,
    render_page_header,
)
from cashflow_ai.frontend.dashboard_workflow import (
    cash_balance_hero,
    pulse_line_html,
)
from cashflow_ai.frontend.session import FrontendSessionState
from cashflow_ai.frontend.transaction_workflow import (
    balance_chart,
    cadence_chart,
    category_chart,
    coverage_chart,
    money_text,
    monthly_cash_flow_chart,
    transaction_rows,
)
from cashflow_ai.schemas.analytics import (
    AnalyticsScope,
    AnalyticsView,
    CashFlowAnalytics,
)
from cashflow_ai.schemas.api import (
    AccountResponse,
    Page,
    TransactionResponse,
    TransactionSearchRequest,
    UserProfileResponse,
)
from cashflow_ai.schemas.api_decisions import (
    FinancialDataFreshnessRequest,
    FinancialRoleSuggestionRequest,
    RoleDecisionRequest,
    TransactionRoleReviewRequest,
)
from cashflow_ai.schemas.categories import CategorySummary
from cashflow_ai.schemas.duplicates import (
    DuplicateReviewDecision,
    DuplicateReviewRequest,
    DuplicateReviewResult,
    DuplicateTransactionSummary,
    ProbableDuplicateReviewItem,
)
from cashflow_ai.schemas.financial_roles import (
    FinancialRoleSuggestion,
    RoleDecisionResult,
    RoleReviewItem,
    TransactionReviewAction,
)
from cashflow_ai.schemas.freshness import FinancialDataFreshness, FreshnessPolicy
from cashflow_ai.schemas.hybrid_categorisation import (
    CategoryFeedback,
    CategoryFeedbackAction,
    CategoryFeedbackResult,
)
from cashflow_ai.schemas.statements import DateRange
from cashflow_ai.schemas.transactions import FinancialRole


class TransactionApi(Protocol):
    """Typed API surface used by the transaction workspace."""

    def current_profile(self) -> UserProfileResponse:
        """Return the single local profile."""
        ...

    def list_accounts(self, profile_id: str) -> Page[AccountResponse]:
        """Return the profile's local accounts."""
        ...

    def list_categories(self) -> Page[CategorySummary]:
        """Return the versioned category taxonomy."""
        ...

    def search_transactions(
        self,
        request: TransactionSearchRequest,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Page[TransactionResponse]:
        """Search verified transactions."""
        ...

    def correct_category(self, request: CategoryFeedback) -> CategoryFeedbackResult:
        """Save one explicit category correction."""
        ...

    def correct_financial_role(
        self, transaction_id: str, request: TransactionRoleReviewRequest
    ) -> RoleDecisionResult:
        """Save one explicit financial-role correction."""
        ...

    def generate_role_suggestions(
        self, request: FinancialRoleSuggestionRequest
    ) -> Page[FinancialRoleSuggestion]:
        """Generate non-mutating financial-role suggestions."""
        ...

    def list_role_reviews(self, profile_id: str) -> Page[RoleReviewItem]:
        """Return pending financial-role reviews."""
        ...

    def decide_role_suggestion(
        self,
        suggestion_id: str,
        request: RoleDecisionRequest,
        *,
        confirm: bool,
    ) -> RoleDecisionResult:
        """Confirm or reject a role suggestion."""
        ...

    def list_duplicate_reviews(
        self, profile_id: str
    ) -> Page[ProbableDuplicateReviewItem]:
        """Return unresolved probable duplicates."""
        ...

    def decide_duplicate(
        self,
        profile_id: str,
        raw_transaction_id: str,
        request: DuplicateReviewRequest,
    ) -> DuplicateReviewResult:
        """Keep or reject a probable duplicate candidate."""
        ...

    def cash_flow(self, request: AnalyticsScope) -> CashFlowAnalytics:
        """Return coverage-aware cash-flow analytics."""
        ...

    def freshness(
        self, request: FinancialDataFreshnessRequest
    ) -> FinancialDataFreshness:
        """Return freshness evidence for one account."""
        ...


def _transaction_label(item: TransactionResponse) -> str:
    return (
        f"{item.transaction_date.isoformat()} · {item.description} · "
        f"{money_text(item.amount, item.currency.value)}"
    )


def _render_transaction_table(
    client: TransactionApi,
    *,
    profile_id: str,
    accounts: tuple[AccountResponse, ...],
    categories: tuple[CategorySummary, ...],
) -> tuple[TransactionResponse, ...]:
    st.subheader("Find a transaction")
    st.caption("Search or filter the transactions you have already confirmed.")
    account_names = {item.account_id: item.name for item in accounts}
    category_names = {item.id: item.name for item in categories}

    selected_accounts = st.multiselect(
        "Accounts",
        options=tuple(account_names),
        default=tuple(account_names),
        format_func=account_names.__getitem__,
    )
    search_text = st.text_input("Search merchant or description").strip()
    selected_categories = st.multiselect(
        "Categories",
        options=tuple(category_names),
        format_func=category_names.__getitem__,
    )
    selected_roles = st.multiselect(
        "Financial roles",
        options=tuple(FinancialRole),
        format_func=lambda role: role.value.replace("_", " "),
    )
    use_dates = st.checkbox("Filter by transaction date")
    start_date = st.date_input("From", value=date.today()) if use_dates else None
    end_date = st.date_input("To", value=date.today()) if use_dates else None

    result = client.search_transactions(
        TransactionSearchRequest(
            user_profile_id=profile_id,
            account_ids=tuple(selected_accounts) or None,
            start_date=start_date,
            end_date=end_date,
            search_text=search_text or None,
            category_ids=tuple(selected_categories) or None,
            financial_roles=tuple(selected_roles) or None,
        )
    )
    if result.total > len(result.items):
        st.info(f"Showing the first {len(result.items)} of {result.total} matches.")
    if not result.items:
        render_empty_state(
            "No matching transactions",
            "Adjust the filters or import and confirm a statement first.",
        )
        return ()
    st.dataframe(
        transaction_rows(
            result.items,
            account_names=account_names,
            category_names=category_names,
        ),
        hide_index=True,
        use_container_width=True,
    )
    return result.items


def _render_corrections(
    client: TransactionApi,
    *,
    profile_id: str,
    transactions: tuple[TransactionResponse, ...],
    categories: tuple[CategorySummary, ...],
) -> None:
    st.subheader("Update transaction details")
    if not transactions:
        st.caption("A matching transaction is required before making a correction.")
        return
    by_id = {item.transaction_id: item for item in transactions}
    selected_id = st.selectbox(
        "Transaction",
        options=tuple(by_id),
        format_func=lambda item_id: _transaction_label(by_id[item_id]),
    )
    category_names = {item.id: item.name for item in categories}
    category_id = st.selectbox(
        "Correct category",
        options=tuple(category_names),
        format_func=category_names.__getitem__,
    )
    if st.button("Save category correction", disabled=not categories):
        client.correct_category(
            CategoryFeedback(
                user_profile_id=profile_id,
                transaction_id=selected_id,
                category_id=category_id,
                action=CategoryFeedbackAction.TRANSACTION_ONLY,
                corrected_at=datetime.now(UTC),
            )
        )
        st.success("Category correction saved and downstream results invalidated.")

    action = st.selectbox(
        "How should this transaction count?",
        options=tuple(TransactionReviewAction),
        format_func=lambda item: item.value.replace("_", " "),
    )
    st.caption(
        "Financial role controls whether money is treated as income, spending, "
        "a transfer, refund, reimbursement, cash withdrawal, or excluded activity."
    )
    if st.button("Save how this transaction counts"):
        client.correct_financial_role(
            selected_id,
            TransactionRoleReviewRequest(
                action=action,
                changed_at=datetime.now(UTC),
            ),
        )
        st.success("Financial-role correction saved with an audit record.")


def _render_role_reviews(client: TransactionApi, *, profile_id: str) -> None:
    st.subheader("Money movements to check")
    st.caption("Possible transfers, refunds and reimbursements need your approval.")
    if st.button("Refresh role suggestions"):
        generated = client.generate_role_suggestions(
            FinancialRoleSuggestionRequest(user_profile_id=profile_id)
        )
        st.success(f"Role suggestion scan complete: {generated.total} suggestion(s).")
    reviews = client.list_role_reviews(profile_id).items
    if not reviews:
        st.caption("No money-movement suggestions currently need review.")
        return
    by_id = {item.suggestion.suggestion_id: item for item in reviews}
    suggestion_id = st.selectbox(
        "Role suggestion",
        options=tuple(by_id),
        format_func=lambda item_id: (
            f"{by_id[item_id].suggestion.kind.value}: {by_id[item_id].description}"
        ),
    )
    selected = by_id[suggestion_id]
    st.write(
        {
            "date": selected.transaction_date.isoformat(),
            "description": selected.description,
            "amount": str(selected.amount),
            "current_role": selected.current_role.value,
            "suggested_role": selected.suggestion.suggested_role.value,
            "confidence": selected.suggestion.confidence,
            "reasons": [item.value for item in selected.suggestion.reasons],
            "statement_flags": list(selected.statement_flags),
            "statement_note_reference_only": selected.statement_note,
        }
    )
    confirm, reject = st.columns(2)
    request = RoleDecisionRequest(reviewed_at=datetime.now(UTC))
    if confirm.button("Confirm suggestion"):
        client.decide_role_suggestion(suggestion_id, request, confirm=True)
        st.success("Suggestion confirmed and recorded in the audit history.")
    if reject.button("Reject suggestion"):
        client.decide_role_suggestion(suggestion_id, request, confirm=False)
        st.success("Suggestion rejected; the current financial role was preserved.")


def _duplicate_summary(item: DuplicateTransactionSummary) -> dict[str, object]:
    return {
        "date": item.transaction_date.isoformat(),
        "description": item.description,
        "amount": str(item.amount),
        "currency": item.currency.value,
    }


def _render_duplicate_reviews(client: TransactionApi, *, profile_id: str) -> None:
    st.subheader("Possible duplicates")
    reviews = client.list_duplicate_reviews(profile_id).items
    if not reviews:
        st.caption("No probable imported duplicates currently need review.")
        return
    by_id = {item.raw_transaction_id: item for item in reviews}
    raw_id = st.selectbox(
        "Duplicate candidate",
        options=tuple(by_id),
        format_func=lambda item_id: (
            f"Row {by_id[item_id].source_row_number or 'unknown'} · "
            f"{by_id[item_id].original_description}"
        ),
    )
    selected = by_id[raw_id]
    incoming, existing = st.columns(2)
    incoming.markdown("**Incoming statement row**")
    incoming.write(
        _duplicate_summary(selected.candidate)
        if selected.candidate is not None
        else {
            "date_as_imported": selected.original_date_text,
            "description_as_imported": selected.original_description,
            "amount_as_imported": selected.original_amount_text,
        }
    )
    existing.markdown("**Existing transaction**")
    existing.write(
        _duplicate_summary(selected.existing_transaction)
        if selected.existing_transaction is not None
        else "The original comparison transaction is unavailable."
    )
    st.caption(
        f"Match confidence {selected.score:.0%}; reasons: "
        + ", ".join(item.value.replace("_", " ") for item in selected.reasons)
    )
    keep, reject = st.columns(2)
    if keep.button("Keep as a separate transaction", disabled=not selected.can_keep):
        client.decide_duplicate(
            profile_id,
            raw_id,
            DuplicateReviewRequest(
                decision=DuplicateReviewDecision.KEEP,
                decided_at=datetime.now(UTC),
            ),
        )
        st.success("Kept as a separate transaction.")
    if reject.button("Reject as duplicate"):
        client.decide_duplicate(
            profile_id,
            raw_id,
            DuplicateReviewRequest(
                decision=DuplicateReviewDecision.REJECT,
                decided_at=datetime.now(UTC),
            ),
        )
        st.success("Candidate rejected; its raw source row remains preserved.")
    if not selected.can_keep:
        st.warning(
            "This row predates retained candidate snapshots. Re-import the original "
            "statement to keep it safely; rejecting it is still available."
        )


def _dashboard_range(
    transactions: tuple[TransactionResponse, ...],
) -> tuple[date, date]:
    dates = tuple(item.transaction_date for item in transactions)
    today = date.today()
    return (min(dates), max(dates)) if dates else (today, today)


def _dashboard_boundary_transactions(
    client: TransactionApi,
    *,
    profile_id: str,
    accounts: tuple[AccountResponse, ...],
) -> tuple[TransactionResponse, ...]:
    """Load per-account newest/oldest rows for safe defaults and populated state."""
    boundaries: list[TransactionResponse] = []
    seen_ids: set[str] = set()
    for account in accounts:
        request = TransactionSearchRequest(
            user_profile_id=profile_id,
            account_ids=(account.account_id,),
        )
        newest = client.search_transactions(request, limit=1)
        account_rows = newest.items
        if newest.items and newest.total > 1:
            oldest = client.search_transactions(
                request,
                limit=1,
                offset=newest.total - 1,
            )
            account_rows = (*oldest.items, *newest.items)
        for transaction in account_rows:
            if transaction.transaction_id not in seen_ids:
                seen_ids.add(transaction.transaction_id)
                boundaries.append(transaction)
    return tuple(boundaries)


def _dashboard_period_key(
    account_ids: tuple[str, ...],
    start_date: date,
    end_date: date,
) -> str:
    """Reset automatic dates only when selected data boundaries actually change."""
    payload = "|".join((*account_ids, start_date.isoformat(), end_date.isoformat()))
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"cashflow_dashboard_period_v3_{fingerprint}"


def _render_cash_balance_hero(analytics: CashFlowAnalytics) -> None:
    """Render verified cash balance and the single requested pulse animation."""
    hero = cash_balance_hero(analytics)
    if hero.latest_observation_date is None:
        date_label = "No verified balance date"
    elif hero.earliest_latest_observation_date == hero.latest_observation_date:
        date_label = f"As of {hero.latest_observation_date.strftime('%d %b %Y')}"
    else:
        earliest = hero.earliest_latest_observation_date
        date_label = (
            "Latest account balances observed "
            f"{earliest.strftime('%d %b %Y') if earliest is not None else 'unknown'}"
            f" to {hero.latest_observation_date.strftime('%d %b %Y')}"
        )
    comparison = hero.change_label
    if hero.change_percent_label is not None:
        comparison = f"{comparison} ({hero.change_percent_label})"
    st.markdown(
        (
            '<section class="cf-balance-hero">'
            '<div class="cf-balance-copy">'
            '<span class="cf-balance-kicker">VERIFIED CASH BALANCE</span>'
            f'<div class="cf-balance-value">{hero.cash_balance_label}</div>'
            f'<div class="cf-balance-meta">{date_label}</div>'
            f'<div class="cf-balance-change is-{hero.change_tone}">'
            f"{comparison}</div></div>"
            f'<div class="cf-balance-pulse">{pulse_line_html(analytics)}</div>'
            "</section>"
        ),
        unsafe_allow_html=True,
    )


def _render_recent_transactions(
    client: TransactionApi,
    *,
    profile_id: str,
    account_ids: tuple[str, ...],
    account_names: dict[str, str],
    start_date: date,
    end_date: date,
) -> None:
    """Render a bounded newest-first activity list without raw import payloads."""
    try:
        recent = client.search_transactions(
            TransactionSearchRequest(
                user_profile_id=profile_id,
                account_ids=account_ids,
                start_date=start_date,
                end_date=end_date,
            ),
            limit=6,
        ).items
    except ApiClientError as error:
        st.caption(
            "Recent activity is temporarily unavailable"
            + (f" · {error.problem_code}" if error.problem_code else "")
        )
        return
    if not recent:
        return

    rows = []
    for transaction in recent:
        if transaction.amount > 0:
            amount_tone = "positive"
        elif transaction.amount < 0:
            amount_tone = "negative"
        else:
            amount_tone = "neutral"
        role = transaction.financial_role.value.replace("_", " ").title()
        rows.append(
            '<div class="cf-transaction-row">'
            '<div class="cf-transaction-main">'
            f"<strong>{escape(transaction.description)}</strong>"
            '<span class="cf-transaction-meta">'
            f"{transaction.transaction_date.strftime('%d %b %Y')} · "
            f"{escape(account_names.get(transaction.account_id, 'Account'))} · "
            f"{escape(role)}</span></div>"
            f'<div class="cf-transaction-amount is-{amount_tone}">'
            f"{escape(money_text(transaction.amount, transaction.currency.value))}"
            "</div></div>"
        )
    st.markdown(
        (
            '<section class="cf-activity-card">'
            '<div class="cf-section-heading"><div><span>RECENT ACTIVITY</span>'
            "<h3>Latest transactions</h3></div>"
            f'<div class="cf-count-pill">{len(recent)} shown</div></div>'
            + "".join(rows)
            + "</section>"
        ),
        unsafe_allow_html=True,
    )


def _render_dashboard(
    client: TransactionApi,
    *,
    profile_id: str,
    accounts: tuple[AccountResponse, ...],
    transactions: tuple[TransactionResponse, ...],
) -> None:
    st.subheader("Your financial pulse")
    account_names = {item.account_id: item.name for item in accounts}
    populated_ids = tuple(
        account.account_id
        for account in accounts
        if any(
            transaction.account_id == account.account_id for transaction in transactions
        )
    )
    if not populated_ids:
        render_empty_state(
            "No confirmed transaction history",
            "Add and confirm a statement to build your private dashboard.",
        )
        return
    account_by_id = {item.account_id: item for item in accounts}
    default_currency = account_by_id[populated_ids[0]].currency
    default_ids = tuple(
        account_id
        for account_id in populated_ids
        if account_by_id[account_id].currency is default_currency
    )
    selected_ids = st.multiselect(
        "Dashboard accounts",
        options=tuple(account_names),
        default=default_ids,
        format_func=account_names.__getitem__,
    )
    if not selected_ids:
        st.warning("Select at least one account for the dashboard.")
        return
    selected_accounts = tuple(account_by_id[account_id] for account_id in selected_ids)
    if len({account.currency for account in selected_accounts}) != 1:
        st.warning(
            "Choose accounts in one currency at a time. CashFlow AI does not "
            "invent exchange rates when combining balances."
        )
        return
    selected_transactions = tuple(
        transaction
        for transaction in transactions
        if transaction.account_id in selected_ids
    )
    default_start, default_end = _dashboard_range(selected_transactions)
    selected_id_tuple = tuple(selected_ids)
    requested = st.date_input(
        "Dashboard period",
        value=(default_start, default_end),
        key=_dashboard_period_key(selected_id_tuple, default_start, default_end),
    )
    if not isinstance(requested, tuple) or len(requested) != 2:
        st.warning("Choose both a start and end date.")
        return
    start_date, end_date = requested
    if end_date < start_date:
        st.warning("The dashboard end date must not precede its start date.")
        return
    scope = AnalyticsScope(
        user_profile_id=profile_id,
        account_ids=selected_id_tuple,
        period=DateRange(start_date=start_date, end_date=end_date),
        view=(
            AnalyticsView.ACCOUNT
            if len(selected_ids) == 1
            else AnalyticsView.CONSOLIDATED
        ),
    )
    try:
        with loading_state("Calculating your cash flow…"):
            analytics = client.cash_flow(scope)
    except ApiClientError as error:
        render_error(error)
        st.caption(
            "Only this dashboard section failed. Your imported source rows were "
            "not changed."
        )
        return

    _render_cash_balance_hero(analytics)
    st.caption(
        "Totals use only the dates covered by your statements. Missing dates are "
        "left out rather than counted as zero spending."
    )
    st.vega_lite_chart(coverage_chart(analytics.coverage), use_container_width=True)
    _render_recent_transactions(
        client,
        profile_id=profile_id,
        account_ids=selected_id_tuple,
        account_names=account_names,
        start_date=start_date,
        end_date=end_date,
    )
    if analytics.totals is None:
        st.warning(
            "Transactions exist for this period, but their confirmed statement "
            "coverage does not match the selected dates. Re-import a statement "
            "using the auto-detected range; no existing source row has been changed."
        )
        return
    freshness_columns = st.columns(len(selected_ids))
    for column, account_id in zip(freshness_columns, selected_ids, strict=True):
        try:
            freshness = client.freshness(
                FinancialDataFreshnessRequest(
                    account_id=account_id,
                    as_of_date=end_date,
                    policy=FreshnessPolicy(
                        max_transaction_age_days=45,
                        max_balance_age_days=45,
                        max_coverage_age_days=45,
                        minimum_contiguous_coverage_days=60,
                    ),
                )
            )
        except ApiClientError as error:
            column.metric(account_names[account_id], "Needs attention")
            column.caption(
                "Freshness check unavailable"
                + (f" · {error.problem_code}" if error.problem_code else "")
            )
            continue
        column.metric(
            account_names[account_id],
            freshness.mode.value.replace("_", " "),
            help=(
                "No freshness warnings"
                if not freshness.warnings
                else ", ".join(
                    item.value.replace("_", " ") for item in freshness.warnings
                )
            ),
        )
    totals = analytics.totals
    income, expenses, net, savings = st.columns(4)
    income.metric(
        "Observed income", money_text(totals.total_income, analytics.currency.value)
    )
    expenses.metric(
        "Observed expenses", money_text(totals.total_expenses, analytics.currency.value)
    )
    net.metric(
        "Net cash flow", money_text(totals.net_cash_flow, analytics.currency.value)
    )
    savings.metric(
        "Savings rate",
        (
            f"{analytics.savings_rate.rate_percent}%"
            if analytics.savings_rate.rate_percent is not None
            else "Unavailable"
        ),
        help=(
            None
            if analytics.savings_rate.unavailable_reason is None
            else analytics.savings_rate.unavailable_reason.value.replace("_", " ")
        ),
    )
    if totals.unknown_transaction_count:
        st.warning(
            f"{totals.unknown_transaction_count} transaction(s) still need a "
            "financial-role review before income and spending totals are complete."
        )
    st.markdown("#### Monthly cash flow")
    st.vega_lite_chart(monthly_cash_flow_chart(analytics), use_container_width=True)
    left, right = st.columns(2)
    left.markdown("#### Spending by category")
    left.vega_lite_chart(category_chart(analytics), use_container_width=True)
    right.markdown("#### Spending pattern")
    right.vega_lite_chart(cadence_chart(analytics), use_container_width=True)
    if analytics.balance_history:
        st.markdown("#### Balance history")
        st.vega_lite_chart(balance_chart(analytics), use_container_width=True)
        st.caption(
            "Balance lines stop at statement gaps; missing dates are not interpolated."
        )
    if analytics.largest_transactions:
        st.markdown("**Largest observed transactions**")
        st.dataframe(
            [
                {
                    "Date": item.transaction_date.isoformat(),
                    "Description": item.description,
                    "Amount": money_text(item.amount, item.currency.value),
                    "Role": item.financial_role.value.replace("_", " "),
                }
                for item in analytics.largest_transactions
            ],
            hide_index=True,
            use_container_width=True,
        )


def render_transaction_page(
    client: TransactionApi,
    session: FrontendSessionState,
) -> FrontendSessionState:
    """Render the complete local review workspace without caching financial rows."""
    render_page_header(
        "Transactions",
        "Understand where your money goes.",
        "Review activity, fix anything the app misunderstood, and see totals based "
        "only on the statement dates you actually provided.",
    )
    try:
        profile = client.current_profile()
        accounts = client.list_accounts(profile.profile_id).items
        if not accounts:
            render_empty_state(
                "No local accounts",
                "Create an account and import a statement under Add a statement.",
            )
            return session
        section = st.segmented_control(
            "Transaction workspace",
            options=("Dashboard", "Transactions", "Needs review"),
            default="Dashboard",
            label_visibility="collapsed",
            key="transaction_workspace_section",
        )
        if section == "Dashboard":
            with loading_state("Finding your statement dates…"):
                dashboard_transactions = _dashboard_boundary_transactions(
                    client,
                    profile_id=profile.profile_id,
                    accounts=accounts,
                )
            _render_dashboard(
                client,
                profile_id=profile.profile_id,
                accounts=accounts,
                transactions=dashboard_transactions,
            )
        elif section == "Transactions":
            categories = client.list_categories().items
            with loading_state("Loading your transactions…"):
                transactions = _render_transaction_table(
                    client,
                    profile_id=profile.profile_id,
                    accounts=accounts,
                    categories=categories,
                )
            _render_corrections(
                client,
                profile_id=profile.profile_id,
                transactions=transactions,
                categories=categories,
            )
        else:
            _render_role_reviews(client, profile_id=profile.profile_id)
            _render_duplicate_reviews(client, profile_id=profile.profile_id)
    except ApiClientError as error:
        render_error(error)
        return session
    return FrontendSessionState(
        selected_page=session.selected_page,
        user_profile_id=profile.profile_id,
        account_id=session.account_id or accounts[0].account_id,
        privacy_notice_seen=True,
    )


__all__ = ["TransactionApi", "render_transaction_page"]
