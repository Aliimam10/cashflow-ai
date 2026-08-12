"""Tests for deterministic, coverage-aware cash-flow analytics."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.analytics import (
    AnalyticsServiceError,
    AnalyticsServiceErrorCode,
    compute_cash_flow_analytics,
)
from cashflow_ai.persistence import (
    AnalyticsRepository,
    Base,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import (
    AccountRecord,
    BalanceSnapshotRecord,
    CategoryRecord,
    FinancialRoleRecord,
    FinancialRoleSuggestionRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas import (
    AnalyticsCoverageStatus,
    AnalyticsScope,
    AnalyticsValueBasis,
    AnalyticsView,
    DateRange,
    FinancialRole,
    MonthlyComparisonUnavailableReason,
    SavingsRateResult,
    SavingsRateUnavailableReason,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Engine:
    database_engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    return database_engine


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _seed_foundation(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        session.add_all(
            [
                UserProfileRecord(
                    id="profile-1",
                    display_name="Synthetic User",
                    base_currency="GBP",
                    timezone="Europe/London",
                ),
                UserProfileRecord(
                    id="profile-2",
                    display_name="Other Synthetic User",
                    base_currency="GBP",
                    timezone="Europe/London",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AccountRecord(
                    id="current-1",
                    user_profile_id="profile-1",
                    name="Synthetic Current",
                    account_type="current",
                    currency="GBP",
                ),
                AccountRecord(
                    id="savings-1",
                    user_profile_id="profile-1",
                    name="Synthetic Savings",
                    account_type="savings",
                    currency="GBP",
                ),
                AccountRecord(
                    id="other-1",
                    user_profile_id="profile-2",
                    name="Other Synthetic Current",
                    account_type="current",
                    currency="GBP",
                ),
            ]
        )
        session.add_all(
            FinancialRoleRecord(
                id=role.value,
                name=role.value.replace("_", " ").title(),
            )
            for role in FinancialRole
        )
        session.add_all(
            [
                CategoryRecord(
                    id="groceries",
                    name="Groceries",
                    parent_id=None,
                    taxonomy_version="1.0",
                    is_active=True,
                ),
                CategoryRecord(
                    id="refund",
                    name="Refund",
                    parent_id=None,
                    taxonomy_version="1.0",
                    is_active=True,
                ),
            ]
        )


def _add_batch(
    session: Session,
    batch_id: str,
    *,
    account_id: str = "current-1",
    status: str = "verified",
) -> ImportBatchRecord:
    existing = session.get(ImportBatchRecord, batch_id)
    if existing is not None:
        return existing
    record = ImportBatchRecord(
        id=batch_id,
        account_id=account_id,
        source_type="csv",
        source_filename=f"{batch_id}.csv",
        file_hash=_hash(f"file-{batch_id}"),
        mime_type="text/csv",
        byte_size=100,
        verification_status=status,
        imported_at=NOW,
    )
    session.add(record)
    session.flush()
    return record


def _add_coverage(
    session: Session,
    batch_id: str,
    *,
    start: date,
    end: date,
    account_id: str = "current-1",
    coverage_status: str = "complete",
    missing: list[dict[str, str]] | None = None,
    batch_status: str = "verified",
    note: str | None = None,
    flags: list[str] | None = None,
) -> StatementCoverageRecord:
    _add_batch(
        session,
        batch_id,
        account_id=account_id,
        status=batch_status,
    )
    context = ImportContextRecord(
        id=f"context-{batch_id}",
        import_batch_id=batch_id,
        flags_json=flags or [],
        note=note,
        created_at=NOW,
    )
    session.add(context)
    session.flush()
    coverage = StatementCoverageRecord(
        id=f"coverage-{batch_id}",
        import_context_id=context.id,
        statement_start_date=start,
        statement_end_date=end,
        coverage_status=coverage_status,
        missing_periods_json=missing or [],
    )
    session.add(coverage)
    session.flush()
    return coverage


def _add_transaction(
    session: Session,
    transaction_id: str,
    *,
    transaction_date: date,
    amount: str,
    role: FinancialRole,
    account_id: str = "current-1",
    category_id: str | None = None,
    description: str | None = None,
    batch_id: str | None = None,
) -> VerifiedTransactionRecord:
    resolved_batch_id = batch_id or f"batch-{transaction_id}"
    _add_batch(session, resolved_batch_id, account_id=account_id)
    raw = RawTransactionRecord(
        id=f"raw-{transaction_id}",
        import_batch_id=resolved_batch_id,
        source_type="csv",
        source_row_number=2,
        page_number=None,
        page_record_number=None,
        raw_payload={
            "Date": transaction_date.isoformat(),
            "Description": description or f"Synthetic {transaction_id}",
            "Amount": amount,
        },
        original_date_text=transaction_date.isoformat(),
        original_description=description or f"Synthetic {transaction_id}",
        original_amount_text=amount,
        parser_name="synthetic_parser",
        parser_version="1.0.0",
        source_fingerprint=_hash(f"source-{transaction_id}"),
        canonical_fingerprint=_hash(f"canonical-{transaction_id}"),
        issues_json=[],
        review_status="confirmed",
        created_at=NOW,
    )
    session.add(raw)
    parsed_amount = Decimal(amount)
    record = VerifiedTransactionRecord(
        id=transaction_id,
        raw_transaction_id=raw.id,
        account_id=account_id,
        transaction_date=transaction_date,
        posting_date=None,
        description=description or f"Synthetic {transaction_id}",
        merchant=None,
        amount=parsed_amount,
        balance_after=None,
        currency="GBP",
        external_id=transaction_id,
        transaction_type="synthetic",
        direction="inflow" if parsed_amount > 0 else "outflow",
        category_id=category_id,
        financial_role_id=role.value,
        verified_at=NOW,
    )
    session.add(record)
    session.flush()
    return record


def _add_raw_only(
    session: Session,
    raw_id: str,
    *,
    transaction_date: date,
) -> None:
    batch_id = f"batch-{raw_id}"
    _add_batch(session, batch_id)
    session.add(
        RawTransactionRecord(
            id=raw_id,
            import_batch_id=batch_id,
            source_type="csv",
            source_row_number=2,
            page_number=None,
            page_record_number=None,
            raw_payload={"Description": "Unapproved synthetic row"},
            original_date_text=transaction_date.isoformat(),
            original_description="Unapproved synthetic row",
            original_amount_text="-5.00",
            parser_name="synthetic_parser",
            parser_version="1.0.0",
            source_fingerprint=_hash(f"source-{raw_id}"),
            canonical_fingerprint=_hash(f"canonical-{raw_id}"),
            issues_json=[],
            review_status="needs_review",
            created_at=NOW,
        )
    )


def _add_balance(
    session: Session,
    snapshot_id: str,
    *,
    as_of_date: date,
    balance: str,
    account_id: str = "current-1",
    source: str = "manual",
    status: str = "verified",
    batch_id: str | None = None,
    recorded_at: datetime = NOW,
) -> BalanceSnapshotRecord:
    if source != "manual" and batch_id is None:
        batch_id = f"batch-balance-{snapshot_id}"
        _add_batch(session, batch_id, account_id=account_id)
    record = BalanceSnapshotRecord(
        id=snapshot_id,
        account_id=account_id,
        import_batch_id=batch_id,
        balance=Decimal(balance),
        currency="GBP",
        as_of_date=as_of_date,
        recorded_at=recorded_at,
        source=source,
        verification_status=status,
    )
    session.add(record)
    session.flush()
    return record


def _confirm_transfer_pair(
    session: Session,
    *,
    suggestion_id: str,
    outgoing: VerifiedTransactionRecord,
    incoming: VerifiedTransactionRecord,
    status: str = "confirmed",
) -> FinancialRoleSuggestionRecord:
    record = FinancialRoleSuggestionRecord(
        id=suggestion_id,
        suggestion_key=_hash(f"suggestion-{suggestion_id}"),
        verified_transaction_id=outgoing.id,
        counterpart_transaction_id=incoming.id,
        kind="transfer",
        suggested_role_id="transfer_out",
        counterpart_role_id="transfer_in",
        confidence=Decimal("1.0000"),
        reason_codes_json=["exact_opposite_amount"],
        algorithm_version="rules-v1",
        status=status,
        created_at=NOW,
        reviewed_at=NOW if status != "pending" else None,
    )
    session.add(record)
    session.flush()
    return record


def _scope(
    start: date,
    end: date,
    *,
    account_ids: tuple[str, ...] = ("current-1",),
    view: AnalyticsView = AnalyticsView.ACCOUNT,
    profile_id: str = "profile-1",
    limit: int = 10,
) -> AnalyticsScope:
    return AnalyticsScope(
        user_profile_id=profile_id,
        account_ids=account_ids,
        period=DateRange(start_date=start, end_date=end),
        view=view,
        largest_transaction_limit=limit,
    )


def _full_month_coverage(
    factory: sessionmaker[Session],
    *,
    account_id: str = "current-1",
    start: date = date(2026, 8, 1),
    end: date = date(2026, 8, 31),
    batch_id: str | None = None,
) -> None:
    with session_scope(factory) as session:
        _add_coverage(
            session,
            batch_id or f"coverage-{account_id}-{start.isoformat()}",
            account_id=account_id,
            start=start,
            end=end,
        )


def test_analytics_contracts_reject_ambiguous_shapes() -> None:
    with pytest.raises(ValidationError, match="account IDs must be unique"):
        _scope(
            date(2026, 8, 1),
            date(2026, 8, 31),
            account_ids=("current-1", "current-1"),
            view=AnalyticsView.CONSOLIDATED,
        )
    with pytest.raises(ValidationError, match="exactly one account"):
        _scope(
            date(2026, 8, 1),
            date(2026, 8, 31),
            account_ids=("current-1", "savings-1"),
        )
    with pytest.raises(ValidationError, match="either a value or"):
        SavingsRateResult()
    with pytest.raises(ValidationError, match="either a value or"):
        SavingsRateResult(
            rate_percent=Decimal("20.00"),
            unavailable_reason=SavingsRateUnavailableReason.NO_INCOME,
        )


def test_role_aware_totals_categories_cadence_and_largest_transactions(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    _full_month_coverage(factory)
    with session_scope(factory) as session:
        rows = (
            ("salary", "2000.00", FinancialRole.INCOME, None),
            ("groceries", "-500.00", FinancialRole.EXPENSE, "groceries"),
            ("uncategorised", "-100.00", FinancialRole.EXPENSE, None),
            ("refund", "50.00", FinancialRole.REFUND, "refund"),
            ("reimbursement", "20.00", FinancialRole.REIMBURSEMENT, None),
            ("cash", "-30.00", FinancialRole.CASH_WITHDRAWAL, None),
            ("unknown-in", "10.00", FinancialRole.UNKNOWN, None),
            ("unknown-out", "-11.00", FinancialRole.UNKNOWN, None),
            ("excluded-in", "12.00", FinancialRole.EXCLUDED, None),
            ("excluded-out", "-99.00", FinancialRole.EXCLUDED, None),
            ("transfer", "-200.00", FinancialRole.TRANSFER_OUT, None),
        )
        for index, (identifier, amount, role, category_id) in enumerate(rows, start=1):
            _add_transaction(
                session,
                identifier,
                transaction_date=date(2026, 8, index),
                amount=amount,
                role=role,
                category_id=category_id,
            )

    result = compute_cash_flow_analytics(
        factory,
        _scope(date(2026, 8, 1), date(2026, 8, 31), limit=4),
    )

    totals = cast(Any, result.totals)
    assert totals.basis is AnalyticsValueBasis.COMPLETE_PERIOD
    assert totals.total_income == Decimal("2000.00")
    assert totals.total_expenses == Decimal("600.00")
    assert totals.total_refunds == Decimal("50.00")
    assert totals.total_reimbursements == Decimal("20.00")
    assert totals.total_cash_withdrawals == Decimal("30.00")
    assert totals.net_cash_flow == Decimal("1440.00")
    assert totals.transfer_outflow == Decimal("200.00")
    assert totals.net_transfer_movement == Decimal("-200.00")
    assert totals.unknown_inflow == Decimal("10.00")
    assert totals.unknown_outflow == Decimal("11.00")
    assert totals.excluded_inflow == Decimal("12.00")
    assert totals.excluded_outflow == Decimal("99.00")
    assert totals.unknown_transaction_count == 2
    assert totals.excluded_transaction_count == 2
    assert result.savings_rate.unavailable_reason is (
        SavingsRateUnavailableReason.UNRESOLVED_FINANCIAL_ROLES
    )
    categories = cast(Any, result.category_spending)
    assert [(item.category_id, item.amount) for item in categories] == [
        ("groceries", Decimal("500.00")),
        (None, Decimal("100.00")),
    ]
    cadence = cast(Any, result.spending_cadence)
    assert cadence.recurring == Decimal("0.00")
    assert cadence.discretionary == Decimal("0.00")
    assert cadence.unclassified == Decimal("600.00")
    assert cadence.unclassified_count == 2
    assert [item.transaction_id for item in result.largest_transactions] == [
        "salary",
        "groceries",
        "transfer",
        "uncategorised",
    ]
    assert "excluded-out" not in {
        item.transaction_id for item in result.largest_transactions
    }


def test_savings_rate_uses_external_net_cash_flow_and_serialises_decimal(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    _full_month_coverage(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "income",
            transaction_date=date(2026, 8, 1),
            amount="100.00",
            role=FinancialRole.INCOME,
        )
        _add_transaction(
            session,
            "expense",
            transaction_date=date(2026, 8, 2),
            amount="-25.00",
            role=FinancialRole.EXPENSE,
        )
        _add_transaction(
            session,
            "refund",
            transaction_date=date(2026, 8, 3),
            amount="10.00",
            role=FinancialRole.REFUND,
        )
        _add_transaction(
            session,
            "ignored",
            transaction_date=date(2026, 8, 4),
            amount="-80.00",
            role=FinancialRole.EXCLUDED,
        )

    result = compute_cash_flow_analytics(
        factory,
        _scope(date(2026, 8, 1), date(2026, 8, 31)),
    )

    assert result.savings_rate.rate_percent == Decimal("85.00")
    assert result.savings_rate.unavailable_reason is None
    payload = result.model_dump_json()
    assert '"rate_percent":"85.00"' in payload
    assert '"net_cash_flow":"85.00"' in payload


def test_account_and_consolidated_transfer_views_use_confirmed_current_pair(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    for account_id in ("current-1", "savings-1"):
        _full_month_coverage(
            factory,
            account_id=account_id,
            start=date(2026, 1, 1),
            end=date(2026, 2, 28),
        )
    with session_scope(factory) as session:
        outgoing = _add_transaction(
            session,
            "transfer-out",
            account_id="current-1",
            transaction_date=date(2026, 1, 31),
            amount="-500.00",
            role=FinancialRole.TRANSFER_OUT,
        )
        incoming = _add_transaction(
            session,
            "transfer-in",
            account_id="savings-1",
            transaction_date=date(2026, 2, 1),
            amount="500.00",
            role=FinancialRole.TRANSFER_IN,
        )
        _confirm_transfer_pair(
            session,
            suggestion_id="pair-1",
            outgoing=outgoing,
            incoming=incoming,
        )

    account = compute_cash_flow_analytics(
        factory,
        _scope(date(2026, 1, 1), date(2026, 1, 31)),
    )
    consolidated_january = compute_cash_flow_analytics(
        factory,
        _scope(
            date(2026, 1, 1),
            date(2026, 1, 31),
            account_ids=("current-1", "savings-1"),
            view=AnalyticsView.CONSOLIDATED,
        ),
    )
    boundary = compute_cash_flow_analytics(
        factory,
        _scope(
            date(2026, 1, 1),
            date(2026, 1, 31),
            account_ids=("current-1",),
            view=AnalyticsView.CONSOLIDATED,
        ),
    )
    consolidated_both_months = compute_cash_flow_analytics(
        factory,
        _scope(
            date(2026, 1, 1),
            date(2026, 2, 28),
            account_ids=("current-1", "savings-1"),
            view=AnalyticsView.CONSOLIDATED,
        ),
    )

    assert cast(Any, account.totals).transfer_outflow == Decimal("500.00")
    assert cast(Any, consolidated_january.totals).transfer_outflow == Decimal("0.00")
    assert cast(Any, consolidated_january.totals).matched_internal_transfer_count == 1
    assert cast(Any, boundary.totals).transfer_outflow == Decimal("500.00")
    assert cast(Any, consolidated_both_months.totals).transfer_inflow == Decimal("0.00")
    assert cast(Any, consolidated_both_months.totals).transfer_outflow == Decimal(
        "0.00"
    )
    assert (
        cast(Any, consolidated_both_months.totals).matched_internal_transfer_count == 2
    )
    assert cast(Any, consolidated_both_months.totals).net_cash_flow == Decimal("0.00")


def test_pending_and_stale_transfer_suggestions_do_not_create_internal_links(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    for account_id in ("current-1", "savings-1"):
        _full_month_coverage(factory, account_id=account_id)
    with session_scope(factory) as session:
        pending_out = _add_transaction(
            session,
            "pending-out",
            account_id="current-1",
            transaction_date=date(2026, 8, 2),
            amount="-40.00",
            role=FinancialRole.TRANSFER_OUT,
        )
        pending_in = _add_transaction(
            session,
            "pending-in",
            account_id="savings-1",
            transaction_date=date(2026, 8, 2),
            amount="40.00",
            role=FinancialRole.TRANSFER_IN,
        )
        _confirm_transfer_pair(
            session,
            suggestion_id="pending-pair",
            outgoing=pending_out,
            incoming=pending_in,
            status="pending",
        )
        stale_out = _add_transaction(
            session,
            "stale-out",
            account_id="current-1",
            transaction_date=date(2026, 8, 3),
            amount="-50.00",
            role=FinancialRole.TRANSFER_OUT,
        )
        stale_in = _add_transaction(
            session,
            "stale-in",
            account_id="savings-1",
            transaction_date=date(2026, 8, 3),
            amount="50.00",
            role=FinancialRole.TRANSFER_IN,
        )
        _confirm_transfer_pair(
            session,
            suggestion_id="stale-pair",
            outgoing=stale_out,
            incoming=stale_in,
        )
        stale_in.financial_role_id = FinancialRole.INCOME.value

    result = compute_cash_flow_analytics(
        factory,
        _scope(
            date(2026, 8, 1),
            date(2026, 8, 31),
            account_ids=("current-1", "savings-1"),
            view=AnalyticsView.CONSOLIDATED,
        ),
    )

    totals = cast(Any, result.totals)
    assert totals.transfer_outflow == Decimal("90.00")
    assert totals.transfer_inflow == Decimal("40.00")
    assert totals.total_income == Decimal("50.00")
    assert totals.matched_internal_transfer_count == 0


def test_corrupt_confirmed_transfer_link_is_ignored_without_changing_current_role(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_foundation(factory)
    _full_month_coverage(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "visible-transfer",
            transaction_date=date(2026, 8, 2),
            amount="-25.00",
            role=FinancialRole.TRANSFER_OUT,
        )

    monkeypatch.setattr(
        AnalyticsRepository,
        "list_confirmed_transfer_pairs",
        lambda *args, **kwargs: (
            (
                SimpleNamespace(
                    suggested_role_id="transfer_out",
                    counterpart_role_id="transfer_in",
                ),
                SimpleNamespace(
                    id="visible-transfer",
                    account_id="current-1",
                    currency="GBP",
                    amount=Decimal("-25.00"),
                    financial_role_id="invented",
                ),
                SimpleNamespace(
                    id="counterpart",
                    account_id="savings-1",
                    currency="GBP",
                    amount=Decimal("25.00"),
                    financial_role_id="transfer_in",
                ),
            ),
        ),
    )

    result = compute_cash_flow_analytics(
        factory,
        _scope(date(2026, 8, 1), date(2026, 8, 31)),
    )

    assert cast(Any, result.totals).transfer_outflow == Decimal("25.00")
    assert cast(Any, result.totals).matched_internal_transfer_count == 0


def test_disconnected_coverage_marks_unknown_months_unavailable_not_zero(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "january",
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
        )
        _add_coverage(
            session,
            "august",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
        )

    result = compute_cash_flow_analytics(
        factory,
        _scope(date(2026, 1, 1), date(2026, 8, 31)),
    )

    assert result.coverage.status is AnalyticsCoverageStatus.PARTIAL
    assert result.coverage.fully_covered_periods == (
        DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)),
        DateRange(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31)),
    )
    assert result.coverage.missing_periods == (
        DateRange(start_date=date(2026, 2, 1), end_date=date(2026, 7, 31)),
    )
    assert result.monthly_cash_flow[0].totals is not None
    assert cast(Any, result.monthly_cash_flow[0].totals).total_income == Decimal("0.00")
    assert all(month.totals is None for month in result.monthly_cash_flow[1:7])
    assert result.monthly_cash_flow[7].totals is not None
    assert result.monthly_comparisons[0].unavailable_reason is (
        MonthlyComparisonUnavailableReason.INCOMPLETE_COVERAGE
    )


def test_consolidated_coverage_uses_intersection_union_and_account_details(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "current-january",
            account_id="current-1",
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
        )
        _add_coverage(
            session,
            "savings-january",
            account_id="savings-1",
            start=date(2026, 1, 15),
            end=date(2026, 1, 31),
        )
        _add_balance(
            session,
            "current-only-balance",
            as_of_date=date(2026, 1, 20),
            balance="100.00",
        )

    result = compute_cash_flow_analytics(
        factory,
        _scope(
            date(2026, 1, 1),
            date(2026, 1, 31),
            account_ids=("current-1", "savings-1"),
            view=AnalyticsView.CONSOLIDATED,
        ),
    )

    assert result.coverage.status is AnalyticsCoverageStatus.PARTIAL
    assert result.coverage.fully_covered_periods == (
        DateRange(start_date=date(2026, 1, 15), end_date=date(2026, 1, 31)),
    )
    assert result.coverage.partially_covered_periods == (
        DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 14)),
    )
    assert result.coverage.missing_periods == ()
    assert result.coverage.fully_covered_days == 17
    assert result.coverage.partially_covered_days == 14
    assert result.coverage.accounts[0].status is AnalyticsCoverageStatus.COMPLETE
    assert result.coverage.accounts[1].status is AnalyticsCoverageStatus.PARTIAL
    assert cast(Any, result.totals).basis is AnalyticsValueBasis.OBSERVED_ONLY


def test_disjoint_account_coverage_keeps_partial_union_without_false_intersection(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "current-early",
            account_id="current-1",
            start=date(2026, 1, 1),
            end=date(2026, 1, 3),
        )
        _add_coverage(
            session,
            "current-late",
            account_id="current-1",
            start=date(2026, 1, 7),
            end=date(2026, 1, 10),
        )
        _add_coverage(
            session,
            "savings-late",
            account_id="savings-1",
            start=date(2026, 1, 7),
            end=date(2026, 1, 10),
        )

    result = compute_cash_flow_analytics(
        factory,
        _scope(
            date(2026, 1, 1),
            date(2026, 1, 10),
            account_ids=("current-1", "savings-1"),
            view=AnalyticsView.CONSOLIDATED,
        ),
    )

    assert result.coverage.fully_covered_periods == (
        DateRange(start_date=date(2026, 1, 7), end_date=date(2026, 1, 10)),
    )
    assert result.coverage.partially_covered_periods == (
        DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 3)),
    )
    assert result.coverage.missing_periods == (
        DateRange(start_date=date(2026, 1, 4), end_date=date(2026, 1, 6)),
    )


def test_gapped_overlapping_partial_unknown_and_unverified_coverage(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "gapped",
            start=date(2026, 1, 1),
            end=date(2026, 1, 10),
            coverage_status="gapped",
            missing=[{"start_date": "2026-01-04", "end_date": "2026-01-06"}],
        )
        _add_coverage(
            session,
            "overlap",
            start=date(2026, 1, 9),
            end=date(2026, 1, 12),
            coverage_status="overlapping",
        )
        _add_coverage(
            session,
            "partial",
            start=date(2026, 1, 13),
            end=date(2026, 1, 14),
            coverage_status="partial",
        )
        _add_coverage(
            session,
            "unknown",
            start=date(2026, 1, 15),
            end=date(2026, 1, 16),
            coverage_status="unknown",
        )
        _add_coverage(
            session,
            "unverified",
            start=date(2026, 1, 17),
            end=date(2026, 1, 20),
            batch_status="needs_review",
        )

    result = compute_cash_flow_analytics(
        factory,
        _scope(date(2026, 1, 1), date(2026, 1, 20)),
    )

    assert result.coverage.fully_covered_periods == (
        DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 3)),
        DateRange(start_date=date(2026, 1, 7), end_date=date(2026, 1, 12)),
    )
    assert result.coverage.missing_periods == (
        DateRange(start_date=date(2026, 1, 4), end_date=date(2026, 1, 6)),
        DateRange(start_date=date(2026, 1, 13), end_date=date(2026, 1, 20)),
    )


def test_balance_history_uses_verified_priority_and_breaks_at_gaps(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "balance-coverage",
            start=date(2026, 1, 1),
            end=date(2026, 1, 10),
            coverage_status="gapped",
            missing=[{"start_date": "2026-01-04", "end_date": "2026-01-06"}],
        )
        for snapshot_id, source, value in (
            ("same-opening", "statement_opening", "900.00"),
            ("same-running", "running_balance", "910.00"),
            ("same-closing", "statement_closing", "920.00"),
            ("same-manual", "manual", "930.00"),
        ):
            _add_balance(
                session,
                snapshot_id,
                as_of_date=date(2026, 1, 2),
                balance=value,
                source=source,
            )
        _add_balance(
            session,
            "covered-first",
            as_of_date=date(2026, 1, 3),
            balance="940.00",
        )
        _add_balance(
            session,
            "standalone-gap",
            as_of_date=date(2026, 1, 5),
            balance="935.00",
        )
        _add_balance(
            session,
            "covered-second",
            as_of_date=date(2026, 1, 8),
            balance="950.00",
        )
        _add_balance(
            session,
            "unverified",
            as_of_date=date(2026, 1, 9),
            balance="999.00",
            status="needs_review",
        )

    result = compute_cash_flow_analytics(
        factory,
        _scope(date(2026, 1, 1), date(2026, 1, 10)),
    )

    segments = result.balance_history[0].segments
    assert len(segments) == 3
    assert [point.snapshot_id for point in segments[0].points] == [
        "same-manual",
        "covered-first",
    ]
    assert segments[0].coverage_period == DateRange(
        start_date=date(2026, 1, 1), end_date=date(2026, 1, 3)
    )
    assert segments[1].coverage_period is None
    assert segments[1].points[0].snapshot_id == "standalone-gap"
    assert [point.snapshot_id for point in segments[2].points] == ["covered-second"]
    assert segments[2].coverage_period == DateRange(
        start_date=date(2026, 1, 7), end_date=date(2026, 1, 10)
    )


def test_monthly_comparison_requires_full_covered_resolved_calendar_months(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "two-months",
            start=date(2026, 1, 1),
            end=date(2026, 2, 28),
        )
        _add_transaction(
            session,
            "jan-income",
            transaction_date=date(2026, 1, 10),
            amount="100.00",
            role=FinancialRole.INCOME,
        )
        _add_transaction(
            session,
            "jan-expense",
            transaction_date=date(2026, 1, 11),
            amount="-40.00",
            role=FinancialRole.EXPENSE,
        )
        _add_transaction(
            session,
            "feb-income",
            transaction_date=date(2026, 2, 10),
            amount="150.00",
            role=FinancialRole.INCOME,
        )
        _add_transaction(
            session,
            "feb-expense",
            transaction_date=date(2026, 2, 11),
            amount="-50.00",
            role=FinancialRole.EXPENSE,
        )

    result = compute_cash_flow_analytics(
        factory,
        _scope(date(2026, 1, 1), date(2026, 2, 28)),
    )

    comparison = result.monthly_comparisons[0]
    assert comparison.comparable
    assert comparison.unavailable_reason is None
    assert comparison.income_change == Decimal("50.00")
    assert comparison.expense_change == Decimal("10.00")
    assert comparison.net_cash_flow_change == Decimal("40.00")


@pytest.mark.parametrize(
    ("scope_start", "unknown_role", "expected_reason"),
    [
        (
            date(2026, 1, 15),
            False,
            MonthlyComparisonUnavailableReason.PARTIAL_CALENDAR_MONTH,
        ),
        (
            date(2026, 1, 1),
            True,
            MonthlyComparisonUnavailableReason.UNRESOLVED_FINANCIAL_ROLES,
        ),
    ],
)
def test_monthly_comparison_withholds_misleading_changes(
    factory: sessionmaker[Session],
    scope_start: date,
    unknown_role: bool,
    expected_reason: MonthlyComparisonUnavailableReason,
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "months",
            start=date(2026, 1, 1),
            end=date(2026, 2, 28),
        )
        _add_transaction(
            session,
            "january",
            transaction_date=date(2026, 1, 20),
            amount="100.00",
            role=FinancialRole.INCOME,
        )
        _add_transaction(
            session,
            "february",
            transaction_date=date(2026, 2, 20),
            amount="10.00",
            role=FinancialRole.UNKNOWN if unknown_role else FinancialRole.INCOME,
        )

    result = compute_cash_flow_analytics(
        factory,
        _scope(scope_start, date(2026, 2, 28)),
    )

    comparison = result.monthly_comparisons[0]
    assert not comparison.comparable
    assert comparison.unavailable_reason is expected_reason
    assert comparison.income_change is None


def test_inclusive_leap_day_and_deterministic_largest_tie_breaking(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "leap-day",
            start=date(2024, 2, 29),
            end=date(2024, 2, 29),
        )
        _add_transaction(
            session,
            "b-transaction",
            transaction_date=date(2024, 2, 29),
            amount="-10.00",
            role=FinancialRole.EXPENSE,
        )
        _add_transaction(
            session,
            "a-transaction",
            transaction_date=date(2024, 2, 29),
            amount="10.00",
            role=FinancialRole.INCOME,
        )

    result = compute_cash_flow_analytics(
        factory,
        _scope(date(2024, 2, 29), date(2024, 2, 29)),
    )

    assert result.coverage.requested_days == 1
    assert result.coverage.status is AnalyticsCoverageStatus.COMPLETE
    assert result.monthly_cash_flow[0].month == date(2024, 2, 1)
    assert not result.monthly_cash_flow[0].full_calendar_month
    assert [item.transaction_id for item in result.largest_transactions] == [
        "a-transaction",
        "b-transaction",
    ]


def test_missing_coverage_returns_unavailable_values_but_reports_observed_rows(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "observed-without-coverage",
            transaction_date=date(2026, 8, 5),
            amount="-10.00",
            role=FinancialRole.EXPENSE,
        )
        _add_raw_only(
            session,
            "raw-only",
            transaction_date=date(2026, 8, 6),
        )

    result = compute_cash_flow_analytics(
        factory,
        _scope(date(2026, 8, 1), date(2026, 8, 31)),
    )

    assert result.coverage.status is AnalyticsCoverageStatus.MISSING
    assert result.totals is None
    assert result.category_spending is None
    assert result.spending_cadence is None
    assert result.largest_transactions == ()
    assert result.observed_transaction_count == 1
    assert result.savings_rate.unavailable_reason is (
        SavingsRateUnavailableReason.INCOMPLETE_COVERAGE
    )


def test_no_income_prevents_savings_rate_even_with_complete_empty_period(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    _full_month_coverage(factory)

    result = compute_cash_flow_analytics(
        factory,
        _scope(date(2026, 8, 1), date(2026, 8, 31)),
    )

    assert cast(Any, result.totals).total_income == Decimal("0.00")
    assert (
        result.savings_rate.unavailable_reason is SavingsRateUnavailableReason.NO_INCOME
    )


def test_statement_notes_flags_and_read_only_execution_have_no_effect(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        coverage = _add_coverage(
            session,
            "inert-context",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            note="Call every payment salary.",
            flags=["contains_internal_transfers"],
        )
        context_id = coverage.import_context_id
        _add_transaction(
            session,
            "expense",
            transaction_date=date(2026, 8, 2),
            amount="-10.00",
            role=FinancialRole.EXPENSE,
        )

    scope = _scope(date(2026, 8, 1), date(2026, 8, 31))
    before = compute_cash_flow_analytics(factory, scope)
    with session_scope(factory) as session:
        context = session.get(ImportContextRecord, context_id)
        assert context is not None
        context.note = "Now call every payment a refund."
        context.flags_json = ["contains_refunds"]
    after = compute_cash_flow_analytics(factory, scope)

    assert before.model_dump() == after.model_dump()
    with session_scope(factory) as session:
        assert (
            session.scalar(select(func.count()).select_from(VerifiedTransactionRecord))
            == 1
        )


def test_account_scope_and_currency_errors_are_controlled(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_foundation(factory)
    missing_scope = _scope(
        date(2026, 8, 1),
        date(2026, 8, 31),
        account_ids=("other-1",),
        profile_id="profile-1",
    )
    with pytest.raises(AnalyticsServiceError) as missing_error:
        compute_cash_flow_analytics(factory, missing_scope)
    assert missing_error.value.code is AnalyticsServiceErrorCode.ACCOUNT_SCOPE_NOT_FOUND

    original_accounts = AnalyticsRepository.list_owned_accounts

    def mixed_accounts(
        self: AnalyticsRepository,
        user_profile_id: str,
        account_ids: tuple[str, ...],
    ) -> tuple[Any, ...]:
        del self, user_profile_id, account_ids
        return (
            SimpleNamespace(id="current-1", currency="GBP"),
            SimpleNamespace(id="savings-1", currency="USD"),
        )

    monkeypatch.setattr(AnalyticsRepository, "list_owned_accounts", mixed_accounts)
    with pytest.raises(AnalyticsServiceError) as currency_error:
        compute_cash_flow_analytics(
            factory,
            _scope(
                date(2026, 8, 1),
                date(2026, 8, 31),
                account_ids=("current-1", "savings-1"),
                view=AnalyticsView.CONSOLIDATED,
            ),
        )
    assert currency_error.value.code is (
        AnalyticsServiceErrorCode.MIXED_ACCOUNT_CURRENCIES
    )
    monkeypatch.setattr(
        AnalyticsRepository,
        "list_owned_accounts",
        original_accounts,
    )


def test_transaction_currency_invalid_role_and_sign_errors_are_controlled(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_foundation(factory)
    _full_month_coverage(factory)
    scope = _scope(date(2026, 8, 1), date(2026, 8, 31))
    original_transactions = AnalyticsRepository.list_transactions

    def transaction_rows(
        currency: str,
        role: str,
        amount: Decimal,
    ) -> tuple[Any, ...]:
        return (
            (
                SimpleNamespace(
                    id="synthetic-corrupt",
                    account_id="current-1",
                    transaction_date=date(2026, 8, 2),
                    description="Synthetic corrupt transaction",
                    amount=amount,
                    currency=currency,
                    financial_role_id=role,
                ),
                None,
            ),
        )

    monkeypatch.setattr(
        AnalyticsRepository,
        "list_transactions",
        lambda *args, **kwargs: transaction_rows(
            "USD", FinancialRole.EXPENSE.value, Decimal("-1.00")
        ),
    )
    with pytest.raises(AnalyticsServiceError) as currency_error:
        compute_cash_flow_analytics(factory, scope)
    assert currency_error.value.code is AnalyticsServiceErrorCode.DATA_CURRENCY_MISMATCH

    monkeypatch.setattr(
        AnalyticsRepository,
        "list_transactions",
        lambda *args, **kwargs: transaction_rows("GBP", "invented", Decimal("-1.00")),
    )
    with pytest.raises(AnalyticsServiceError) as role_error:
        compute_cash_flow_analytics(factory, scope)
    assert role_error.value.code is AnalyticsServiceErrorCode.INVALID_FINANCIAL_ROLE

    monkeypatch.setattr(
        AnalyticsRepository,
        "list_transactions",
        lambda *args, **kwargs: transaction_rows(
            "GBP", FinancialRole.EXPENSE.value, Decimal("1.00")
        ),
    )
    with pytest.raises(AnalyticsServiceError) as sign_error:
        compute_cash_flow_analytics(factory, scope)
    assert (
        sign_error.value.code is AnalyticsServiceErrorCode.INVALID_FINANCIAL_ROLE_SIGN
    )
    monkeypatch.setattr(
        AnalyticsRepository,
        "list_transactions",
        original_transactions,
    )


def test_balance_currency_error_and_empty_transfer_lookup(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_foundation(factory)
    _full_month_coverage(factory)
    scope = _scope(date(2026, 8, 1), date(2026, 8, 31))

    with session_scope(factory) as session:
        assert AnalyticsRepository(session).list_confirmed_transfer_pairs(()) == ()

    monkeypatch.setattr(
        AnalyticsRepository,
        "list_verified_balances",
        lambda *args, **kwargs: (
            SimpleNamespace(
                id="foreign-balance",
                account_id="current-1",
                as_of_date=date(2026, 8, 2),
                balance=Decimal("100.00"),
                currency="USD",
                source="manual",
            ),
        ),
    )
    with pytest.raises(AnalyticsServiceError) as error:
        compute_cash_flow_analytics(factory, scope)
    assert error.value.code is AnalyticsServiceErrorCode.DATA_CURRENCY_MISMATCH
