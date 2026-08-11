"""Tests for balance evidence, source priority, and freshness assessment."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.balances import (
    BalanceServiceError,
    BalanceServiceErrorCode,
    ManualBalanceEntry,
    assess_financial_data_freshness,
    record_manual_balance,
)
from cashflow_ai.persistence import (
    AccountRepository,
    BalanceSnapshotRepository,
    Base,
    StatementRepository,
    TransactionRepository,
    UserProfileRepository,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import (
    AccountRecord,
    BalanceSnapshotRecord,
    FinancialRoleRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas import (
    BalanceSnapshotSource,
    Currency,
    DateRange,
    FinancialDataFreshness,
    FinancialDataMode,
    FreshnessPolicy,
    FreshnessWarningCode,
    VerifiedBalanceEvidence,
)

RECORDED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
DEFAULT_POLICY = FreshnessPolicy(
    max_transaction_age_days=1,
    max_balance_age_days=5,
    max_coverage_age_days=1,
    minimum_contiguous_coverage_days=30,
)


@pytest.fixture
def engine() -> Engine:
    database_engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    return database_engine


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _seed_account(
    factory: sessionmaker[Session],
    *,
    active: bool = True,
) -> None:
    with session_scope(factory) as session:
        UserProfileRepository(session).add(
            UserProfileRecord(
                id="profile-1",
                display_name="Synthetic User",
                base_currency="GBP",
                timezone="Europe/London",
            )
        )
        AccountRepository(session).add(
            AccountRecord(
                id="account-1",
                user_profile_id="profile-1",
                name="Synthetic Current Account",
                account_type="current",
                currency="GBP",
                is_active=active,
            )
        )
        session.add(FinancialRoleRecord(id="unknown", name="Unknown"))


def _add_batch(
    session: Session,
    batch_id: str,
    *,
    verification_status: str = "verified",
) -> ImportBatchRecord:
    batch = ImportBatchRecord(
        id=batch_id,
        account_id="account-1",
        source_type="csv",
        source_filename=f"{batch_id}.csv",
        file_hash=_digest(batch_id),
        mime_type="text/csv",
        byte_size=100,
        verification_status=verification_status,
        imported_at=RECORDED_AT,
    )
    session.add(batch)
    session.flush()
    return batch


def _add_coverage(
    session: Session,
    batch_id: str,
    *,
    start: date,
    end: date,
    status: str = "complete",
    missing_periods: list[dict[str, str]] | None = None,
    verification_status: str = "verified",
) -> StatementCoverageRecord:
    _add_batch(session, batch_id, verification_status=verification_status)
    context = ImportContextRecord(
        id=f"context-{batch_id}",
        import_batch_id=batch_id,
        flags_json=[],
        note=None,
        created_at=RECORDED_AT,
    )
    session.add(context)
    session.flush()
    record = StatementCoverageRecord(
        id=f"coverage-{batch_id}",
        import_context_id=context.id,
        statement_start_date=start,
        statement_end_date=end,
        coverage_status=status,
        missing_periods_json=missing_periods or [],
    )
    session.add(record)
    session.flush()
    return record


def _add_transaction(
    session: Session,
    batch_id: str,
    *,
    transaction_id: str,
    transaction_date: date,
) -> VerifiedTransactionRecord:
    raw = RawTransactionRecord(
        id=f"raw-{transaction_id}",
        import_batch_id=batch_id,
        source_type="csv",
        source_row_number=2,
        page_number=None,
        page_record_number=None,
        raw_payload={
            "Date": transaction_date.isoformat(),
            "Description": "Synthetic transaction",
            "Amount": "-10.00",
        },
        original_date_text=transaction_date.isoformat(),
        original_description="Synthetic transaction",
        original_amount_text="-10.00",
        parser_name="synthetic_parser",
        parser_version="1.0.0",
        source_fingerprint=_digest(f"source-{transaction_id}"),
        canonical_fingerprint=_digest(f"canonical-{transaction_id}"),
        issues_json=[],
        review_status="confirmed",
        created_at=RECORDED_AT,
    )
    session.add(raw)
    session.flush()
    transaction = VerifiedTransactionRecord(
        id=transaction_id,
        raw_transaction_id=raw.id,
        account_id="account-1",
        transaction_date=transaction_date,
        posting_date=None,
        description="Synthetic transaction",
        merchant="Synthetic merchant",
        amount=Decimal("-10.00"),
        balance_after=None,
        currency="GBP",
        external_id=transaction_id,
        transaction_type="card",
        direction="outflow",
        category_id=None,
        financial_role_id="unknown",
        verified_at=RECORDED_AT,
    )
    session.add(transaction)
    session.flush()
    return transaction


def _add_balance(
    session: Session,
    *,
    snapshot_id: str,
    as_of_date: date,
    source: str = "manual",
    status: str = "verified",
    balance: Decimal = Decimal("940.00"),
    recorded_at: datetime = RECORDED_AT,
    batch_id: str | None = None,
) -> BalanceSnapshotRecord:
    record = BalanceSnapshotRecord(
        id=snapshot_id,
        account_id="account-1",
        import_batch_id=batch_id,
        balance=balance,
        currency="GBP",
        as_of_date=as_of_date,
        recorded_at=recorded_at,
        source=source,
        verification_status=status,
    )
    session.add(record)
    session.flush()
    return record


def _seed_complete_evidence(
    factory: sessionmaker[Session],
    *,
    account_active: bool = True,
    transaction_date: date = date(2026, 8, 9),
    balance_date: date = date(2026, 8, 5),
    coverage_start: date = date(2026, 7, 10),
    coverage_end: date = date(2026, 8, 9),
) -> None:
    _seed_account(factory, active=account_active)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "batch-1",
            start=coverage_start,
            end=coverage_end,
        )
        _add_transaction(
            session,
            "batch-1",
            transaction_id="transaction-1",
            transaction_date=transaction_date,
        )
        _add_balance(
            session,
            snapshot_id="00000000-0000-0000-0000-000000000001",
            as_of_date=balance_date,
        )


@pytest.mark.parametrize(
    "balance",
    [Decimal("100.00"), Decimal("0.00"), Decimal("-25.50")],
)
def test_manual_balance_is_verified_evidence_not_a_transaction(
    factory: sessionmaker[Session],
    balance: Decimal,
) -> None:
    _seed_account(factory)

    snapshot = record_manual_balance(
        factory,
        ManualBalanceEntry(
            account_id="account-1",
            balance=balance,
            as_of_date=date(2026, 8, 9),
            recorded_at=RECORDED_AT,
        ),
    )

    assert snapshot.balance == balance
    assert snapshot.source is BalanceSnapshotSource.MANUAL
    assert snapshot.source_document_id is None
    with session_scope(factory) as session:
        stored = session.get(BalanceSnapshotRecord, str(snapshot.snapshot_id))
        assert stored is not None
        assert stored.import_batch_id is None
        assert stored.verification_status == "verified"
        for model in (
            ImportBatchRecord,
            ImportContextRecord,
            StatementCoverageRecord,
            RawTransactionRecord,
            VerifiedTransactionRecord,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_manual_balance_rejects_missing_inactive_and_mismatched_accounts(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = ManualBalanceEntry(
        account_id="account-1",
        balance=Decimal("10.00"),
        as_of_date=date(2026, 8, 9),
        recorded_at=RECORDED_AT,
    )
    with pytest.raises(BalanceServiceError) as missing:
        record_manual_balance(factory, entry)
    assert missing.value.code is BalanceServiceErrorCode.ACCOUNT_NOT_FOUND

    _seed_account(factory, active=False)
    with pytest.raises(BalanceServiceError) as inactive:
        record_manual_balance(factory, entry)
    assert inactive.value.code is BalanceServiceErrorCode.ACCOUNT_INACTIVE

    monkeypatch.setattr(
        AccountRepository,
        "get",
        lambda self, account_id: SimpleNamespace(is_active=True, currency="EUR"),
    )
    with pytest.raises(BalanceServiceError) as mismatch:
        record_manual_balance(factory, entry)
    assert mismatch.value.code is BalanceServiceErrorCode.ACCOUNT_CURRENCY_MISMATCH


@pytest.mark.parametrize(
    "values",
    [
        {
            "account_id": "account-1",
            "balance": "10.00",
            "as_of_date": "2026-08-10",
            "recorded_at": RECORDED_AT,
        },
        {
            "account_id": "account-1",
            "balance": "10.00",
            "as_of_date": "2026-08-09",
            "recorded_at": datetime(2026, 8, 9, 12, 0),
        },
    ],
)
def test_manual_balance_requires_a_non_future_date_and_aware_time(
    values: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ManualBalanceEntry.model_validate(values)


def test_manual_balance_write_rolls_back_on_failure(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)

    def fail_add(
        repository: BalanceSnapshotRepository,
        record: BalanceSnapshotRecord,
    ) -> BalanceSnapshotRecord:
        del repository, record
        raise RuntimeError("synthetic balance failure")

    monkeypatch.setattr(BalanceSnapshotRepository, "add", fail_add)
    with pytest.raises(RuntimeError, match="synthetic balance failure"):
        record_manual_balance(
            factory,
            ManualBalanceEntry(
                account_id="account-1",
                balance=Decimal("10.00"),
                as_of_date=date(2026, 8, 9),
                recorded_at=RECORDED_AT,
            ),
        )

    with session_scope(factory) as session:
        assert (
            session.scalar(select(func.count()).select_from(BalanceSnapshotRecord)) == 0
        )


def test_balance_repository_uses_date_then_same_day_source_priority(
    factory: sessionmaker[Session],
) -> None:
    _seed_account(factory)
    with session_scope(factory) as session:
        _add_batch(session, "balance-batch")
        _add_balance(
            session,
            snapshot_id="older-manual",
            as_of_date=date(2026, 8, 4),
        )
        newer_running = _add_balance(
            session,
            snapshot_id="newer-running",
            as_of_date=date(2026, 8, 5),
            source="running_balance",
            batch_id="balance-batch",
        )
        for source, snapshot_id in (
            ("statement_opening", "same-opening"),
            ("running_balance", "same-running"),
            ("statement_closing", "same-closing"),
        ):
            _add_balance(
                session,
                snapshot_id=snapshot_id,
                as_of_date=date(2026, 8, 6),
                source=source,
                batch_id="balance-batch",
            )
        same_day_manual = _add_balance(
            session,
            snapshot_id="same-manual",
            as_of_date=date(2026, 8, 6),
        )
        _add_balance(
            session,
            snapshot_id="unverified-newer",
            as_of_date=date(2026, 8, 8),
            status="needs_review",
        )
        _add_balance(
            session,
            snapshot_id="future",
            as_of_date=date(2026, 8, 10),
        )

        repository = BalanceSnapshotRepository(session)
        assert (
            repository.latest_verified_for_account(
                "account-1", as_of_date=date(2026, 8, 5)
            )
            is newer_running
        )
        assert (
            repository.latest_verified_for_account(
                "account-1", as_of_date=date(2026, 8, 9)
            )
            is same_day_manual
        )
        assert (
            repository.latest_verified_for_account(
                "missing", as_of_date=date(2026, 8, 9)
            )
            is None
        )


def test_balance_repository_uses_recording_time_then_id_as_stable_ties(
    factory: sessionmaker[Session],
) -> None:
    _seed_account(factory)
    earlier = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
    later = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    with session_scope(factory) as session:
        _add_balance(
            session,
            snapshot_id="a",
            as_of_date=date(2026, 8, 9),
            recorded_at=earlier,
        )
        _add_balance(
            session,
            snapshot_id="b",
            as_of_date=date(2026, 8, 9),
            recorded_at=later,
        )
        latest_recording = _add_balance(
            session,
            snapshot_id="c",
            as_of_date=date(2026, 8, 9),
            recorded_at=later,
        )

        selected = BalanceSnapshotRepository(session).latest_verified_for_account(
            "account-1", as_of_date=date(2026, 8, 9)
        )
        assert selected is latest_recording


def test_latest_transaction_and_verified_coverage_queries_respect_cutoff_and_status(
    factory: sessionmaker[Session],
) -> None:
    _seed_account(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "verified-batch",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        eligible = _add_transaction(
            session,
            "verified-batch",
            transaction_id="eligible",
            transaction_date=date(2026, 7, 30),
        )
        _add_transaction(
            session,
            "verified-batch",
            transaction_id="future",
            transaction_date=date(2026, 8, 10),
        )
        _add_coverage(
            session,
            "review-batch",
            start=date(2026, 8, 1),
            end=date(2026, 8, 5),
            verification_status="needs_review",
        )
        _add_coverage(
            session,
            "future-batch",
            start=date(2026, 8, 1),
            end=date(2026, 8, 10),
        )

        transactions = TransactionRepository(session)
        statements = StatementRepository(session)
        assert (
            transactions.latest_verified_date("account-1", as_of_date=date(2026, 8, 9))
            == eligible.transaction_date
        )
        assert (
            transactions.latest_verified_date("missing", as_of_date=date(2026, 8, 9))
            is None
        )
        coverages = statements.list_verified_coverages_for_account(
            "account-1", as_of_date=date(2026, 8, 9)
        )
        assert [record.id for record in coverages] == ["coverage-verified-batch"]


def test_complete_adjacent_coverage_can_enter_active_forecasting_mode(
    factory: sessionmaker[Session],
) -> None:
    _seed_account(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "batch-1",
            start=date(2026, 7, 10),
            end=date(2026, 7, 31),
        )
        _add_coverage(
            session,
            "batch-2",
            start=date(2026, 8, 1),
            end=date(2026, 8, 9),
            status="overlapping",
        )
        _add_transaction(
            session,
            "batch-2",
            transaction_id="transaction-1",
            transaction_date=date(2026, 8, 9),
        )
        _add_balance(
            session,
            snapshot_id="balance-1",
            as_of_date=date(2026, 8, 5),
        )

    result = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 9),
        policy=FreshnessPolicy(
            max_transaction_age_days=0,
            max_balance_age_days=4,
            max_coverage_age_days=0,
            minimum_contiguous_coverage_days=31,
        ),
    )

    assert result.mode is FinancialDataMode.ACTIVE_FORECASTING
    assert result.warnings == ()
    assert result.latest_contiguous_coverage == DateRange(
        start_date=date(2026, 7, 10), end_date=date(2026, 8, 9)
    )
    assert result.contiguous_coverage_days == 31
    assert result.transaction_age_days == 0
    assert result.balance_age_days == 4
    assert result.data_freshness_days == 0


def test_plan_example_reports_five_day_freshness_and_old_transaction_warning(
    factory: sessionmaker[Session],
) -> None:
    _seed_complete_evidence(
        factory,
        transaction_date=date(2026, 6, 30),
        balance_date=date(2026, 8, 4),
        coverage_start=date(2026, 6, 1),
        coverage_end=date(2026, 6, 30),
    )

    result = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 9),
        policy=FreshnessPolicy(
            max_transaction_age_days=30,
            max_balance_age_days=10,
            max_coverage_age_days=50,
            minimum_contiguous_coverage_days=30,
        ),
    )

    assert result.latest_transaction_date == date(2026, 6, 30)
    assert result.latest_verified_balance is not None
    assert result.latest_verified_balance.balance == Decimal("940.00")
    assert result.latest_verified_balance.as_of_date == date(2026, 8, 4)
    assert result.transaction_age_days == 40
    assert result.balance_age_days == 5
    assert result.data_freshness_days == 5
    assert result.warnings == (FreshnessWarningCode.TRANSACTIONS_STALE,)
    assert result.mode is FinancialDataMode.ARCHIVE


def test_missing_evidence_is_unknown_not_zero(
    factory: sessionmaker[Session],
) -> None:
    _seed_account(factory)

    result = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 9),
        policy=DEFAULT_POLICY,
    )

    assert result.latest_transaction_date is None
    assert result.latest_verified_balance is None
    assert result.data_freshness_days is None
    assert result.latest_contiguous_coverage is None
    assert result.contiguous_coverage_days == 0
    assert result.warnings == (
        FreshnessWarningCode.NO_VERIFIED_TRANSACTIONS,
        FreshnessWarningCode.NO_VERIFIED_BALANCE,
        FreshnessWarningCode.NO_VERIFIED_COVERAGE,
    )


def test_freshness_rejects_a_missing_account(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(BalanceServiceError) as error:
        assess_financial_data_freshness(
            factory,
            account_id="missing",
            as_of_date=date(2026, 8, 9),
            policy=DEFAULT_POLICY,
        )
    assert error.value.code is BalanceServiceErrorCode.ACCOUNT_NOT_FOUND


def test_inactive_account_stays_archive_despite_current_evidence(
    factory: sessionmaker[Session],
) -> None:
    _seed_complete_evidence(factory, account_active=False)

    result = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 9),
        policy=FreshnessPolicy(
            max_transaction_age_days=0,
            max_balance_age_days=4,
            max_coverage_age_days=0,
            minimum_contiguous_coverage_days=31,
        ),
    )

    assert result.warnings == (FreshnessWarningCode.ACCOUNT_INACTIVE,)
    assert result.mode is FinancialDataMode.ARCHIVE


def test_age_thresholds_are_inclusive_then_emit_stable_stale_warnings(
    factory: sessionmaker[Session],
) -> None:
    _seed_complete_evidence(factory)

    boundary = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 10),
        policy=FreshnessPolicy(
            max_transaction_age_days=1,
            max_balance_age_days=5,
            max_coverage_age_days=1,
            minimum_contiguous_coverage_days=31,
        ),
    )
    stale = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 10),
        policy=FreshnessPolicy(
            max_transaction_age_days=0,
            max_balance_age_days=4,
            max_coverage_age_days=0,
            minimum_contiguous_coverage_days=31,
        ),
    )

    assert boundary.mode is FinancialDataMode.ACTIVE_FORECASTING
    assert stale.warnings == (
        FreshnessWarningCode.TRANSACTIONS_STALE,
        FreshnessWarningCode.BALANCE_STALE,
        FreshnessWarningCode.COVERAGE_STALE,
    )


@pytest.mark.parametrize("status", ["partial", "unknown"])
def test_partial_and_unknown_coverage_cannot_prove_continuity(
    factory: sessionmaker[Session],
    status: str,
) -> None:
    _seed_account(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "batch-1",
            start=date(2026, 7, 1),
            end=date(2026, 8, 9),
            status=status,
        )
        _add_transaction(
            session,
            "batch-1",
            transaction_id="transaction-1",
            transaction_date=date(2026, 8, 9),
        )
        _add_balance(
            session,
            snapshot_id="balance-1",
            as_of_date=date(2026, 8, 9),
        )

    result = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 9),
        policy=DEFAULT_POLICY,
    )

    assert result.latest_contiguous_coverage is None
    assert result.warnings == (FreshnessWarningCode.NO_VERIFIED_COVERAGE,)


def test_gapped_coverage_uses_only_the_latest_contiguous_tail(
    factory: sessionmaker[Session],
) -> None:
    _seed_account(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "batch-1",
            start=date(2026, 7, 1),
            end=date(2026, 8, 9),
            status="gapped",
            missing_periods=[{"start_date": "2026-07-20", "end_date": "2026-08-01"}],
        )
        _add_transaction(
            session,
            "batch-1",
            transaction_id="transaction-1",
            transaction_date=date(2026, 8, 9),
        )
        _add_balance(
            session,
            snapshot_id="balance-1",
            as_of_date=date(2026, 8, 9),
        )

    enough = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 9),
        policy=FreshnessPolicy(
            max_transaction_age_days=0,
            max_balance_age_days=0,
            max_coverage_age_days=0,
            minimum_contiguous_coverage_days=8,
        ),
    )
    insufficient = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 9),
        policy=FreshnessPolicy(
            max_transaction_age_days=0,
            max_balance_age_days=0,
            max_coverage_age_days=0,
            minimum_contiguous_coverage_days=9,
        ),
    )

    assert enough.latest_contiguous_coverage == DateRange(
        start_date=date(2026, 8, 2), end_date=date(2026, 8, 9)
    )
    assert enough.mode is FinancialDataMode.ACTIVE_FORECASTING
    assert insufficient.warnings == (
        FreshnessWarningCode.INSUFFICIENT_CONTIGUOUS_COVERAGE,
    )


def test_a_gap_covering_the_entire_statement_proves_no_continuity(
    factory: sessionmaker[Session],
) -> None:
    _seed_account(factory)
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "batch-1",
            start=date(2026, 8, 1),
            end=date(2026, 8, 9),
            status="gapped",
            missing_periods=[{"start_date": "2026-08-01", "end_date": "2026-08-09"}],
        )
        _add_transaction(
            session,
            "batch-1",
            transaction_id="transaction-1",
            transaction_date=date(2026, 8, 9),
        )
        _add_balance(
            session,
            snapshot_id="balance-1",
            as_of_date=date(2026, 8, 9),
        )

    result = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 9),
        policy=DEFAULT_POLICY,
    )

    assert result.latest_contiguous_coverage is None
    assert result.warnings == (FreshnessWarningCode.NO_VERIFIED_COVERAGE,)


def test_latest_transaction_must_belong_to_the_latest_contiguous_coverage(
    factory: sessionmaker[Session],
) -> None:
    _seed_complete_evidence(
        factory,
        transaction_date=date(2026, 8, 9),
        balance_date=date(2026, 8, 9),
        coverage_start=date(2026, 8, 1),
        coverage_end=date(2026, 8, 5),
    )

    result = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 9),
        policy=FreshnessPolicy(
            max_transaction_age_days=0,
            max_balance_age_days=0,
            max_coverage_age_days=4,
            minimum_contiguous_coverage_days=5,
        ),
    )

    assert result.warnings == (
        FreshnessWarningCode.LATEST_TRANSACTION_OUTSIDE_CONTIGUOUS_COVERAGE,
    )


def test_unverified_and_future_balances_are_excluded_but_stale_evidence_is_visible(
    factory: sessionmaker[Session],
) -> None:
    _seed_account(factory)
    with session_scope(factory) as session:
        _add_balance(
            session,
            snapshot_id="old-verified",
            as_of_date=date(2026, 7, 1),
            balance=Decimal("800.00"),
        )
        _add_balance(
            session,
            snapshot_id="recent-unverified",
            as_of_date=date(2026, 8, 8),
            status="unverified",
        )
        _add_balance(
            session,
            snapshot_id="future-verified",
            as_of_date=date(2026, 8, 10),
        )

    result = assess_financial_data_freshness(
        factory,
        account_id="account-1",
        as_of_date=date(2026, 8, 9),
        policy=DEFAULT_POLICY,
    )

    assert result.latest_verified_balance is not None
    assert result.latest_verified_balance.balance == Decimal("800.00")
    assert result.balance_age_days == 39
    assert FreshnessWarningCode.BALANCE_STALE in result.warnings


def _valid_freshness_payload() -> dict[str, Any]:
    return {
        "account_id": "account-1",
        "assessed_on": date(2026, 8, 9),
        "mode": FinancialDataMode.ACTIVE_FORECASTING,
        "latest_transaction_date": date(2026, 8, 9),
        "latest_verified_balance": VerifiedBalanceEvidence(
            balance=Decimal("940.00"),
            currency=Currency.GBP,
            as_of_date=date(2026, 8, 9),
            recorded_at=RECORDED_AT,
            source=BalanceSnapshotSource.MANUAL,
        ),
        "transaction_age_days": 0,
        "balance_age_days": 0,
        "data_freshness_days": 0,
        "latest_contiguous_coverage": DateRange(
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 9)
        ),
        "contiguous_coverage_days": 9,
        "coverage_age_days": 0,
        "warnings": (),
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"transaction_age_days": None},
        {"balance_age_days": None},
        {"data_freshness_days": 1},
        {
            "latest_contiguous_coverage": None,
            "contiguous_coverage_days": 1,
            "coverage_age_days": None,
        },
        {"contiguous_coverage_days": 8},
        {"coverage_age_days": None},
        {"warnings": (FreshnessWarningCode.BALANCE_STALE,)},
        {
            "mode": FinancialDataMode.ARCHIVE,
            "warnings": (),
        },
    ],
)
def test_freshness_contract_rejects_inconsistent_evidence(
    changes: dict[str, Any],
) -> None:
    payload = _valid_freshness_payload()
    payload.update(changes)

    with pytest.raises(ValidationError):
        FinancialDataFreshness.model_validate(payload)
