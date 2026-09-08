"""Tests for atomic, review-gated digital-PDF persistence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import cashflow_ai.imports.pdf_persistence as pdf_persistence_module
from cashflow_ai.imports.normalisation import (
    calculate_canonical_fingerprint,
    calculate_file_hash,
    calculate_source_fingerprint,
)
from cashflow_ai.imports.pdf_persistence import (
    PdfPersistenceError,
    PdfPersistenceErrorCode,
    persist_approved_pdf,
)
from cashflow_ai.imports.reconciliation import reconcile_statement
from cashflow_ai.persistence import (
    AccountRepository,
    Base,
    UserProfileRepository,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import (
    AccountRecord,
    BalanceSnapshotRecord,
    FinancialDataRevisionRecord,
    FinancialRoleRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.persistence.repositories import TransactionRepository
from cashflow_ai.schemas.csv_imports import CsvCoverageAnalysis
from cashflow_ai.schemas.imports import (
    ExtractionMethod,
    ExtractionProvenance,
    ImportIssue,
    IssueSeverity,
    ParserIdentity,
    SourceType,
)
from cashflow_ai.schemas.normalisation import (
    OriginalTransactionValues,
    SourceFieldValue,
    SourceRecordIdentity,
)
from cashflow_ai.schemas.pdf_persistence import PdfImportSummary, PdfRecordLocation
from cashflow_ai.schemas.reconciliation import (
    AmountSignConvention,
    ApprovedReviewRow,
    ApprovedStatement,
    DateFormat,
    ReviewReason,
    RowDecision,
    StatementBalanceEvidence,
    StatementBalanceField,
    StatementReviewRow,
)
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    DateRange,
    StatementBalances,
    StatementCoverage,
)
from cashflow_ai.schemas.transactions import (
    CanonicalTransaction,
    Currency,
    Direction,
    FinancialRole,
    TransactionDraft,
)

CONTENT = b"%PDF-1.7\n% fictional persistence fixture\n"
FILE_HASH = calculate_file_hash(CONTENT)
RECEIVED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
APPROVED_AT = RECEIVED_AT - timedelta(minutes=1)
PARSER = ParserIdentity(name="fictional_digital_pdf_adapter", version="2.1.0")
COVERAGE = StatementCoverage(
    statement_start_date=date(2026, 8, 1),
    statement_end_date=date(2026, 8, 31),
    status=CoverageStatus.COMPLETE,
)


@pytest.fixture(autouse=True)
def _isolate_persistence_writes_from_pdf_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep these persistence-unit tests focused below the trust boundary."""

    def use_supplied_fixture(
        _content: bytes,
        _safe_filename: str,
        *,
        statement: ApprovedStatement,
        **_kwargs: object,
    ) -> ApprovedStatement:
        return statement

    monkeypatch.setattr(
        pdf_persistence_module,
        "_reextract_approved_statement",
        use_supplied_fixture,
    )


@pytest.fixture
def engine() -> Engine:
    database_engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    return database_engine


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _seed_account(
    factory: sessionmaker[Session],
    *,
    account_id: str = "account-1",
    active: bool = True,
    currency: str = "GBP",
) -> None:
    with session_scope(factory) as session:
        UserProfileRepository(session).add(
            UserProfileRecord(
                id="profile-1",
                display_name="Fictional User",
                base_currency="GBP",
                timezone="Europe/London",
            )
        )
        AccountRepository(session).add(
            AccountRecord(
                id=account_id,
                user_profile_id="profile-1",
                name="Fictional Current Account",
                account_type="current",
                currency=currency,
                is_active=active,
            )
        )
        session.add(FinancialRoleRecord(id="unknown", name="Unknown"))


def _original(
    *,
    description: str,
    amount_text: str,
    transaction_date: str = "2026-08-05",
) -> OriginalTransactionValues:
    return OriginalTransactionValues(
        transaction_date_text=transaction_date,
        description_text=description,
        signed_amount_text=amount_text,
        raw_fields=(
            SourceFieldValue(column="Date", value=transaction_date),
            SourceFieldValue(column="Description", value=description),
            SourceFieldValue(column="Amount", value=amount_text),
        ),
    )


def _identity(page: int, record: int) -> SourceRecordIdentity:
    return SourceRecordIdentity(
        source_type=SourceType.DIGITAL_PDF,
        source_document_hash=FILE_HASH,
        page_number=page,
        page_record_number=record,
    )


def _provenance(page: int) -> ExtractionProvenance:
    return ExtractionProvenance(
        source_type=SourceType.DIGITAL_PDF,
        method=ExtractionMethod.PDF_TABLE,
        page_number=page,
        parser=PARSER,
    )


def _draft(
    *,
    description: str,
    amount: Decimal,
    balance: Decimal | None,
    external_id: str | None = None,
    account_id: str = "account-1",
    currency: Currency = Currency.GBP,
    transaction_date: date = date(2026, 8, 5),
    posting_date: date | None = None,
    category_id: str | None = None,
    financial_role: FinancialRole = FinancialRole.UNKNOWN,
) -> TransactionDraft:
    return TransactionDraft(
        transaction_date=transaction_date,
        posting_date=posting_date,
        description=description,
        merchant=description.title(),
        amount=amount,
        balance_after=balance,
        currency=currency,
        account_id=account_id,
        external_id=external_id,
        direction=Direction.INFLOW if amount > 0 else Direction.OUTFLOW,
        category_id=category_id,
        financial_role=financial_role,
    )


def _canonical(draft: TransactionDraft) -> CanonicalTransaction:
    return CanonicalTransaction.model_validate(draft.model_dump())


