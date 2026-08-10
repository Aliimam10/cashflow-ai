"""Tests for confirmed, atomic CSV import persistence."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.imports import (
    CsvImportError,
    CsvImportErrorCode,
    calculate_file_hash,
    persist_confirmed_csv,
)
from cashflow_ai.imports.csv_import_service import _duplicate_facts_from_records
from cashflow_ai.persistence import (
    AccountRepository,
    Base,
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
    CoverageStatus,
    CsvColumnMapping,
    CsvImportConfirmation,
    CsvImportPlan,
    DateRange,
    ImportContext,
    StatementBalances,
    StatementCoverage,
    StatementFlag,
)

CSV_CONTENT = (
    b"Date,Description,Amount,Balance,Transaction ID\n"
    b"2026-07-01,Coffee,-4.50,995.50,new-1\n"
    b"2026-07-02,Coffee,-4.50,991.00,new-1\n"
    b"2026-07-03,Coffee,-4.50,986.50,\n"
    b"2026-07-02,Groceries,-20.00,966.50,new-2\n"
    b"31/02/2026,Broken date,-8.00,978.50,bad-1\n"
)
CONFIRMED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Engine:
    database_engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    return database_engine


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def seed_account(factory: sessionmaker[Session]) -> None:
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
            )
        )
        session.add(FinancialRoleRecord(id="unknown", name="Unknown"))


def import_plan() -> CsvImportPlan:
    return CsvImportPlan(
        account_id="account-1",
        statement_context=ImportContext(
            account_id="account-1",
            coverage=StatementCoverage(
                statement_start_date=date(2026, 7, 1),
                statement_end_date=date(2026, 7, 31),
                status=CoverageStatus.GAPPED,
                missing_periods=(
                    DateRange(
                        start_date=date(2026, 7, 10),
                        end_date=date(2026, 7, 12),
                    ),
                ),
            ),
            balances=StatementBalances(
                opening_balance=Decimal("1000.00"),
                closing_balance=Decimal("978.50"),
            ),
            flags=frozenset(
                {StatementFlag.CONTAINS_REFUNDS, StatementFlag.OTHER_CONTEXT}
            ),
            note="Synthetic fixture with one deliberately invalid row.",
        ),
        mapping=CsvColumnMapping(
            transaction_date_column="Date",
            description_column="Description",
            signed_amount_column="Amount",
            running_balance_column="Balance",
            external_id_column="Transaction ID",
        ),
    )


def confirmation(content: bytes = CSV_CONTENT) -> CsvImportConfirmation:
    return CsvImportConfirmation(
        preview_file_hash=calculate_file_hash(content),
        user_confirmed=True,
        confirmed_at=CONFIRMED_AT,
    )


def import_csv(
    factory: sessionmaker[Session],
    content: bytes = CSV_CONTENT,
) -> object:
    return persist_confirmed_csv(
        factory,
        content,
        "synthetic-statement.csv",
        mime_type="text/csv",
        plan=import_plan(),
        confirmation=confirmation(content),
    )


def test_confirmed_import_preserves_and_classifies_every_row(
    factory: sessionmaker[Session],
) -> None:
    seed_account(factory)

    summary = persist_confirmed_csv(
        factory,
        CSV_CONTENT,
        "synthetic-statement.csv",
        mime_type="text/csv",
        plan=import_plan(),
        confirmation=confirmation(),
    )

    assert summary.new_transactions == 2
    assert summary.exact_duplicates_skipped == 1
    assert summary.probable_duplicates == 1
    assert summary.rejected_rows == 1
    assert summary.exact_duplicate_rows == (3,)
    assert summary.probable_duplicate_rows == (4,)
    assert summary.rejected_row_numbers == (6,)
    assert summary.repeated_file is False
    assert summary.coverage.previous_statement_count == 0
    assert summary.coverage.new_missing_periods == (
        DateRange(start_date=date(2026, 7, 10), end_date=date(2026, 7, 12)),
    )

    with session_scope(factory) as session:
        batch = session.get(ImportBatchRecord, summary.import_batch_id)
        assert batch is not None
        assert batch.verification_status == "needs_review"
        assert batch.source_filename == "synthetic-statement.csv"
        assert batch.imported_at == CONFIRMED_AT

        context = session.scalar(select(ImportContextRecord))
        coverage = session.scalar(select(StatementCoverageRecord))
        balances = tuple(
            session.scalars(
                select(BalanceSnapshotRecord).order_by(BalanceSnapshotRecord.source)
            )
        )
        raw_rows = tuple(
            session.scalars(
                select(RawTransactionRecord).order_by(
                    RawTransactionRecord.source_row_number
                )
            )
        )
        verified = tuple(session.scalars(select(VerifiedTransactionRecord)))

        assert context is not None
        assert context.flags_json == ["contains_refunds", "other_context"]
        assert context.note == "Synthetic fixture with one deliberately invalid row."
        assert coverage is not None
        assert coverage.missing_periods_json == [
            {"start_date": "2026-07-10", "end_date": "2026-07-12"}
        ]
        assert [(item.source, item.balance) for item in balances] == [
            ("statement_closing", Decimal("978.50")),
            ("statement_opening", Decimal("1000.00")),
        ]
        assert [item.review_status for item in raw_rows] == [
            "confirmed",
            "rejected",
            "needs_review",
            "confirmed",
            "rejected",
        ]
        assert raw_rows[1].issues_json[0]["code"] == "exact_duplicate"
        assert raw_rows[2].issues_json[0]["code"] == "probable_duplicate"
        assert raw_rows[4].issues_json[0]["code"] == "invalid_date"
        assert raw_rows[-1].canonical_fingerprint is None
        assert raw_rows[-1].raw_payload["Date"] == "31/02/2026"
        assert len(verified) == 2
        assert verified[0].amount == Decimal("-4.50")
        assert verified[0].financial_role_id == "unknown"


def test_repeated_file_returns_existing_batch_without_writing_again(
    factory: sessionmaker[Session],
) -> None:
    seed_account(factory)
    first = persist_confirmed_csv(
        factory,
        CSV_CONTENT,
        "synthetic-statement.csv",
        mime_type="text/csv",
        plan=import_plan(),
        confirmation=confirmation(),
    )

    repeated = persist_confirmed_csv(
        factory,
        CSV_CONTENT,
        "renamed.csv",
        mime_type="application/csv",
        plan=import_plan(),
        confirmation=confirmation(),
    )

    assert repeated.import_batch_id == first.import_batch_id
    assert repeated.repeated_file is True
    assert repeated.new_transactions == 0
    assert repeated.exact_duplicates_skipped == 5
    assert repeated.exact_duplicate_rows == (2, 3, 4, 5, 6)
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchRecord)) == 1
        assert (
            session.scalar(select(func.count()).select_from(RawTransactionRecord)) == 5
        )


def test_separate_amount_columns_and_optional_balances_are_supported(
    factory: sessionmaker[Session],
) -> None:
    seed_account(factory)
    separate_content = (
        b"Date,Description,Debit,Credit\n"
        b"2026-06-01,Synthetic Rent,700.00,\n"
        b"2026-06-02,Synthetic Salary,,2000.00\n"
    )
    no_balance_plan = CsvImportPlan(
        account_id="account-1",
        statement_context=ImportContext(
            account_id="account-1",
            coverage=StatementCoverage(
                statement_start_date=date(2026, 6, 1),
                statement_end_date=date(2026, 6, 30),
                status=CoverageStatus.COMPLETE,
            ),
        ),
        mapping=CsvColumnMapping(
            transaction_date_column="Date",
            description_column="Description",
            debit_amount_column="Debit",
            credit_amount_column="Credit",
        ),
    )

    summary = persist_confirmed_csv(
        factory,
        separate_content,
        "separate-amounts.csv",
        mime_type="text/plain",
        plan=no_balance_plan,
        confirmation=confirmation(separate_content),
    )

    assert summary.new_transactions == 2
    with session_scope(factory) as session:
        raw_rows = tuple(
            session.scalars(
                select(RawTransactionRecord).order_by(
                    RawTransactionRecord.source_row_number
                )
            )
        )
        assert [item.original_amount_text for item in raw_rows] == [
            "700.00",
            "2000.00",
        ]
        assert tuple(session.scalars(select(BalanceSnapshotRecord))) == ()

    closing_content = b"Date,Description,Amount\n2026-05-01,Synthetic Bill,-50.00\n"
    closing_only_plan = CsvImportPlan(
        account_id="account-1",
        statement_context=ImportContext(
            account_id="account-1",
            coverage=StatementCoverage(
                statement_start_date=date(2026, 5, 1),
                statement_end_date=date(2026, 5, 31),
                status=CoverageStatus.COMPLETE,
            ),
            balances=StatementBalances(closing_balance=Decimal("950.00")),
        ),
        mapping=CsvColumnMapping(
            transaction_date_column="Date",
            description_column="Description",
            signed_amount_column="Amount",
        ),
    )
    persist_confirmed_csv(
        factory,
        closing_content,
        "closing-balance.csv",
        mime_type="text/csv",
        plan=closing_only_plan,
        confirmation=confirmation(closing_content),
    )

    with session_scope(factory) as session:
        balances = tuple(session.scalars(select(BalanceSnapshotRecord)))
        assert len(balances) == 1
        assert balances[0].source == "statement_closing"


def test_verified_record_without_canonical_identity_is_rejected() -> None:
    raw = RawTransactionRecord(
        source_fingerprint="a" * 64,
        canonical_fingerprint=None,
    )
    verified = VerifiedTransactionRecord(
        account_id="account-1",
        transaction_date=date(2026, 7, 1),
        amount=Decimal("-1.00"),
        description="Synthetic row",
        merchant=None,
        external_id=None,
    )

    with pytest.raises(RuntimeError, match="canonical fingerprint"):
        _duplicate_facts_from_records(verified, raw)


@pytest.mark.parametrize(
    ("confirmation_value", "mime_type", "expected_code"),
    [
        (None, "text/csv", CsvImportErrorCode.CONFIRMATION_REQUIRED),
        (confirmation(), "application/pdf", CsvImportErrorCode.UNSUPPORTED_MIME_TYPE),
        (
            CsvImportConfirmation(
                preview_file_hash="a" * 64,
                user_confirmed=True,
                confirmed_at=CONFIRMED_AT,
            ),
            "text/csv",
            CsvImportErrorCode.PREVIEW_CHANGED,
        ),
    ],
)
def test_confirmation_mime_and_exact_preview_bytes_are_required(
    factory: sessionmaker[Session],
    confirmation_value: CsvImportConfirmation | None,
    mime_type: str,
    expected_code: CsvImportErrorCode,
) -> None:
    with pytest.raises(CsvImportError) as error:
        persist_confirmed_csv(
            factory,
            CSV_CONTENT,
            "statement.csv",
            mime_type=mime_type,
            plan=import_plan(),
            confirmation=confirmation_value,
        )

    assert error.value.code is expected_code


def test_import_requires_an_existing_matching_account(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(CsvImportError) as missing:
        import_csv(factory)
    assert missing.value.code is CsvImportErrorCode.ACCOUNT_NOT_FOUND

    monkeypatch.setattr(
        AccountRepository,
        "get",
        lambda self, account_id: SimpleNamespace(currency="EUR"),
    )
    with pytest.raises(CsvImportError) as mismatch:
        import_csv(factory)
    assert mismatch.value.code is CsvImportErrorCode.ACCOUNT_CURRENCY_MISMATCH


def test_database_failure_rolls_back_the_complete_import(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_account(factory)

    def fail_verified_write(
        repository: TransactionRepository,
        transaction: VerifiedTransactionRecord,
    ) -> VerifiedTransactionRecord:
        del repository, transaction
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(TransactionRepository, "add_verified", fail_verified_write)
    with pytest.raises(RuntimeError, match="synthetic database failure"):
        import_csv(factory)

    with session_scope(factory) as session:
        for model in (
            ImportBatchRecord,
            ImportContextRecord,
            StatementCoverageRecord,
            BalanceSnapshotRecord,
            RawTransactionRecord,
            VerifiedTransactionRecord,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_confirmation_contract_rejects_false_or_naive_confirmation() -> None:
    with pytest.raises(ValidationError):
        CsvImportConfirmation.model_validate(
            {
                "preview_file_hash": "a" * 64,
                "user_confirmed": False,
                "confirmed_at": CONFIRMED_AT,
            }
        )
    with pytest.raises(ValidationError):
        CsvImportConfirmation(
            preview_file_hash="a" * 64,
            user_confirmed=True,
            confirmed_at=datetime(2026, 8, 10, 12, 0),
        )
