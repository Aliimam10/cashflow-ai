"""Atomic persistence for explicitly approved digital-PDF statements."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any, Final, cast

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.imports.coverage import analyse_statement_coverage
from cashflow_ai.imports.duplicates import assess_duplicate_facts
from cashflow_ai.imports.normalisation import (
    calculate_canonical_fingerprint,
    calculate_file_hash,
    calculate_source_fingerprint,
)
from cashflow_ai.imports.reconciliation import (
    StatementReviewError,
    approve_statement_review,
    prepare_statement_review,
    reconcile_statement,
)
from cashflow_ai.imports.spatial_pdf import SpatialColumnMapping
from cashflow_ai.imports.text_pdf import (
    DEFAULT_MAX_PDF_BYTES,
    PdfImportError,
    extract_text_pdf,
)
from cashflow_ai.invalidation import invalidate_derived_results_in_session
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
from cashflow_ai.schemas.csv_imports import CsvCoverageAnalysis
from cashflow_ai.schemas.duplicates import (
    DuplicateAssessment,
    DuplicateCandidateSnapshot,
    DuplicateFacts,
    DuplicateStatus,
)
from cashflow_ai.schemas.imports import (
    ImportIssue,
    IssueSeverity,
    ReviewStatus,
    SourceType,
    VerificationStatus,
)
from cashflow_ai.schemas.invalidation import SourceDataChangeType
from cashflow_ai.schemas.pdf_persistence import PdfImportSummary, PdfRecordLocation
from cashflow_ai.schemas.reconciliation import (
    ApprovedReviewRow,
    ApprovedStatement,
    ReconciliationStatus,
    RowDecision,
    RowReview,
    StatementApproval,
    StatementBalanceField,
    StatementReviewRow,
)
from cashflow_ai.schemas.statements import (
    BalanceSnapshotSource,
    CoverageStatus,
    DateRange,
    StatementBalances,
    StatementCoverage,
)
from cashflow_ai.schemas.transactions import (
    CanonicalTransaction,
    Currency,
    FinancialRole,
    TransactionDraft,
)

ALLOWED_PDF_MIME_TYPES: Final = frozenset({"application/pdf"})

type PersistablePdfRow = ApprovedReviewRow | StatementReviewRow


class PdfPersistenceErrorCode(StrEnum):
    """Stable, privacy-safe failures from the PDF persistence boundary."""

    INVALID_LIMIT = "invalid_limit"
    INVALID_FILENAME = "invalid_filename"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    UNSUPPORTED_MIME_TYPE = "unsupported_mime_type"
    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    INVALID_SIGNATURE = "invalid_signature"
    FILE_CHANGED = "file_changed"
    UNSUPPORTED_SOURCE_TYPE = "unsupported_source_type"
    INVALID_APPROVAL_TIME = "invalid_approval_time"
    EMPTY_STATEMENT = "empty_statement"
    COVERAGE_REQUIRED = "coverage_required"
    INVALID_LINEAGE = "invalid_lineage"
    DUPLICATE_SOURCE_ROW = "duplicate_source_row"
    ACCOUNT_NOT_FOUND = "account_not_found"
    ACCOUNT_INACTIVE = "account_inactive"
    ACCOUNT_CURRENCY_MISMATCH = "account_currency_mismatch"
    INVALID_TRANSACTION_SCOPE = "invalid_transaction_scope"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    RECONCILIATION_FAILED = "reconciliation_failed"
    NON_CHRONOLOGICAL_ROWS = "non_chronological_rows"
    RUNNING_BALANCE_MISMATCH = "running_balance_mismatch"
    INVALID_STORED_EVIDENCE = "invalid_stored_evidence"


class _SourceOrder(StrEnum):
    OLDEST_FIRST = "oldest_first"
    NEWEST_FIRST = "newest_first"
    UNDETERMINED = "undetermined"


class PdfPersistenceError(ValueError):
    """Controlled persistence failure that never includes source values."""

    def __init__(
        self,
        code: PdfPersistenceErrorCode,
        message: str,
        *,
        page_number: int | None = None,
        page_record_number: int | None = None,
    ) -> None:
        """Retain a machine-readable code and data-minimised explanation."""
        super().__init__(message)
        self.code = code
        self.page_number = page_number
        self.page_record_number = page_record_number


def _safe_pdf_filename(filename: str) -> str:
    cleaned = filename.strip().replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    cleaned = "".join(character for character in cleaned if character.isprintable())
    if not cleaned or len(cleaned) > 255 or cleaned in {".", ".."}:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_FILENAME,
            "provide a non-empty PDF filename of at most 255 characters",
        )
    if not cleaned.casefold().endswith(".pdf"):
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.UNSUPPORTED_FILE_TYPE,
            "digital-PDF persistence requires a .pdf filename",
        )
    return cleaned


def _location(row: PersistablePdfRow) -> PdfRecordLocation:
    identity = row.source_identity
    if identity.page_number is None or identity.page_record_number is None:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_LINEAGE,
            "every PDF row requires a page and record location",
        )
    return PdfRecordLocation(
        page_number=identity.page_number,
        page_record_number=identity.page_record_number,
    )


def _ordered_rows(
    statement: ApprovedStatement,
) -> tuple[tuple[PersistablePdfRow, bool], ...]:
    items: tuple[tuple[PersistablePdfRow, bool], ...] = tuple(
        (row, False) for row in statement.rows
    ) + tuple((row, True) for row in statement.rejected_rows)
    return tuple(
        sorted(
            items,
            key=lambda item: (
                _location(item[0]).page_number,
                _location(item[0]).page_record_number,
            ),
        )
    )


def _statement_account_and_currency(
    rows: tuple[tuple[PersistablePdfRow, bool], ...],
) -> tuple[str, Currency]:
    account_ids: set[str] = set()
    currencies: set[Currency] = set()
    for row, rejected in rows:
        draft = (
            row.extracted_draft
            if rejected
            else cast(ApprovedReviewRow, row).transaction
        )
        if draft.account_id is not None:
            account_ids.add(draft.account_id)
        if draft.currency is not None:
            currencies.add(draft.currency)
    if len(account_ids) != 1 or len(currencies) != 1:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_TRANSACTION_SCOPE,
            "one approved PDF must target exactly one account and currency",
        )
    return next(iter(account_ids)), next(iter(currencies))


def _within_coverage(transaction_date: date, coverage: StatementCoverage) -> bool:
    return (
        coverage.statement_start_date <= transaction_date <= coverage.statement_end_date
        and not any(
            gap.start_date <= transaction_date <= gap.end_date
            for gap in coverage.missing_periods
        )
    )


def _validate_reconciliation(
    statement: ApprovedStatement,
    rows: tuple[tuple[PersistablePdfRow, bool], ...],
) -> None:
    balances = statement.balances
    if (
        balances is None
        or balances.opening_balance is None
        or balances.closing_balance is None
    ):
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.RECONCILIATION_REQUIRED,
            "Version 1 requires confirmed opening and closing balances before "
            "PDF persistence",
        )
    evidence_fields = tuple(evidence.field for evidence in statement.balance_evidence)
    if len(evidence_fields) != 2 or set(evidence_fields) != {
        StatementBalanceField.OPENING,
        StatementBalanceField.CLOSING,
    }:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.RECONCILIATION_REQUIRED,
            "Version 1 requires source evidence for both statement balances",
        )
    evidence_by_field = {
        evidence.field: evidence.amount for evidence in statement.balance_evidence
    }
    extracted_balances = StatementBalances(
        opening_balance=evidence_by_field[StatementBalanceField.OPENING],
        closing_balance=evidence_by_field[StatementBalanceField.CLOSING],
        currency=balances.currency,
    )
    if statement.balance_was_edited != (balances != extracted_balances):
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.RECONCILIATION_REQUIRED,
            "confirmed balances are not bound to their extracted PDF evidence",
        )
    amounts = tuple(
        cast(ApprovedReviewRow, row).transaction.amount
        for row, rejected in rows
        if not rejected
    )
    reconciliation = reconcile_statement(balances, amounts)
    if (
        reconciliation.status is not ReconciliationStatus.RECONCILED
        or statement.reconciliation != reconciliation
    ):
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.RECONCILIATION_FAILED,
            "the approved PDF does not reconcile to its confirmed balances",
        )


def _running_balance_mismatch(
    rows: tuple[ApprovedReviewRow, ...],
    *,
    opening_balance: Decimal,
    tolerance: Decimal,
    order: _SourceOrder,
) -> PdfRecordLocation | None:
    chronological_rows = rows if order is _SourceOrder.OLDEST_FIRST else rows[::-1]
    running_balance = opening_balance
    for row in chronological_rows:
        running_balance += row.transaction.amount
        current_balance = row.transaction.balance_after
        if (
            current_balance is not None
            and abs(current_balance - running_balance) > tolerance
        ):
            return _location(row)
    return None


def _validate_running_balances(
    statement: ApprovedStatement,
    rows: tuple[tuple[PersistablePdfRow, bool], ...],
) -> _SourceOrder:
    assert statement.balances is not None
    assert statement.balances.opening_balance is not None
    assert statement.balances.closing_balance is not None
    approved_rows = tuple(
        cast(ApprovedReviewRow, row) for row, rejected in rows if not rejected
    )
    balance_rows = tuple(
        row for row in approved_rows if row.transaction.balance_after is not None
    )
    if not balance_rows:
        return _SourceOrder.UNDETERMINED

    dates = tuple(row.transaction.transaction_date for row in approved_rows)
    has_ascending_step = any(left < right for left, right in pairwise(dates))
    has_descending_step = any(left > right for left, right in pairwise(dates))
    if has_ascending_step and has_descending_step:
        location = _location(balance_rows[0])
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.NON_CHRONOLOGICAL_ROWS,
            "PDF transaction rows are not in one chronological source order",
            page_number=location.page_number,
            page_record_number=location.page_record_number,
        )

    candidate_orders = (
        (_SourceOrder.OLDEST_FIRST,)
        if has_ascending_step
        else (_SourceOrder.NEWEST_FIRST,)
        if has_descending_step
        else (_SourceOrder.OLDEST_FIRST, _SourceOrder.NEWEST_FIRST)
    )
    mismatches = {
        order: _running_balance_mismatch(
            approved_rows,
            opening_balance=statement.balances.opening_balance,
            tolerance=statement.reconciliation.tolerance,
            order=order,
        )
        for order in candidate_orders
    }
    matching = tuple(
        order for order, mismatch in mismatches.items() if mismatch is None
    )
    if len(matching) == 1:
        return matching[0]
    if len(matching) == 2:
        return _SourceOrder.UNDETERMINED

    location = next(
        mismatch for mismatch in mismatches.values() if mismatch is not None
    )
    assert location is not None
    raise PdfPersistenceError(
        PdfPersistenceErrorCode.RUNNING_BALANCE_MISMATCH,
        "running-balance continuity failed at the retained PDF row",
        page_number=location.page_number,
        page_record_number=location.page_record_number,
    )


def _validate_row_lineage(
    row: PersistablePdfRow,
    *,
    file_hash: str,
) -> None:
    identity = row.source_identity
    provenance = row.provenance
    if (
        identity.source_type is not SourceType.DIGITAL_PDF
        or identity.source_document_hash != file_hash
        or provenance.source_type is not SourceType.DIGITAL_PDF
        or provenance.page_number != identity.page_number
        or provenance.parser is None
    ):
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_LINEAGE,
            "PDF row extraction lineage is incomplete or inconsistent",
        )
    _location(row)
    expected_fingerprint = calculate_source_fingerprint(identity, row.original)
    if row.source_fingerprint != expected_fingerprint:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_LINEAGE,
            "PDF row fingerprint does not match its preserved source evidence",
        )


def _validate_statement(
    statement: ApprovedStatement,
    *,
    file_hash: str,
    received_at: datetime,
) -> tuple[
    str,
    Currency,
    StatementCoverage,
    tuple[tuple[PersistablePdfRow, bool], ...],
    _SourceOrder,
]:
    if statement.source_type is not SourceType.DIGITAL_PDF:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.UNSUPPORTED_SOURCE_TYPE,
            "this persistence boundary accepts approved digital PDFs only",
        )
    if statement.file_hash != file_hash:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.FILE_CHANGED,
            "approved statement does not match the supplied PDF bytes",
        )
    approved_at = statement.approved_at
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_APPROVAL_TIME,
            "PDF approval time must be timezone-aware",
        )
    if approved_at.astimezone(UTC) > received_at:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_APPROVAL_TIME,
            "PDF approval time cannot be in the future",
        )
    coverage = statement.statement_coverage
    if coverage is None:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.COVERAGE_REQUIRED,
            "confirm the statement coverage before persistence",
        )
    rows = _ordered_rows(statement)
    if not rows:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.EMPTY_STATEMENT,
            "an approved PDF must retain at least one reviewed row",
        )

    expected_rejected = tuple(row.source_fingerprint for row in statement.rejected_rows)
    if statement.rejected_source_fingerprints != expected_rejected:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_LINEAGE,
            "rejected PDF row identities do not match their preserved evidence",
        )

    seen_fingerprints: set[str] = set()
    seen_locations: set[tuple[int, int]] = set()
    for row, rejected in rows:
        _validate_row_lineage(row, file_hash=file_hash)
        location = _location(row)
        location_key = (location.page_number, location.page_record_number)
        if (
            row.source_fingerprint in seen_fingerprints
            or location_key in seen_locations
        ):
            raise PdfPersistenceError(
                PdfPersistenceErrorCode.DUPLICATE_SOURCE_ROW,
                "an approved PDF cannot contain a source row more than once",
            )
        seen_fingerprints.add(row.source_fingerprint)
        seen_locations.add(location_key)
        if rejected:
            continue
        approved = cast(ApprovedReviewRow, row)
        transaction = approved.transaction
        if (
            approved.row_decision is RowDecision.REJECT
            or approved.was_edited
            != (
                approved.transaction.model_dump()
                != approved.extracted_draft.model_dump()
            )
            or transaction.category_id is not None
            or transaction.financial_role is not FinancialRole.UNKNOWN
            or not _within_coverage(transaction.transaction_date, coverage)
            or (
                transaction.posting_date is not None
                and not _within_coverage(transaction.posting_date, coverage)
            )
        ):
            raise PdfPersistenceError(
                PdfPersistenceErrorCode.INVALID_TRANSACTION_SCOPE,
                "approved PDF transaction is outside the ingestion trust boundary",
            )

    account_id, currency = _statement_account_and_currency(rows)
    for row, _rejected in rows:
        draft = row.extracted_draft
        if draft.account_id not in {None, account_id} or draft.currency not in {
            None,
            currency,
        }:
            raise PdfPersistenceError(
                PdfPersistenceErrorCode.INVALID_TRANSACTION_SCOPE,
                "PDF extraction evidence crosses account or currency boundaries",
            )
    for evidence in statement.balance_evidence:
        if (
            evidence.source_identity.source_type is not SourceType.DIGITAL_PDF
            or evidence.source_identity.source_document_hash != file_hash
            or evidence.provenance.source_type is not SourceType.DIGITAL_PDF
            or evidence.provenance.page_number != evidence.source_identity.page_number
            or evidence.provenance.parser is None
            or evidence.source_identity.page_record_number != evidence.line_number
        ):
            raise PdfPersistenceError(
                PdfPersistenceErrorCode.INVALID_LINEAGE,
                "PDF balance lineage is incomplete or inconsistent",
            )
    _validate_reconciliation(statement, rows)
    source_order = _validate_running_balances(statement, rows)
    return account_id, currency, coverage, rows, source_order


def _replay_row_reviews(statement: ApprovedStatement) -> tuple[RowReview, ...]:
    """Project only explicit decisions and edits onto a fresh trusted review."""
    decisions: list[RowReview] = []
    for row in statement.rows:
        if row.row_decision is None:
            continue
        decisions.append(
            RowReview(
                source_fingerprint=row.source_fingerprint,
                decision=RowDecision.CONFIRM,
                corrected_draft=(
                    TransactionDraft.model_validate(row.transaction.model_dump())
                    if row.was_edited
                    else None
                ),
            )
        )
    decisions.extend(
        RowReview(
            source_fingerprint=row.source_fingerprint,
            decision=RowDecision.REJECT,
        )
        for row in statement.rejected_rows
    )
    return tuple(decisions)


def _reextract_approved_statement(
    content: bytes,
    safe_filename: str,
    *,
    mime_type: str,
    statement: ApprovedStatement,
    account_id: str,
    currency: Currency,
    max_bytes: int,
    spatial_mapping: SpatialColumnMapping | None,
) -> ApprovedStatement:
    """Rebuild approval from exact bytes instead of trusting supplied evidence."""
    try:
        preview = extract_text_pdf(
            content,
            safe_filename,
            mime_type=mime_type,
            account_id=account_id,
            account_currency=currency,
            max_bytes=max_bytes,
            spatial_mapping=spatial_mapping,
        )
        trusted_review = prepare_statement_review(preview)
        trusted_statement = approve_statement_review(
            trusted_review,
            StatementApproval(
                file_hash=trusted_review.file_hash,
                approved_at=statement.approved_at,
                statement_approved=True,
                date_format=statement.date_format,
                sign_convention=statement.sign_convention,
                confirmed_statement_coverage=statement.statement_coverage,
                confirmed_balances=statement.balances,
                row_reviews=_replay_row_reviews(statement),
            ),
        )
    except (PdfImportError, StatementReviewError) as error:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_LINEAGE,
            "approved PDF decisions could not be replayed on the supplied bytes",
        ) from error

    if trusted_statement != statement:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_LINEAGE,
            "approved PDF evidence does not match a fresh extraction of the "
            "supplied bytes",
        )
    return trusted_statement


def _coverage_from_record(record: StatementCoverageRecord) -> StatementCoverage:
    return StatementCoverage(
        statement_start_date=record.statement_start_date,
        statement_end_date=record.statement_end_date,
        status=CoverageStatus(record.coverage_status),
        missing_periods=tuple(
            DateRange.model_validate(item) for item in record.missing_periods_json
        ),
    )


def _coverage_analysis(
    repository: StatementRepository,
    *,
    account_id: str,
    incoming: StatementCoverage,
    exclude_batch_id: str | None = None,
) -> CsvCoverageAnalysis:
    records = repository.list_coverages_for_account(
        account_id,
        exclude_batch_id=exclude_batch_id,
    )
    return analyse_statement_coverage(
        incoming,
        (_coverage_from_record(record) for record in records),
    )


def _issue_payload(issue: ImportIssue) -> dict[str, Any]:
    return issue.model_dump(mode="json", exclude_none=True)


def _synthetic_issue(
    code: str,
    message: str,
    *,
    severity: IssueSeverity = IssueSeverity.WARNING,
    **details: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "severity": severity.value,
        **details,
    }


def _statement_approval_payload(statement: ApprovedStatement) -> dict[str, Any]:
    assert statement.statement_coverage is not None
    assert statement.balances is not None
    return {
        "approved_at": statement.approved_at.isoformat(),
        "date_format": None
        if statement.date_format is None
        else statement.date_format.value,
        "sign_convention": (
            None
            if statement.sign_convention is None
            else statement.sign_convention.value
        ),
        "statement_coverage": statement.statement_coverage.model_dump(mode="json"),
        "coverage_was_edited": statement.coverage_was_edited,
        "balances": statement.balances.model_dump(mode="json"),
        "balance_evidence": [
            evidence.model_dump(mode="json", exclude_none=True)
            for evidence in statement.balance_evidence
        ],
        "balance_was_edited": statement.balance_was_edited,
        "reconciliation": statement.reconciliation.model_dump(
            mode="json", exclude_none=True
        ),
    }


def _raw_payload(
    row: PersistablePdfRow,
    *,
    statement: ApprovedStatement,
) -> dict[str, Any]:
    if isinstance(row, ApprovedReviewRow):
        reviewed_draft = row.transaction.model_dump(mode="json")
        decision = (
            "statement_confirmed"
            if row.row_decision is None
            else row.row_decision.value
        )
    else:
        reviewed_draft = row.working_draft.model_dump(mode="json")
        decision = RowDecision.REJECT.value
    return {
        "schema_version": "pdf-source-row-2.0",
        "original": row.original.model_dump(mode="json"),
        "raw_fields": [
            field.model_dump(mode="json") for field in row.original.raw_fields
        ],
        "extracted_draft": row.extracted_draft.model_dump(mode="json"),
        "reviewed_draft": reviewed_draft,
        "row_decision": decision,
        "was_edited": row.was_edited,
        "provenance": row.provenance.model_dump(mode="json", exclude_none=True),
        "source_line_numbers": list(row.source_line_numbers),
        "field_confidences": [
            confidence.model_dump(mode="json", exclude_none=True)
            for confidence in row.field_confidences
        ],
        "review_reasons": sorted(reason.value for reason in row.review_reasons),
        "document_issues": [
            _issue_payload(issue) for issue in statement.document_issues
        ],
        "statement_approval": _statement_approval_payload(statement),
    }


def _original_amount_text(row: PersistablePdfRow) -> str | None:
    original = row.original
    if original.signed_amount_text is not None:
        return original.signed_amount_text
    return original.debit_amount_text or original.credit_amount_text


def _raw_record(
    *,
    import_batch_id: str,
    row: PersistablePdfRow,
    canonical_fingerprint: str | None,
    review_status: ReviewStatus,
    issues: list[dict[str, Any]],
    candidate: CanonicalTransaction | None,
    statement: ApprovedStatement,
    received_at: datetime,
) -> RawTransactionRecord:
    parser = row.provenance.parser
    assert parser is not None
    identity = row.source_identity
    return RawTransactionRecord(
        import_batch_id=import_batch_id,
        source_type=SourceType.DIGITAL_PDF.value,
        source_row_number=None,
        page_number=identity.page_number,
        page_record_number=identity.page_record_number,
        raw_payload=_raw_payload(row, statement=statement),
        original_date_text=row.original.transaction_date_text,
        original_description=row.original.description_text,
        original_amount_text=_original_amount_text(row),
        parser_name=parser.name,
        parser_version=parser.version,
        source_fingerprint=row.source_fingerprint,
        canonical_fingerprint=canonical_fingerprint,
        candidate_json=(
            None
            if candidate is None
            else DuplicateCandidateSnapshot(
                draft=TransactionDraft.model_validate(candidate.model_dump())
            ).model_dump(mode="json")
        ),
        issues_json=issues,
        review_status=review_status.value,
        created_at=received_at,
    )


def _duplicate_facts_from_records(
    verified: VerifiedTransactionRecord,
    raw: RawTransactionRecord,
) -> DuplicateFacts:
    if raw.canonical_fingerprint is None:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_STORED_EVIDENCE,
            "stored verified transaction evidence is incomplete",
        )
    return DuplicateFacts(
        source_fingerprint=raw.source_fingerprint,
        canonical_fingerprint=raw.canonical_fingerprint,
        account_id=verified.account_id,
        transaction_date=verified.transaction_date,
        amount=verified.amount,
        description=verified.description,
        merchant=verified.merchant,
        external_id=verified.external_id,
    )


def _duplicate_facts(
    row: ApprovedReviewRow,
    canonical_fingerprint: str,
) -> DuplicateFacts:
    transaction = row.transaction
    return DuplicateFacts(
        source_fingerprint=row.source_fingerprint,
        canonical_fingerprint=canonical_fingerprint,
        account_id=transaction.account_id,
        transaction_date=transaction.transaction_date,
        amount=transaction.amount,
        description=transaction.description,
        merchant=transaction.merchant,
        external_id=transaction.external_id,
    )


def _best_duplicate(
    incoming: DuplicateFacts,
    repository: TransactionRepository,
) -> DuplicateAssessment | None:
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
    transaction: CanonicalTransaction,
    *,
    raw_transaction_id: str,
    received_at: datetime,
) -> VerifiedTransactionRecord:
    return VerifiedTransactionRecord(
        raw_transaction_id=raw_transaction_id,
        account_id=transaction.account_id,
        transaction_date=transaction.transaction_date,
        posting_date=transaction.posting_date,
        description=transaction.description,
        merchant=transaction.merchant,
        amount=transaction.amount,
        balance_after=transaction.balance_after,
        currency=transaction.currency.value,
        external_id=transaction.external_id,
        transaction_type=transaction.transaction_type,
        direction=transaction.direction.value,
        category_id=None,
        financial_role_id=FinancialRole.UNKNOWN.value,
        verified_at=received_at,
    )


def _daily_running_balance_fingerprints(
    rows: tuple[tuple[PersistablePdfRow, bool], ...],
    *,
    source_order: _SourceOrder,
) -> frozenset[str]:
    """Select at most one deterministic closing observation for each source day."""
    by_date: dict[date, list[ApprovedReviewRow]] = {}
    for source_row, rejected in rows:
        if rejected:
            continue
        row = cast(ApprovedReviewRow, source_row)
        transaction = row.transaction
        if transaction.balance_after is None:
            continue
        snapshot_date = transaction.posting_date or transaction.transaction_date
        by_date.setdefault(snapshot_date, []).append(row)

    selected: set[str] = set()
    for candidates in by_date.values():
        if source_order is _SourceOrder.OLDEST_FIRST:
            selected.add(candidates[-1].source_fingerprint)
        elif source_order is _SourceOrder.NEWEST_FIRST or len(candidates) == 1:
            selected.add(candidates[0].source_fingerprint)
    return frozenset(selected)


def _persist_statement_metadata(
    repository: StatementRepository,
    *,
    account_id: str,
    currency: Currency,
    import_batch_id: str,
    coverage: StatementCoverage,
    statement: ApprovedStatement,
    received_at: datetime,
) -> None:
    context = repository.add_context(
        ImportContextRecord(
            import_batch_id=import_batch_id,
            flags_json=[],
            note=None,
            created_at=received_at,
        )
    )
    repository.add_coverage(
        StatementCoverageRecord(
            import_context_id=context.id,
            statement_start_date=coverage.statement_start_date,
            statement_end_date=coverage.statement_end_date,
            coverage_status=coverage.status.value,
            missing_periods_json=[
                gap.model_dump(mode="json") for gap in coverage.missing_periods
            ],
        )
    )
    balances = statement.balances
    assert balances is not None
    assert balances.opening_balance is not None
    assert balances.closing_balance is not None
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
        repository.add_balance(
            BalanceSnapshotRecord(
                account_id=account_id,
                import_batch_id=import_batch_id,
                balance=balance,
                currency=currency.value,
                as_of_date=as_of_date,
                recorded_at=received_at,
                source=source.value,
                verification_status=VerificationStatus.VERIFIED.value,
            )
        )


def _repeated_summary(
    statement: ApprovedStatement,
    *,
    batch: ImportBatchRecord,
    coverage: StatementCoverage,
    rows: tuple[tuple[PersistablePdfRow, bool], ...],
    statement_repository: StatementRepository,
) -> PdfImportSummary:
    stored = statement_repository.get_coverage_for_batch(batch.id)
    effective_coverage = coverage if stored is None else _coverage_from_record(stored)
    locations = tuple(_location(row) for row, _rejected in rows)
    return PdfImportSummary(
        import_batch_id=batch.id,
        file_hash=statement.file_hash,
        rows_read=len(rows),
        imported_transactions=0,
        exact_duplicates_skipped=len(rows),
        probable_duplicates=0,
        rejected_rows=0,
        repeated_file=True,
        exact_duplicate_locations=locations,
        coverage=_coverage_analysis(
            statement_repository,
            account_id=batch.account_id,
            incoming=effective_coverage,
            exclude_batch_id=batch.id,
        ),
    )


def _persist_statement(
    session: Session,
    statement: ApprovedStatement,
    *,
    safe_filename: str,
    mime_type: str,
    byte_size: int,
    account_id: str,
    currency: Currency,
    coverage: StatementCoverage,
    rows: tuple[tuple[PersistablePdfRow, bool], ...],
    source_order: _SourceOrder,
    received_at: datetime,
) -> PdfImportSummary:
    account = AccountRepository(session).get(account_id)
    if account is None:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.ACCOUNT_NOT_FOUND,
            "selected PDF import account does not exist",
        )
    if not account.is_active:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.ACCOUNT_INACTIVE,
            "selected PDF import account is inactive",
        )
    if account.currency != currency.value:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.ACCOUNT_CURRENCY_MISMATCH,
            "approved PDF currency does not match the selected account",
        )

    batch_repository = ImportBatchRepository(session)
    statement_repository = StatementRepository(session)
    prior_batch = batch_repository.get_by_file_hash(account_id, statement.file_hash)
    if prior_batch is not None:
        return _repeated_summary(
            statement,
            batch=prior_batch,
            coverage=coverage,
            rows=rows,
            statement_repository=statement_repository,
        )

    coverage_analysis = _coverage_analysis(
        statement_repository,
        account_id=account_id,
        incoming=coverage,
    )
    batch = batch_repository.add(
        ImportBatchRecord(
            account_id=account_id,
            source_type=SourceType.DIGITAL_PDF.value,
            source_filename=safe_filename,
            file_hash=statement.file_hash,
            mime_type=mime_type,
            byte_size=byte_size,
            verification_status=VerificationStatus.UNVERIFIED.value,
            imported_at=received_at,
        )
    )
    _persist_statement_metadata(
        statement_repository,
        account_id=account_id,
        currency=currency,
        import_batch_id=batch.id,
        coverage=coverage,
        statement=statement,
        received_at=received_at,
    )

    transaction_repository = TransactionRepository(session)
    balance_repository = BalanceSnapshotRepository(session)
    exact_locations: list[PdfRecordLocation] = []
    probable_locations: list[PdfRecordLocation] = []
    rejected_locations: list[PdfRecordLocation] = []
    daily_balance_fingerprints = _daily_running_balance_fingerprints(
        rows,
        source_order=source_order,
    )
    imported_count = 0
    for source_row, was_rejected in rows:
        location = _location(source_row)
        base_issues = [_issue_payload(issue) for issue in source_row.issues]
        if (
            transaction_repository.get_raw_by_source_fingerprint(
                source_row.source_fingerprint
            )
            is not None
        ):
            raise PdfPersistenceError(
                PdfPersistenceErrorCode.DUPLICATE_SOURCE_ROW,
                "the PDF source row is already retained by another import",
            )
        if was_rejected:
            rejected_locations.append(location)
            transaction_repository.add_raw(
                _raw_record(
                    import_batch_id=batch.id,
                    row=source_row,
                    canonical_fingerprint=None,
                    review_status=ReviewStatus.REJECTED,
                    issues=[
                        *base_issues,
                        _synthetic_issue(
                            "user_rejected_pdf_row",
                            "the user rejected this extracted PDF row",
                        ),
                    ],
                    candidate=None,
                    statement=statement,
                    received_at=received_at,
                )
            )
            continue

        row = cast(ApprovedReviewRow, source_row)
        transaction = row.transaction
        canonical_fingerprint = calculate_canonical_fingerprint(transaction)
        duplicate = _best_duplicate(
            _duplicate_facts(row, canonical_fingerprint),
            transaction_repository,
        )
        if duplicate is not None:
            exact = duplicate.status is DuplicateStatus.EXACT
            (exact_locations if exact else probable_locations).append(location)
            transaction_repository.add_raw(
                _raw_record(
                    import_batch_id=batch.id,
                    row=row,
                    canonical_fingerprint=canonical_fingerprint,
                    review_status=(
                        ReviewStatus.REJECTED if exact else ReviewStatus.NEEDS_REVIEW
                    ),
                    issues=[
                        *base_issues,
                        _synthetic_issue(
                            "exact_duplicate" if exact else "probable_duplicate",
                            (
                                "row matches an existing transaction and was skipped"
                                if exact
                                else "row may duplicate an existing transaction"
                            ),
                            score=duplicate.score,
                            reasons=[reason.value for reason in duplicate.reasons],
                            existing_source_fingerprint=(
                                duplicate.existing_source_fingerprint
                            ),
                        ),
                    ],
                    candidate=None if exact else transaction,
                    statement=statement,
                    received_at=received_at,
                )
            )
            continue

        raw = transaction_repository.add_raw(
            _raw_record(
                import_batch_id=batch.id,
                row=row,
                canonical_fingerprint=canonical_fingerprint,
                review_status=ReviewStatus.CONFIRMED,
                issues=base_issues,
                candidate=None,
                statement=statement,
                received_at=received_at,
            )
        )
        verified = transaction_repository.add_verified(
            _verified_record(
                transaction,
                raw_transaction_id=raw.id,
                received_at=received_at,
            )
        )
        if row.source_fingerprint in daily_balance_fingerprints:
            assert verified.balance_after is not None
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
        imported_count += 1

    batch.verification_status = (
        VerificationStatus.NEEDS_REVIEW.value
        if probable_locations
        else VerificationStatus.VERIFIED.value
    )
    invalidate_derived_results_in_session(
        session,
        account_id=account_id,
        change_type=SourceDataChangeType.STATEMENT_ADDED,
        changed_at=received_at,
    )
    return PdfImportSummary(
        import_batch_id=batch.id,
        file_hash=statement.file_hash,
        rows_read=len(rows),
        imported_transactions=imported_count,
        exact_duplicates_skipped=len(exact_locations),
        probable_duplicates=len(probable_locations),
        rejected_rows=len(rejected_locations),
        exact_duplicate_locations=tuple(exact_locations),
        probable_duplicate_locations=tuple(probable_locations),
        rejected_locations=tuple(rejected_locations),
        coverage=coverage_analysis,
    )


def persist_approved_pdf(
    factory: sessionmaker[Session],
    content: bytes,
    filename: str,
    *,
    mime_type: str,
    statement: ApprovedStatement,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    spatial_mapping: SpatialColumnMapping | None = None,
) -> PdfImportSummary:
    """Validate and atomically store one exact, explicitly approved digital PDF."""
    if max_bytes <= 0:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_LIMIT,
            "PDF byte limit must be positive",
        )
    safe_filename = _safe_pdf_filename(filename)
    if mime_type not in ALLOWED_PDF_MIME_TYPES:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.UNSUPPORTED_MIME_TYPE,
            "PDF source has an unsupported MIME type",
        )
    if not content:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.EMPTY_FILE,
            "PDF source is empty",
        )
    if len(content) > max_bytes:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.FILE_TOO_LARGE,
            "PDF source exceeds the configured byte limit",
        )
    if b"%PDF-" not in content[:1024]:
        raise PdfPersistenceError(
            PdfPersistenceErrorCode.INVALID_SIGNATURE,
            "PDF source does not contain a valid file signature",
        )

    file_hash = calculate_file_hash(content)
    received_at = utc_now()
    account_id, currency, coverage, rows, source_order = _validate_statement(
        statement,
        file_hash=file_hash,
        received_at=received_at,
    )
    trusted_statement = _reextract_approved_statement(
        content,
        safe_filename,
        mime_type=mime_type,
        statement=statement,
        account_id=account_id,
        currency=currency,
        max_bytes=max_bytes,
        spatial_mapping=spatial_mapping,
    )
    account_id, currency, coverage, rows, source_order = _validate_statement(
        trusted_statement,
        file_hash=file_hash,
        received_at=received_at,
    )
    with session_scope(factory) as session:
        return _persist_statement(
            session,
            trusted_statement,
            safe_filename=safe_filename,
            mime_type=mime_type,
            byte_size=len(content),
            account_id=account_id,
            currency=currency,
            coverage=coverage,
            rows=rows,
            source_order=source_order,
            received_at=received_at,
        )