def _approved_row(
    page: int,
    record: int,
    *,
    final: TransactionDraft,
    extracted: TransactionDraft | None = None,
) -> ApprovedReviewRow:
    extracted_draft = final if extracted is None else extracted
    assert extracted_draft.description is not None
    assert extracted_draft.amount is not None
    assert extracted_draft.transaction_date is not None
    original = _original(
        description=extracted_draft.description,
        amount_text=format(extracted_draft.amount, ".2f"),
        transaction_date=extracted_draft.transaction_date.isoformat(),
    )
    identity = _identity(page, record)
    return ApprovedReviewRow(
        source_identity=identity,
        source_fingerprint=calculate_source_fingerprint(identity, original),
        original=original,
        extracted_draft=extracted_draft,
        provenance=_provenance(page),
        row_decision=RowDecision.CONFIRM,
        transaction=_canonical(final),
        was_edited=final != extracted_draft,
    )


def _rejected_row(page: int, record: int) -> StatementReviewRow:
    original = _original(description="FICTIONAL NON-TRANSACTION", amount_text="-1.00")
    identity = _identity(page, record)
    extracted = _draft(
        description="FICTIONAL NON-TRANSACTION",
        amount=Decimal("-1.00"),
        balance=None,
    )
    issue = ImportIssue(
        code="ambiguous_record",
        message="the extracted record requires explicit review",
        severity=IssueSeverity.ERROR,
    )
    return StatementReviewRow(
        source_identity=identity,
        source_fingerprint=calculate_source_fingerprint(identity, original),
        original=original,
        extracted_draft=extracted,
        working_draft=extracted,
        provenance=_provenance(page),
        issues=(issue,),
        review_reasons=frozenset({ReviewReason.EXTRACTION_ERROR}),
    )


def _balance_evidence(
    field: StatementBalanceField,
    amount: Decimal,
    *,
    record: int,
) -> StatementBalanceEvidence:
    return StatementBalanceEvidence(
        field=field,
        raw_amount_text=f"GBP {amount:.2f}",
        amount=amount,
        source_identity=_identity(1, record),
        provenance=_provenance(1),
        line_number=record,
    )


def _statement(
    rows: tuple[ApprovedReviewRow, ...],
    *,
    rejected: tuple[StatementReviewRow, ...] = (),
    opening: Decimal = Decimal("1000.00"),
    closing: Decimal | None = None,
) -> ApprovedStatement:
    final_closing = closing
    if final_closing is None:
        final_closing = opening + sum(
            (row.transaction.amount for row in rows), Decimal("0.00")
        )
    balances = StatementBalances(
        opening_balance=opening,
        closing_balance=final_closing,
    )
    return ApprovedStatement(
        file_hash=FILE_HASH,
        source_type=SourceType.DIGITAL_PDF,
        approved_at=APPROVED_AT,
        date_format=DateFormat.ISO,
        sign_convention=AmountSignConvention.SIGNED_AMOUNT,
        statement_coverage=COVERAGE,
        coverage_was_edited=False,
        balances=balances,
        balance_evidence=(
            _balance_evidence(
                StatementBalanceField.OPENING,
                opening,
                record=90,
            ),
            _balance_evidence(
                StatementBalanceField.CLOSING,
                final_closing,
                record=91,
            ),
        ),
        balance_was_edited=False,
        document_issues=(
            ImportIssue(
                code="fictional_layout",
                message="the fictional layout was extracted deterministically",
                severity=IssueSeverity.WARNING,
            ),
        ),
        rows=rows,
        rejected_rows=rejected,
        rejected_source_fingerprints=tuple(row.source_fingerprint for row in rejected),
        reconciliation=reconcile_statement(
            balances,
            (row.transaction.amount for row in rows),
        ),
    )


