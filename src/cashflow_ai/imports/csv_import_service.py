"""Atomic persistence of explicitly confirmed CSV statement imports."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, cast

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.imports.coverage import analyse_statement_coverage
from cashflow_ai.imports.csv_preview import (
    DEFAULT_MAX_CSV_BYTES,
    CsvImportError,
    CsvImportErrorCode,
    parse_csv_document,
    validate_csv_import_plan,
)
from cashflow_ai.imports.duplicates import (
    assess_duplicate_facts,
    duplicate_facts_from_normalised,
)
from cashflow_ai.imports.normalisation import (
    NORMALISER_IDENTITY,
    TransactionNormalisationError,
    calculate_source_fingerprint,
    map_csv_row,
    normalise_transaction,
)
from cashflow_ai.persistence.base import utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    BalanceSnapshotRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    StatementCoverageRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.persistence.repositories import (
    AccountRepository,
    BalanceSnapshotRepository,
    ImportBatchRepository,
    StatementRepository,
    TransactionRepository,
)
from cashflow_ai.schemas.csv_imports import (
    CsvCoverageAnalysis,
    CsvDocument,
    CsvImportConfirmation,
    CsvImportPlan,
    CsvImportSummary,
)
from cashflow_ai.schemas.duplicates import (
    DuplicateAssessment,
    DuplicateFacts,
    DuplicateStatus,
)
from cashflow_ai.schemas.imports import IssueSeverity, ReviewStatus, VerificationStatus
from cashflow_ai.schemas.normalisation import (
    NormalisedTransaction,
    OriginalTransactionValues,
    SourceRecordIdentity,
)
from cashflow_ai.schemas.statements import (
    BalanceSnapshotSource,
    CoverageStatus,
    DateRange,
    StatementCoverage,
)
from cashflow_ai.schemas.transactions import Currency, Direction, FinancialRole

ALLOWED_CSV_MIME_TYPES: Final = frozenset({"text/csv", "application/csv", "text/plain"})


def _coverage_from_record(record: StatementCoverageRecord) -> StatementCoverage:
    return StatementCoverage(
        statement_start_date=record.statement_start_date,
        statement_end_date=record.statement_end_date,
        status=CoverageStatus(record.coverage_status),
        missing_periods=tuple(
            DateRange.model_validate(item) for item in record.missing_periods_json
        ),
    )


def _raw_payload(original: OriginalTransactionValues) -> dict[str, str]:
    return {field.column: field.value for field in original.raw_fields}


def _original_amount_text(original: OriginalTransactionValues) -> str | None:
    if original.signed_amount_text is not None:
        return original.signed_amount_text
    return original.debit_amount_text or original.credit_amount_text


def _issue(
    code: str,
    message: str,
    severity: IssueSeverity,
    **details: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "severity": severity.value,
        **details,
    }


def _raw_record(
    *,
    import_batch_id: str,
    original: OriginalTransactionValues,
    identity: SourceRecordIdentity,
    source_fingerprint: str,
    canonical_fingerprint: str | None,
    review_status: ReviewStatus,
    issues: list[dict[str, Any]],
    received_at: datetime,
) -> RawTransactionRecord:
    return RawTransactionRecord(
        import_batch_id=import_batch_id,
        source_type=identity.source_type.value,
        source_row_number=identity.source_row_number,
        page_number=identity.page_number,
        page_record_number=identity.page_record_number,
        raw_payload=_raw_payload(original),
        original_date_text=original.transaction_date_text,
        original_description=original.description_text,
        original_amount_text=_original_amount_text(original),
        parser_name=NORMALISER_IDENTITY.name,
        parser_version=NORMALISER_IDENTITY.version,
        source_fingerprint=source_fingerprint,
        canonical_fingerprint=canonical_fingerprint,
        issues_json=issues,
        review_status=review_status.value,
        created_at=received_at,
    )


def _duplicate_facts_from_records(
    verified: VerifiedTransactionRecord,
    raw: RawTransactionRecord,
) -> DuplicateFacts:
    canonical_fingerprint = raw.canonical_fingerprint
    if canonical_fingerprint is None:
        msg = "verified transactions require a canonical fingerprint"
        raise RuntimeError(msg)
    return DuplicateFacts(
        source_fingerprint=raw.source_fingerprint,
        canonical_fingerprint=canonical_fingerprint,
        account_id=verified.account_id,
        transaction_date=verified.transaction_date,
        amount=verified.amount,
        description=verified.description,
        merchant=verified.merchant,
        external_id=verified.external_id,
    )


def _best_duplicate(
    transaction: NormalisedTransaction,
    repository: TransactionRepository,
) -> DuplicateAssessment | None:
    incoming = duplicate_facts_from_normalised(transaction)
    candidates = repository.list_duplicate_candidates(
        account_id=incoming.account_id,
        transaction_date=incoming.transaction_date,
        external_id=incoming.external_id,
    )
    probable: list[DuplicateAssessment] = []
    for verified, raw in candidates:
        assessment = assess_duplicate_facts(
            incoming,
            _duplicate_facts_from_records(verified, raw),
        )
        if assessment.status is DuplicateStatus.EXACT:
            return assessment
        if assessment.status is DuplicateStatus.PROBABLE:
            probable.append(assessment)
    return max(probable, key=lambda item: item.score, default=None)


def _verified_record(
    transaction: NormalisedTransaction,
    raw_transaction_id: str,
    *,
    received_at: datetime,
) -> VerifiedTransactionRecord:
    draft = transaction.draft
    return VerifiedTransactionRecord(
        raw_transaction_id=raw_transaction_id,
        account_id=cast(str, draft.account_id),
        transaction_date=cast(date, draft.transaction_date),
        posting_date=draft.posting_date,
        description=cast(str, draft.description),
        merchant=draft.merchant,
        amount=cast(Decimal, draft.amount),
        balance_after=draft.balance_after,
        currency=cast(Currency, draft.currency).value,
        external_id=draft.external_id,
        transaction_type=draft.transaction_type,
        direction=cast(Direction, draft.direction).value,
        category_id=None,
        financial_role_id=FinancialRole.UNKNOWN.value,
        verified_at=received_at,
    )


def _persist_statement_metadata(
    repository: StatementRepository,
    *,
    import_batch_id: str,
    plan: CsvImportPlan,
    received_at: datetime,
) -> None:
    statement_context = plan.statement_context
    coverage = statement_context.coverage
    context_record = repository.add_context(
        ImportContextRecord(
            import_batch_id=import_batch_id,
            flags_json=sorted(flag.value for flag in statement_context.flags),
            note=statement_context.note,
            created_at=received_at,
        )
    )
    repository.add_coverage(
        StatementCoverageRecord(
            import_context_id=context_record.id,
            statement_start_date=coverage.statement_start_date,
            statement_end_date=coverage.statement_end_date,
            coverage_status=coverage.status.value,
            missing_periods_json=[
                missing.model_dump(mode="json") for missing in coverage.missing_periods
            ],
        )
    )
    balances = statement_context.balances
    if balances is None:
        return
    for balance, as_of_date, source in (
        (
            balances.opening_balance,
            coverage.statement_start_date,
            BalanceSnapshotSource.STATEMENT_OPENING,
        ),
        (
            balances.closing_balance,
            coverage.statement_end_date,
            BalanceSnapshotSource.STATEMENT_CLOSING,
        ),
    ):
        if balance is None:
            continue
        repository.add_balance(
            BalanceSnapshotRecord(
                account_id=plan.account_id,
                import_batch_id=import_batch_id,
                balance=balance,
                currency=balances.currency.value,
                as_of_date=as_of_date,
                recorded_at=received_at,
                source=source.value,
                verification_status=VerificationStatus.VERIFIED.value,
            )
        )


def _coverage_analysis(
    repository: StatementRepository,
    plan: CsvImportPlan,
    *,
    exclude_batch_id: str | None = None,
    incoming: StatementCoverage | None = None,
) -> CsvCoverageAnalysis:
    records = repository.list_coverages_for_account(
        plan.account_id,
        exclude_batch_id=exclude_batch_id,
    )
    return analyse_statement_coverage(
        incoming or plan.statement_context.coverage,
        (_coverage_from_record(record) for record in records),
    )


def _repeated_summary(
    document: CsvDocument,
    plan: CsvImportPlan,
    batch: ImportBatchRecord,
    statement_repository: StatementRepository,
) -> CsvImportSummary:
    stored_record = statement_repository.get_coverage_for_batch(batch.id)
    stored_coverage = (
        _coverage_from_record(stored_record)
        if stored_record is not None
        else plan.statement_context.coverage
    )
    row_numbers = tuple(row.source_row_number for row in document.rows)
    return CsvImportSummary(
        import_batch_id=batch.id,
        file_hash=document.file_hash,
        rows_read=len(document.rows),
        new_transactions=0,
        exact_duplicates_skipped=len(document.rows),
        probable_duplicates=0,
        rejected_rows=0,
        repeated_file=True,
        exact_duplicate_rows=row_numbers,
        coverage=_coverage_analysis(
            statement_repository,
            plan,
            exclude_batch_id=batch.id,
            incoming=stored_coverage,
        ),
    )


def _persist_document(
    session: Session,
    document: CsvDocument,
    *,
    mime_type: str,
    plan: CsvImportPlan,
    received_at: datetime,
) -> CsvImportSummary:
    account = AccountRepository(session).get(plan.account_id)
    if account is None:
        raise CsvImportError(
            CsvImportErrorCode.ACCOUNT_NOT_FOUND,
            "selected import account does not exist",
        )
    if account.currency != plan.account_currency.value:
        raise CsvImportError(
            CsvImportErrorCode.ACCOUNT_CURRENCY_MISMATCH,
            "statement currency does not match the selected account",
        )

    batch_repository = ImportBatchRepository(session)
    statement_repository = StatementRepository(session)
    prior_batch = batch_repository.get_by_file_hash(plan.account_id, document.file_hash)
    if prior_batch is not None:
        return _repeated_summary(document, plan, prior_batch, statement_repository)

    coverage = _coverage_analysis(statement_repository, plan)
    batch = batch_repository.add(
        ImportBatchRecord(
            account_id=plan.account_id,
            source_type="csv",
            source_filename=document.source_filename,
            file_hash=document.file_hash,
            mime_type=mime_type,
            byte_size=document.byte_size,
            verification_status=VerificationStatus.UNVERIFIED.value,
            imported_at=received_at,
        )
    )
    _persist_statement_metadata(
        statement_repository,
        import_batch_id=batch.id,
        plan=plan,
        received_at=received_at,
    )

    transaction_repository = TransactionRepository(session)
    balance_repository = BalanceSnapshotRepository(session)
    exact_rows: list[int] = []
    probable_rows: list[int] = []
    rejected_rows: list[int] = []
    accepted_count = 0
    for row in document.rows:
        original, identity = map_csv_row(
            document.columns,
            row,
            plan,
            source_document_hash=document.file_hash,
        )
        source_fingerprint = calculate_source_fingerprint(identity, original)
        try:
            transaction = normalise_transaction(
                original,
                account_id=plan.account_id,
                account_currency=plan.account_currency,
                source_identity=identity,
            )
        except TransactionNormalisationError as error:
            rejected_rows.append(row.source_row_number)
            transaction_repository.add_raw(
                _raw_record(
                    import_batch_id=batch.id,
                    original=original,
                    identity=identity,
                    source_fingerprint=source_fingerprint,
                    canonical_fingerprint=None,
                    review_status=ReviewStatus.REJECTED,
                    issues=[
                        _issue(
                            error.code.value,
                            str(error),
                            IssueSeverity.ERROR,
                        )
                    ],
                    received_at=received_at,
                )
            )
            continue

        duplicate = _best_duplicate(transaction, transaction_repository)
        if duplicate is not None:
            is_exact = duplicate.status is DuplicateStatus.EXACT
            target_rows = exact_rows if is_exact else probable_rows
            target_rows.append(row.source_row_number)
            transaction_repository.add_raw(
                _raw_record(
                    import_batch_id=batch.id,
                    original=original,
                    identity=identity,
                    source_fingerprint=transaction.source_fingerprint,
                    canonical_fingerprint=transaction.canonical_fingerprint,
                    review_status=(
                        ReviewStatus.REJECTED if is_exact else ReviewStatus.NEEDS_REVIEW
                    ),
                    issues=[
                        _issue(
                            "exact_duplicate" if is_exact else "probable_duplicate",
                            (
                                "row matches an existing transaction and was skipped"
                                if is_exact
                                else "row may duplicate an existing transaction"
                            ),
                            IssueSeverity.WARNING,
                            score=duplicate.score,
                            reasons=[reason.value for reason in duplicate.reasons],
                            existing_source_fingerprint=(
                                duplicate.existing_source_fingerprint
                            ),
                        )
                    ],
                    received_at=received_at,
                )
            )
            continue

        raw = transaction_repository.add_raw(
            _raw_record(
                import_batch_id=batch.id,
                original=original,
                identity=identity,
                source_fingerprint=transaction.source_fingerprint,
                canonical_fingerprint=transaction.canonical_fingerprint,
                review_status=ReviewStatus.CONFIRMED,
                issues=[],
                received_at=received_at,
            )
        )
        verified = transaction_repository.add_verified(
            _verified_record(transaction, raw.id, received_at=received_at)
        )
        if verified.balance_after is not None:
            balance_repository.add(
                BalanceSnapshotRecord(
                    account_id=verified.account_id,
                    import_batch_id=batch.id,
                    balance=verified.balance_after,
                    currency=verified.currency,
                    as_of_date=verified.posting_date or verified.transaction_date,
                    recorded_at=received_at,
                    source=BalanceSnapshotSource.RUNNING_BALANCE.value,
                    verification_status=VerificationStatus.VERIFIED.value,
                )
            )
        accepted_count += 1

    batch.verification_status = (
        VerificationStatus.NEEDS_REVIEW.value
        if probable_rows or rejected_rows
        else VerificationStatus.VERIFIED.value
    )
    return CsvImportSummary(
        import_batch_id=batch.id,
        file_hash=document.file_hash,
        rows_read=len(document.rows),
        new_transactions=accepted_count,
        exact_duplicates_skipped=len(exact_rows),
        probable_duplicates=len(probable_rows),
        rejected_rows=len(rejected_rows),
        exact_duplicate_rows=tuple(exact_rows),
        probable_duplicate_rows=tuple(probable_rows),
        rejected_row_numbers=tuple(rejected_rows),
        coverage=coverage,
    )


def persist_confirmed_csv(
    factory: sessionmaker[Session],
    content: bytes,
    filename: str,
    *,
    mime_type: str,
    plan: CsvImportPlan,
    confirmation: CsvImportConfirmation | None,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
) -> CsvImportSummary:
    """Validate and atomically store one explicitly confirmed CSV document."""
    if confirmation is None:
        raise CsvImportError(
            CsvImportErrorCode.CONFIRMATION_REQUIRED,
            "confirm the CSV preview before importing transactions",
        )
    if mime_type not in ALLOWED_CSV_MIME_TYPES:
        raise CsvImportError(
            CsvImportErrorCode.UNSUPPORTED_MIME_TYPE,
            "CSV source has an unsupported MIME type",
        )
    document = parse_csv_document(content, filename, max_bytes=max_bytes)
    validate_csv_import_plan(document, plan)
    if confirmation.preview_file_hash != document.file_hash:
        raise CsvImportError(
            CsvImportErrorCode.PREVIEW_CHANGED,
            "uploaded CSV bytes changed after the confirmed preview",
        )
    received_at = utc_now()
    if confirmation.confirmed_at > received_at:
        raise CsvImportError(
            CsvImportErrorCode.INVALID_CONFIRMATION_TIME,
            "CSV confirmation time cannot be in the future",
        )
    with session_scope(factory) as session:
        return _persist_document(
            session,
            document,
            mime_type=mime_type,
            plan=plan,
            received_at=received_at,
        )