def _seed_duplicate_evidence(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        for index, external_id in ((1, "existing-exact"), (2, None)):
            original = _original(
                description="FICTIONAL CAFE",
                amount_text="-4.50",
            )
            identity = SourceRecordIdentity(
                source_type=SourceType.CSV,
                source_document_hash=str(index) * 64,
                source_row_number=index + 1,
            )
            transaction = _canonical(
                _draft(
                    description="FICTIONAL CAFE",
                    amount=Decimal("-4.50"),
                    balance=None,
                    external_id=external_id,
                )
            )
            raw = RawTransactionRecord(
                id=f"existing-raw-{index}",
                import_batch_id=f"existing-batch-{index}",
                source_type="csv",
                source_row_number=index + 1,
                page_number=None,
                page_record_number=None,
                raw_payload={"fixture": f"existing-{index}"},
                original_date_text=original.transaction_date_text,
                original_description=original.description_text,
                original_amount_text=original.signed_amount_text,
                parser_name="fictional_csv",
                parser_version="1.0",
                source_fingerprint=calculate_source_fingerprint(identity, original),
                canonical_fingerprint=calculate_canonical_fingerprint(transaction),
                candidate_json=None,
                issues_json=[],
                review_status="confirmed",
                created_at=RECEIVED_AT - timedelta(days=40),
            )
            session.add(
                ImportBatchRecord(
                    id=f"existing-batch-{index}",
                    account_id="account-1",
                    source_type="csv",
                    source_filename=f"existing-{index}.csv",
                    file_hash=str(index) * 64,
                    mime_type="text/csv",
                    byte_size=100,
                    verification_status="verified",
                    imported_at=RECEIVED_AT - timedelta(days=40),
                )
            )
            session.add(raw)
            session.add(
                VerifiedTransactionRecord(
                    id=f"existing-transaction-{index}",
                    raw_transaction_id=raw.id,
                    account_id="account-1",
                    transaction_date=transaction.transaction_date,
                    description=transaction.description,
                    merchant=transaction.merchant,
                    amount=transaction.amount,
                    currency="GBP",
                    external_id=external_id,
                    direction="outflow",
                    category_id=None,
                    financial_role_id="unknown",
                    verified_at=RECEIVED_AT - timedelta(days=40),
                )
            )


def _mixed_statement() -> tuple[ApprovedStatement, ApprovedReviewRow]:
    exact = _approved_row(
        1,
        1,
        final=_draft(
            description="FICTIONAL CAFE",
            amount=Decimal("-4.50"),
            balance=Decimal("995.50"),
            external_id="existing-exact",
        ),
    )
    probable = _approved_row(
        1,
        2,
        final=_draft(
            description="FICTIONAL CAFE",
            amount=Decimal("-4.50"),
            balance=Decimal("991.00"),
        ),
    )
    extracted = _draft(
        description="FICTIONAL PAY",
        amount=Decimal("90.00"),
        balance=Decimal("1081.00"),
        external_id="new-pay",
    )
    corrected = _approved_row(
        1,
        3,
        extracted=extracted,
        final=_draft(
            description="FICTIONAL SALARY",
            amount=Decimal("100.00"),
            balance=Decimal("1091.00"),
            external_id="new-pay",
        ),
    )
    return _statement(
        (exact, probable, corrected), rejected=(_rejected_row(2, 1),)
    ), corrected


def test_pdf_persistence_preserves_rows_and_classifies_duplicates_atomically(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)
    _seed_duplicate_evidence(factory)
    statement, corrected = _mixed_statement()
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )

    summary = persist_approved_pdf(
        factory,
        CONTENT,
        "../../fictional-statement.pdf",
        mime_type="application/pdf",
        statement=statement,
    )

    assert summary.imported_transactions == 1
    assert summary.exact_duplicates_skipped == 1
    assert summary.probable_duplicates == 1
    assert summary.rejected_rows == 1
    assert summary.exact_duplicate_locations[0].page_record_number == 1
    assert summary.probable_duplicate_locations[0].page_record_number == 2
    assert summary.rejected_locations[0].page_number == 2
    assert summary.coverage.previous_statement_count == 0

    with session_scope(factory) as session:
        batch = session.get(ImportBatchRecord, summary.import_batch_id)
        assert batch is not None
        assert batch.source_filename == "fictional-statement.pdf"
        assert batch.verification_status == "needs_review"
        assert batch.imported_at == RECEIVED_AT
        context = session.scalar(
            select(ImportContextRecord).where(
                ImportContextRecord.import_batch_id == batch.id
            )
        )
        assert context is not None
        assert context.flags_json == []
        assert context.note is None
        assert context.created_at == RECEIVED_AT
        coverage = session.scalar(
            select(StatementCoverageRecord).where(
                StatementCoverageRecord.import_context_id == context.id
            )
        )
        assert coverage is not None
        assert coverage.statement_start_date == date(2026, 8, 1)

        raw_rows = tuple(
            session.scalars(
                select(RawTransactionRecord)
                .where(RawTransactionRecord.import_batch_id == batch.id)
                .order_by(
                    RawTransactionRecord.page_number,
                    RawTransactionRecord.page_record_number,
                )
            )
        )
        assert [row.review_status for row in raw_rows] == [
            "rejected",
            "needs_review",
            "confirmed",
            "rejected",
        ]
        assert raw_rows[0].issues_json[-1]["code"] == "exact_duplicate"
        assert raw_rows[1].issues_json[-1]["code"] == "probable_duplicate"
        assert raw_rows[2].issues_json == []
        assert raw_rows[3].issues_json[-1]["code"] == "user_rejected_pdf_row"
        assert all(row.parser_name == PARSER.name for row in raw_rows)
        assert raw_rows[2].raw_payload["schema_version"] == "pdf-source-row-2.0"
        assert raw_rows[2].raw_payload["raw_fields"][1]["value"] == "FICTIONAL PAY"
        assert raw_rows[2].raw_payload["extracted_draft"]["amount"] == "90.00"
        assert raw_rows[2].raw_payload["reviewed_draft"]["amount"] == "100.00"
        assert raw_rows[2].raw_payload["was_edited"] is True
        assert raw_rows[2].raw_payload["row_decision"] == "confirm"
        assert raw_rows[2].raw_payload["document_issues"][0]["code"] == (
            "fictional_layout"
        )
        approval = raw_rows[2].raw_payload["statement_approval"]
        assert approval["approved_at"] == APPROVED_AT.isoformat()
        assert approval["statement_coverage"]["statement_start_date"] == "2026-08-01"
        assert approval["balance_evidence"][0]["raw_amount_text"] == "GBP 1000.00"
        assert approval["reconciliation"]["status"] == "reconciled"
        assert raw_rows[2].canonical_fingerprint == calculate_canonical_fingerprint(
            corrected.transaction
        )
        assert raw_rows[2].canonical_fingerprint != calculate_canonical_fingerprint(
            corrected.extracted_draft
        )
        assert raw_rows[1].candidate_json is not None
        assert raw_rows[1].candidate_json["draft"]["description"] == ("FICTIONAL CAFE")
        assert raw_rows[3].canonical_fingerprint is None

        verified = session.scalar(
            select(VerifiedTransactionRecord).where(
                VerifiedTransactionRecord.raw_transaction_id == raw_rows[2].id
            )
        )
        assert verified is not None
        assert verified.description == "FICTIONAL SALARY"
        assert verified.amount == Decimal("100.00")
        assert verified.verified_at == RECEIVED_AT
        balances = tuple(
            session.scalars(
                select(BalanceSnapshotRecord)
                .where(BalanceSnapshotRecord.import_batch_id == batch.id)
                .order_by(BalanceSnapshotRecord.source)
            )
        )
        assert [(item.source, item.balance) for item in balances] == [
            ("running_balance", Decimal("1091.00")),
            ("statement_closing", Decimal("1091.00")),
            ("statement_opening", Decimal("1000.00")),
        ]
        assert {item.recorded_at for item in balances} == {RECEIVED_AT}
        revision = session.get(FinancialDataRevisionRecord, "account-1")
        assert revision is not None
        assert revision.revision == 1

    repeated = persist_approved_pdf(
        factory,
        CONTENT,
        "fictional-statement.pdf",
        mime_type="application/pdf",
        statement=statement,
    )
    assert repeated.repeated_file is True
    assert repeated.import_batch_id == summary.import_batch_id
    assert repeated.exact_duplicates_skipped == 4
    assert repeated.exact_duplicate_locations == (
        *summary.exact_duplicate_locations,
        *summary.probable_duplicate_locations,
        PdfRecordLocation(page_number=1, page_record_number=3),
        *summary.rejected_locations,
    )
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchRecord)) == 3
        revision = session.get(FinancialDataRevisionRecord, "account-1")
        assert revision is not None
        assert revision.revision == 1


def test_explicitly_rejected_only_statement_can_be_verified_without_transactions(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    statement = _statement(
        (),
        rejected=(_rejected_row(1, 1),),
        opening=Decimal("100.00"),
        closing=Decimal("100.00"),
    )

    summary = persist_approved_pdf(
        factory,
        CONTENT,
        "rejected-only.pdf",
        mime_type="application/pdf",
        statement=statement,
    )

    assert summary.imported_transactions == 0
    assert summary.rejected_rows == 1
    with session_scope(factory) as session:
        batch = session.get(ImportBatchRecord, summary.import_batch_id)
        assert batch is not None
        assert batch.verification_status == "verified"
        assert (
            session.scalar(select(func.count()).select_from(VerifiedTransactionRecord))
            == 0
        )


def test_unexpected_write_failure_rolls_back_every_pdf_record(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    row = _approved_row(
        1,
        1,
        final=_draft(
            description="FICTIONAL SHOP",
            amount=Decimal("-10.00"),
            balance=Decimal("90.00"),
        ),
    )
    statement = _statement(
        (row,),
        opening=Decimal("100.00"),
        closing=Decimal("90.00"),
    )

    def fail_verified_write(
        self: TransactionRepository,
        transaction: VerifiedTransactionRecord,
    ) -> VerifiedTransactionRecord:
        del self, transaction
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr(TransactionRepository, "add_verified", fail_verified_write)
    with pytest.raises(RuntimeError, match="synthetic write failure"):
        persist_approved_pdf(
            factory,
            CONTENT,
            "rollback.pdf",
            mime_type="application/pdf",
            statement=statement,
        )

    with session_scope(factory) as session:
        for model in (
            ImportBatchRecord,
            ImportContextRecord,
            StatementCoverageRecord,
            BalanceSnapshotRecord,
            RawTransactionRecord,
            VerifiedTransactionRecord,
            FinancialDataRevisionRecord,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def _expect_persistence_error(
    factory: sessionmaker[Session],
    statement: ApprovedStatement,
    expected: PdfPersistenceErrorCode,
    *,
    content: bytes = CONTENT,
    filename: str = "fictional.pdf",
    mime_type: str = "application/pdf",
    max_bytes: int = len(CONTENT),
) -> PdfPersistenceError:
    with pytest.raises(PdfPersistenceError) as captured:
        persist_approved_pdf(
            factory,
            content,
            filename,
            mime_type=mime_type,
            statement=statement,
            max_bytes=max_bytes,
        )
    assert captured.value.code is expected
    return captured.value


def _one_row_statement() -> ApprovedStatement:
    row = _approved_row(
        1,
        1,
        final=_draft(
            description="FICTIONAL SHOP",
            amount=Decimal("-10.00"),
            balance=Decimal("90.00"),
        ),
    )
    return _statement((row,), opening=Decimal("100.00"), closing=Decimal("90.00"))


@pytest.mark.parametrize(
    ("filename", "mime_type", "content", "max_bytes", "expected"),
    [
        (
            "",
            "application/pdf",
            CONTENT,
            len(CONTENT),
            PdfPersistenceErrorCode.INVALID_FILENAME,
        ),
        (
            "statement.txt",
            "application/pdf",
            CONTENT,
            len(CONTENT),
            PdfPersistenceErrorCode.UNSUPPORTED_FILE_TYPE,
        ),
        (
            "statement.pdf",
            "text/plain",
            CONTENT,
            len(CONTENT),
            PdfPersistenceErrorCode.UNSUPPORTED_MIME_TYPE,
        ),
        (
            "statement.pdf",
            "application/pdf",
            b"",
            len(CONTENT),
            PdfPersistenceErrorCode.EMPTY_FILE,
        ),
        (
            "statement.pdf",
            "application/pdf",
            CONTENT,
            1,
            PdfPersistenceErrorCode.FILE_TOO_LARGE,
        ),
        (
            "statement.pdf",
            "application/pdf",
            b"not a pdf",
            100,
            PdfPersistenceErrorCode.INVALID_SIGNATURE,
        ),
        (
            "statement.pdf",
            "application/pdf",
            CONTENT,
            0,
            PdfPersistenceErrorCode.INVALID_LIMIT,
        ),
    ],
)
def test_pdf_persistence_rejects_unsafe_file_boundaries(
    factory: sessionmaker[Session],
    filename: str,
    mime_type: str,
    content: bytes,
    max_bytes: int,
    expected: PdfPersistenceErrorCode,
) -> None:
    _expect_persistence_error(
        factory,
        _one_row_statement(),
        expected,
        content=content,
        filename=filename,
        mime_type=mime_type,
        max_bytes=max_bytes,
    )


def test_pdf_statement_identity_time_coverage_and_rows_are_revalidated(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    statement = _one_row_statement()
    cases = (
        (
            statement.model_copy(update={"source_type": SourceType.OCR_PDF}),
            PdfPersistenceErrorCode.UNSUPPORTED_SOURCE_TYPE,
        ),
        (
            statement.model_copy(update={"file_hash": "0" * 64}),
            PdfPersistenceErrorCode.FILE_CHANGED,
        ),
        (
            statement.model_copy(
                update={"approved_at": APPROVED_AT.replace(tzinfo=None)}
            ),
            PdfPersistenceErrorCode.INVALID_APPROVAL_TIME,
        ),
        (
            statement.model_copy(
                update={"approved_at": RECEIVED_AT + timedelta(seconds=1)}
            ),
            PdfPersistenceErrorCode.INVALID_APPROVAL_TIME,
        ),
        (
            statement.model_copy(update={"statement_coverage": None}),
            PdfPersistenceErrorCode.COVERAGE_REQUIRED,
        ),
        (
            statement.model_copy(update={"rows": (), "rejected_rows": ()}),
            PdfPersistenceErrorCode.EMPTY_STATEMENT,
        ),
        (
            statement.model_copy(update={"rejected_source_fingerprints": ("f" * 64,)}),
            PdfPersistenceErrorCode.INVALID_LINEAGE,
        ),
    )
    for invalid, expected in cases:
        _expect_persistence_error(factory, invalid, expected)


def test_pdf_row_lineage_duplicates_and_locations_are_revalidated(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    statement = _one_row_statement()
    row = statement.rows[0]

    wrong_fingerprint = row.model_copy(update={"source_fingerprint": "f" * 64})
    _expect_persistence_error(
        factory,
        _statement((wrong_fingerprint,), opening=Decimal("100"), closing=Decimal("90")),
        PdfPersistenceErrorCode.INVALID_LINEAGE,
    )

    no_parser = row.model_copy(
        update={"provenance": row.provenance.model_copy(update={"parser": None})}
    )
    _expect_persistence_error(
        factory,
        _statement((no_parser,), opening=Decimal("100"), closing=Decimal("90")),
        PdfPersistenceErrorCode.INVALID_LINEAGE,
    )

    missing_page = row.model_copy(
        update={
            "source_identity": row.source_identity.model_copy(
                update={"page_number": None}
            )
        }
    )
    _expect_persistence_error(
        factory,
        _statement((missing_page,), opening=Decimal("100"), closing=Decimal("90")),
        PdfPersistenceErrorCode.INVALID_LINEAGE,
    )

    duplicated = _statement(
        (row, row),
        opening=Decimal("100"),
        closing=Decimal("80"),
    )
    _expect_persistence_error(
        factory,
        duplicated,
        PdfPersistenceErrorCode.DUPLICATE_SOURCE_ROW,
    )

    same_location = _approved_row(
        1,
        1,
        final=_draft(
            description="FICTIONAL SECOND SHOP",
            amount=Decimal("-5"),
            balance=Decimal("85"),
        ),
    )
    _expect_persistence_error(
        factory,
        _statement((row, same_location), opening=Decimal("100"), closing=Decimal("85")),
        PdfPersistenceErrorCode.DUPLICATE_SOURCE_ROW,
    )


def test_pdf_reconciliation_requires_exactly_bound_balance_evidence(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    statement = _one_row_statement()
    incomplete = statement.model_copy(update={"balances": None})
    _expect_persistence_error(
        factory,
        incomplete,
        PdfPersistenceErrorCode.RECONCILIATION_REQUIRED,
    )

    duplicated_evidence = statement.model_copy(
        update={
            "balance_evidence": (
                statement.balance_evidence[0],
                statement.balance_evidence[0],
                statement.balance_evidence[1],
            )
        }
    )
    _expect_persistence_error(
        factory,
        duplicated_evidence,
        PdfPersistenceErrorCode.RECONCILIATION_REQUIRED,
    )

    changed_evidence = statement.balance_evidence[1].model_copy(
        update={"amount": Decimal("91.00")}
    )
    unmarked_edit = statement.model_copy(
        update={
            "balance_evidence": (
                statement.balance_evidence[0],
                changed_evidence,
            )
        }
    )
    _expect_persistence_error(
        factory,
        unmarked_edit,
        PdfPersistenceErrorCode.RECONCILIATION_REQUIRED,
    )

    mismatch = statement.model_copy(
        update={
            "balances": StatementBalances(
                opening_balance=Decimal("100"),
                closing_balance=Decimal("80"),
            ),
            "balance_was_edited": True,
            "reconciliation": reconcile_statement(
                StatementBalances(
                    opening_balance=Decimal("100"),
                    closing_balance=Decimal("80"),
                ),
                (Decimal("-10"),),
            ),
        }
    )
    _expect_persistence_error(
        factory,
        mismatch,
        PdfPersistenceErrorCode.RECONCILIATION_FAILED,
    )

    invalid_evidence = statement.balance_evidence[0].model_copy(
        update={
            "provenance": statement.balance_evidence[0].provenance.model_copy(
                update={"parser": None}
            )
        }
    )
    _expect_persistence_error(
        factory,
        statement.model_copy(
            update={
                "balance_evidence": (invalid_evidence, statement.balance_evidence[1])
            }
        ),
        PdfPersistenceErrorCode.INVALID_LINEAGE,
    )


def test_explicit_balance_correction_is_preserved_and_can_reconcile(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    statement = _one_row_statement()
    unreadable_closing = statement.balance_evidence[1].model_copy(
        update={"amount": None, "issues": statement.document_issues}
    )
    corrected = statement.model_copy(
        update={
            "balance_evidence": (
                statement.balance_evidence[0],
                unreadable_closing,
            ),
            "balance_was_edited": True,
        }
    )

    summary = persist_approved_pdf(
        factory,
        CONTENT,
        "corrected-balance.pdf",
        mime_type="application/pdf",
        statement=corrected,
    )

    with session_scope(factory) as session:
        raw = session.scalar(
            select(RawTransactionRecord).where(
                RawTransactionRecord.import_batch_id == summary.import_batch_id
            )
        )
        assert raw is not None
        approval = raw.raw_payload["statement_approval"]
        assert approval["balance_was_edited"] is True
        assert approval["balance_evidence"][1]["issues"][0]["code"] == (
            "fictional_layout"
        )


def test_pdf_transaction_scope_rejects_downstream_fields_and_unsafe_dates(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    categorised = _approved_row(
        1,
        1,
        final=_draft(
            description="FICTIONAL SHOP",
            amount=Decimal("-10"),
            balance=Decimal("90"),
            category_id="shopping",
        ),
    )
    outside_posting_date = _approved_row(
        1,
        1,
        final=_draft(
            description="FICTIONAL SHOP",
            amount=Decimal("-10"),
            balance=Decimal("90"),
            posting_date=date(2026, 9, 1),
        ),
    )
    rejected_decision = (
        _one_row_statement()
        .rows[0]
        .model_copy(update={"row_decision": RowDecision.REJECT})
    )
    stale_edit_flag = (
        _one_row_statement().rows[0].model_copy(update={"was_edited": True})
    )
    for row in (categorised, outside_posting_date, rejected_decision, stale_edit_flag):
        _expect_persistence_error(
            factory,
            _statement((row,), opening=Decimal("100"), closing=Decimal("90")),
            PdfPersistenceErrorCode.INVALID_TRANSACTION_SCOPE,
        )


def test_pdf_scope_requires_one_account_and_immutable_extraction_ownership(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    first = _approved_row(
        1,
        1,
        final=_draft(
            description="FICTIONAL ONE",
            amount=Decimal("-10"),
            balance=Decimal("90"),
        ),
    )
    second_account = _approved_row(
        1,
        2,
        final=_draft(
            description="FICTIONAL TWO",
            amount=Decimal("5"),
            balance=Decimal("95"),
            account_id="account-2",
        ),
    )
    _expect_persistence_error(
        factory,
        _statement(
            (first, second_account),
            opening=Decimal("100"),
            closing=Decimal("95"),
        ),
        PdfPersistenceErrorCode.INVALID_TRANSACTION_SCOPE,
    )

    extracted_other_account = _draft(
        description="FICTIONAL SHOP",
        amount=Decimal("-10"),
        balance=Decimal("90"),
        account_id="account-2",
    )
    ownership_edit = _approved_row(
        1,
        1,
        extracted=extracted_other_account,
        final=_draft(
            description="FICTIONAL SHOP",
            amount=Decimal("-10"),
            balance=Decimal("90"),
        ),
    )
    _expect_persistence_error(
        factory,
        _statement((ownership_edit,), opening=Decimal("100"), closing=Decimal("90")),
        PdfPersistenceErrorCode.INVALID_TRANSACTION_SCOPE,
    )


def test_pdf_account_must_exist_be_active_and_match_currency(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    statement = _one_row_statement()
    _expect_persistence_error(
        factory,
        statement,
        PdfPersistenceErrorCode.ACCOUNT_NOT_FOUND,
    )

    _seed_account(factory, active=False)
    _expect_persistence_error(
        factory,
        statement,
        PdfPersistenceErrorCode.ACCOUNT_INACTIVE,
    )

    monkeypatch.setattr(
        AccountRepository,
        "get",
        lambda self, account_id: type(
            "SyntheticAccount",
            (),
            {"is_active": True, "currency": "USD"},
        )(),
    )
    _expect_persistence_error(
        factory,
        statement,
        PdfPersistenceErrorCode.ACCOUNT_CURRENCY_MISMATCH,
    )


def test_newest_first_statement_and_missing_row_balances_are_supported(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    newest = _approved_row(
        1,
        1,
        final=_draft(
            description="FICTIONAL REFUND",
            amount=Decimal("20"),
            balance=Decimal("105"),
            transaction_date=date(2026, 8, 7),
        ),
    )
    middle_without_balance = _approved_row(
        1,
        2,
        final=_draft(
            description="FICTIONAL FEE",
            amount=Decimal("-5"),
            balance=None,
            transaction_date=date(2026, 8, 6),
        ),
    )
    oldest = _approved_row(
        1,
        3,
        final=_draft(
            description="FICTIONAL SHOP",
            amount=Decimal("-10"),
            balance=Decimal("90"),
            transaction_date=date(2026, 8, 5),
        ),
    )
    statement = _statement(
        (newest, middle_without_balance, oldest),
        opening=Decimal("100"),
        closing=Decimal("105"),
    )

    summary = persist_approved_pdf(
        factory,
        CONTENT,
        "newest-first.pdf",
        mime_type="application/pdf",
        statement=statement,
    )

    with session_scope(factory) as session:
        running = tuple(
            session.scalars(
                select(BalanceSnapshotRecord)
                .where(
                    BalanceSnapshotRecord.import_batch_id == summary.import_batch_id,
                    BalanceSnapshotRecord.source == "running_balance",
                )
                .order_by(BalanceSnapshotRecord.as_of_date)
            )
        )
        assert [(item.as_of_date, item.balance) for item in running] == [
            (date(2026, 8, 5), Decimal("90")),
            (date(2026, 8, 7), Decimal("105")),
        ]


def test_same_day_ambiguous_order_avoids_ambiguous_running_snapshots(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    rows = (
        _approved_row(
            1,
            1,
            final=_draft(
                description="FICTIONAL CREDIT ONE",
                amount=Decimal("10"),
                balance=Decimal("110"),
            ),
        ),
        _approved_row(
            1,
            2,
            final=_draft(
                description="FICTIONAL DEBIT",
                amount=Decimal("-10"),
                balance=None,
            ),
        ),
        _approved_row(
            1,
            3,
            final=_draft(
                description="FICTIONAL CREDIT TWO",
                amount=Decimal("10"),
                balance=Decimal("110"),
            ),
        ),
    )
    statement = _statement(rows, opening=Decimal("100"), closing=Decimal("110"))

    summary = persist_approved_pdf(
        factory,
        CONTENT,
        "same-day.pdf",
        mime_type="application/pdf",
        statement=statement,
    )

    with session_scope(factory) as session:
        running_count = session.scalar(
            select(func.count())
            .select_from(BalanceSnapshotRecord)
            .where(
                BalanceSnapshotRecord.import_batch_id == summary.import_batch_id,
                BalanceSnapshotRecord.source == "running_balance",
            )
        )
        assert running_count == 0


def test_non_chronological_and_broken_running_balances_fail_closed(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    mixed_dates = (
        _approved_row(
            1,
            1,
            final=_draft(
                description="FICTIONAL ONE",
                amount=Decimal("-10"),
                balance=Decimal("90"),
                transaction_date=date(2026, 8, 5),
            ),
        ),
        _approved_row(
            1,
            2,
            final=_draft(
                description="FICTIONAL TWO",
                amount=Decimal("20"),
                balance=Decimal("110"),
                transaction_date=date(2026, 8, 7),
            ),
        ),
        _approved_row(
            1,
            3,
            final=_draft(
                description="FICTIONAL THREE",
                amount=Decimal("-5"),
                balance=Decimal("105"),
                transaction_date=date(2026, 8, 6),
            ),
        ),
    )
    error = _expect_persistence_error(
        factory,
        _statement(mixed_dates, opening=Decimal("100"), closing=Decimal("105")),
        PdfPersistenceErrorCode.NON_CHRONOLOGICAL_ROWS,
    )
    assert (error.page_number, error.page_record_number) == (1, 1)

    broken = _approved_row(
        1,
        1,
        final=_draft(
            description="PRIVATE VALUE MUST NOT APPEAR",
            amount=Decimal("-10"),
            balance=Decimal("89"),
        ),
    )
    error = _expect_persistence_error(
        factory,
        _statement((broken,), opening=Decimal("100"), closing=Decimal("90")),
        PdfPersistenceErrorCode.RUNNING_BALANCE_MISMATCH,
    )
    assert "PRIVATE VALUE" not in str(error)
    assert (error.page_number, error.page_record_number) == (1, 1)


def test_missing_periods_are_unknown_not_eligible_transaction_dates(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    statement = _one_row_statement().model_copy(
        update={
            "statement_coverage": COVERAGE.model_copy(
                update={
                    "status": CoverageStatus.GAPPED,
                    "missing_periods": (
                        DateRange(
                            start_date=date(2026, 8, 5),
                            end_date=date(2026, 8, 5),
                        ),
                    ),
                }
            )
        }
    )
    _expect_persistence_error(
        factory,
        statement,
        PdfPersistenceErrorCode.INVALID_TRANSACTION_SCOPE,
    )


def test_implicit_row_confirmation_debit_source_and_empty_optional_fields_persist(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    statement = _one_row_statement()
    base = statement.rows[0]
    original = OriginalTransactionValues(
        transaction_date_text="2026-08-05",
        description_text="FICTIONAL DEBIT",
        debit_amount_text="10.00",
        credit_amount_text="",
        raw_fields=(
            SourceFieldValue(column="Date", value="2026-08-05"),
            SourceFieldValue(column="Description", value="FICTIONAL DEBIT"),
            SourceFieldValue(column="Debit", value="10.00"),
            SourceFieldValue(column="Credit", value=""),
        ),
    )
    source_fingerprint = calculate_source_fingerprint(base.source_identity, original)
    transaction = base.transaction.model_copy(update={"balance_after": None})
    extracted = base.extracted_draft.model_copy(update={"balance_after": None})
    row = base.model_copy(
        update={
            "original": original,
            "source_fingerprint": source_fingerprint,
            "extracted_draft": extracted,
            "transaction": transaction,
            "row_decision": None,
        }
    )
    rejected = _rejected_row(2, 1)
    empty_draft = rejected.extracted_draft.model_copy(
        update={"account_id": None, "currency": None}
    )
    rejected = rejected.model_copy(
        update={"extracted_draft": empty_draft, "working_draft": empty_draft}
    )
    approved = _statement(
        (row,),
        rejected=(rejected,),
        opening=Decimal("100"),
        closing=Decimal("90"),
    )

    summary = persist_approved_pdf(
        factory,
        CONTENT,
        "debit-source.pdf",
        mime_type="application/pdf",
        statement=approved,
    )

    with session_scope(factory) as session:
        raw = session.scalar(
            select(RawTransactionRecord).where(
                RawTransactionRecord.import_batch_id == summary.import_batch_id,
                RawTransactionRecord.page_number == 1,
            )
        )
        assert raw is not None
        assert raw.original_amount_text == "10.00"
        assert raw.raw_payload["row_decision"] == "statement_confirmed"


def test_repeated_legacy_batch_without_coverage_is_safe_and_non_mutating(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    with session_scope(factory) as session:
        session.add(
            ImportBatchRecord(
                id="legacy-pdf-batch",
                account_id="account-1",
                source_type="digital_pdf",
                source_filename="legacy.pdf",
                file_hash=FILE_HASH,
                mime_type="application/pdf",
                byte_size=len(CONTENT),
                verification_status="verified",
                imported_at=RECEIVED_AT - timedelta(days=1),
            )
        )

    result = persist_approved_pdf(
        factory,
        CONTENT,
        "same-bytes.pdf",
        mime_type="application/pdf",
        statement=_one_row_statement(),
    )

    assert result.repeated_file is True
    assert result.import_batch_id == "legacy-pdf-batch"
    assert result.coverage.previous_statement_count == 0
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchRecord)) == 1


def test_existing_source_identity_and_corrupt_duplicate_evidence_fail_atomically(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )
    statement = _one_row_statement()
    source_fingerprint = statement.rows[0].source_fingerprint
    with session_scope(factory) as session:
        session.add(
            ImportBatchRecord(
                id="prior-source-batch",
                account_id="account-1",
                source_type="csv",
                source_filename="fictional.csv",
                file_hash="a" * 64,
                mime_type="text/csv",
                byte_size=10,
                verification_status="verified",
                imported_at=RECEIVED_AT - timedelta(days=1),
            )
        )
        session.add(
            RawTransactionRecord(
                id="prior-source-row",
                import_batch_id="prior-source-batch",
                source_type="csv",
                source_row_number=2,
                page_number=None,
                page_record_number=None,
                raw_payload={"fictional": True},
                original_date_text="2026-08-05",
                original_description="FICTIONAL",
                original_amount_text="-10",
                parser_name="fictional",
                parser_version="1",
                source_fingerprint=source_fingerprint,
                canonical_fingerprint=None,
                candidate_json=None,
                issues_json=[],
                review_status="rejected",
                created_at=RECEIVED_AT - timedelta(days=1),
            )
        )

    _expect_persistence_error(
        factory,
        statement,
        PdfPersistenceErrorCode.DUPLICATE_SOURCE_ROW,
    )
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchRecord)) == 1

    _seed_duplicate_evidence(factory)
    with session_scope(factory) as session:
        corrupt = session.get(RawTransactionRecord, "existing-raw-1")
        assert corrupt is not None
        corrupt.canonical_fingerprint = None

    duplicate_row = _approved_row(
        1,
        1,
        final=_draft(
            description="FICTIONAL CAFE",
            amount=Decimal("-4.50"),
            balance=Decimal("95.50"),
            external_id="existing-exact",
        ),
    )
    _expect_persistence_error(
        factory,
        _statement(
            (duplicate_row,),
            opening=Decimal("100"),
            closing=Decimal("95.50"),
        ),
        PdfPersistenceErrorCode.INVALID_STORED_EVIDENCE,
    )
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchRecord)) == 3


def test_failure_after_all_pdf_writes_rolls_back_everything(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_account(factory)
    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.utc_now", lambda: RECEIVED_AT
    )

    def fail_invalidation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("synthetic invalidation failure")

    monkeypatch.setattr(
        "cashflow_ai.imports.pdf_persistence.invalidate_derived_results_in_session",
        fail_invalidation,
    )
    with pytest.raises(RuntimeError, match="synthetic invalidation failure"):
        persist_approved_pdf(
            factory,
            CONTENT,
            "rollback-after-writes.pdf",
            mime_type="application/pdf",
            statement=_one_row_statement(),
        )

    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchRecord)) == 0
        assert (
            session.scalar(select(func.count()).select_from(RawTransactionRecord)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(BalanceSnapshotRecord)) == 0
        )


def test_pdf_import_summary_rejects_unaccounted_or_misaligned_locations() -> None:
    coverage = CsvCoverageAnalysis(previous_statement_count=0)
    base = {
        "import_batch_id": "batch-1",
        "file_hash": "a" * 64,
        "rows_read": 1,
        "imported_transactions": 1,
        "exact_duplicates_skipped": 0,
        "probable_duplicates": 0,
        "rejected_rows": 0,
        "coverage": coverage,
    }
    with pytest.raises(ValueError, match="account for every row"):
        PdfImportSummary.model_validate({**base, "imported_transactions": 0})
    with pytest.raises(ValueError, match="match their record locations"):
        PdfImportSummary.model_validate(
            {
                **base,
                "imported_transactions": 0,
                "exact_duplicates_skipped": 1,
            }
        )
