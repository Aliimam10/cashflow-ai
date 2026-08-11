"""Balance reconciliation and statement-level extraction approval."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import ValidationError

from cashflow_ai.imports.normalisation import (
    TransactionNormalisationError,
    parse_amount_value,
)
from cashflow_ai.schemas.imports import (
    ExtractionMethod,
    ExtractionProvenance,
    ImportIssue,
    IssueSeverity,
    SourceType,
)
from cashflow_ai.schemas.normalisation import SourceRecordIdentity
from cashflow_ai.schemas.ocr_imports import (
    OcrPageExtraction,
    OcrPdfPreview,
    OcrTransactionCandidate,
)
from cashflow_ai.schemas.pdf_imports import PdfTransactionCandidate, TextPdfPreview
from cashflow_ai.schemas.reconciliation import (
    AmountSignConvention,
    ApprovedReviewRow,
    ApprovedStatement,
    DateFormat,
    ReconciliationStatus,
    ReviewReason,
    RowDecision,
    RowReview,
    StatementApproval,
    StatementBalanceEvidence,
    StatementBalanceField,
    StatementReconciliation,
    StatementReview,
    StatementReviewRow,
)
from cashflow_ai.schemas.statements import StatementBalances, StatementCoverage
from cashflow_ai.schemas.transactions import CanonicalTransaction, TransactionDraft

DEFAULT_OCR_CONFIDENCE_THRESHOLD: Final = 0.85
DEFAULT_RECONCILIATION_TOLERANCE: Final = Decimal("0.01")
_AMBIGUOUS_SLASH_DATE = re.compile(r"^(?P<first>\d{1,2})/(?P<second>\d{1,2})/\d{2,4}$")
_BALANCE_LINE = re.compile(
    r"\b(?P<kind>opening|closing)\s+balance\s*:?\s*(?P<amount>.+?)\s*$",
    re.I,
)
_PROTECTED_CORRECTION_FIELDS: Final = (
    "account_id",
    "currency",
    "category_id",
    "financial_role",
)

type PdfCandidate = PdfTransactionCandidate | OcrTransactionCandidate
type PdfPreview = TextPdfPreview | OcrPdfPreview


class StatementReviewErrorCode(StrEnum):
    """Stable statement-review failures suitable for an API boundary."""

    INVALID_THRESHOLD = "invalid_threshold"
    FILE_CHANGED = "file_changed"
    DUPLICATE_DECISION = "duplicate_decision"
    UNKNOWN_ROW = "unknown_row"
    UNCERTAIN_ROW_UNRESOLVED = "uncertain_row_unresolved"
    DATE_FORMAT_UNCONFIRMED = "date_format_unconfirmed"
    DATE_FORMAT_CONFLICT = "date_format_conflict"
    SIGN_CONVENTION_UNCONFIRMED = "sign_convention_unconfirmed"
    PROTECTED_FIELD_CHANGED = "protected_field_changed"
    BALANCE_EVIDENCE_MISSING = "balance_evidence_missing"
    BALANCES_UNCONFIRMED = "balances_unconfirmed"
    UNEXPECTED_BALANCE_CONFIRMATION = "unexpected_balance_confirmation"
    BALANCE_FIELDS_CHANGED = "balance_fields_changed"
    COVERAGE_UNCONFIRMED = "coverage_unconfirmed"
    TRANSACTION_OUTSIDE_COVERAGE = "transaction_outside_coverage"
    INVALID_CONFIRMED_ROW = "invalid_confirmed_row"
    BALANCE_MISMATCH_UNACKNOWLEDGED = "balance_mismatch_unacknowledged"


class StatementReviewError(ValueError):
    """Controlled statement-review failure without private source values."""

    def __init__(self, code: StatementReviewErrorCode, message: str) -> None:
        """Store a stable public code without source transaction values."""
        super().__init__(message)
        self.code = code


def reconcile_statement(
    balances: StatementBalances | None,
    amounts: Iterable[Decimal | None],
    *,
    tolerance: Decimal = DEFAULT_RECONCILIATION_TOLERANCE,
) -> StatementReconciliation:
    """Compare reported balances with the complete signed transaction total."""
    if tolerance < 0:
        raise ValueError("reconciliation tolerance cannot be negative")
    amount_values = tuple(amounts)
    unusable_count = sum(amount is None for amount in amount_values)
    signed_total = sum(
        (amount for amount in amount_values if amount is not None),
        start=Decimal("0.00"),
    )
    complete_balances = (
        balances is not None
        and balances.opening_balance is not None
        and balances.closing_balance is not None
    )
    if not complete_balances or unusable_count:
        return StatementReconciliation(
            status=ReconciliationStatus.UNAVAILABLE,
            opening_balance=None,
            signed_transaction_total=signed_total,
            expected_closing_balance=None,
            closing_balance=None,
            unexplained_difference=None,
            tolerance=tolerance,
            unusable_transaction_count=unusable_count,
        )

    assert balances is not None
    assert balances.opening_balance is not None
    assert balances.closing_balance is not None
    expected = balances.opening_balance + signed_total
    difference = balances.closing_balance - expected
    status = (
        ReconciliationStatus.RECONCILED
        if abs(difference) <= tolerance
        else ReconciliationStatus.MISMATCH
    )
    return StatementReconciliation(
        status=status,
        opening_balance=balances.opening_balance,
        signed_transaction_total=signed_total,
        expected_closing_balance=expected,
        closing_balance=balances.closing_balance,
        unexplained_difference=difference,
        tolerance=tolerance,
        unusable_transaction_count=0,
    )


def _has_ambiguous_date(value: str | None) -> bool:
    if value is None:
        return False
    match = _AMBIGUOUS_SLASH_DATE.fullmatch(value.strip())
    if match is None:
        return False
    first = int(match.group("first"))
    second = int(match.group("second"))
    return first <= 12 and second <= 12 and first != second


def _candidate_has_ambiguous_date(candidate: PdfCandidate) -> bool:
    return _has_ambiguous_date(
        candidate.original.transaction_date_text
    ) or _has_ambiguous_date(candidate.original.posting_date_text)


def _uses_debit_credit(candidate: PdfCandidate) -> bool:
    return (
        candidate.original.debit_amount_text is not None
        or candidate.original.credit_amount_text is not None
    )


def _review_reasons(
    candidate: PdfCandidate,
    *,
    ocr_confidence_threshold: float,
) -> frozenset[ReviewReason]:
    reasons: set[ReviewReason] = set()
    if candidate.canonical_fingerprint is None or any(
        issue.severity is IssueSeverity.ERROR for issue in candidate.issues
    ):
        reasons.add(ReviewReason.EXTRACTION_ERROR)
    if isinstance(candidate, OcrTransactionCandidate) and any(
        confidence.confidence < ocr_confidence_threshold
        for confidence in candidate.field_confidences
    ):
        reasons.add(ReviewReason.LOW_OCR_CONFIDENCE)
    return frozenset(reasons)


def _balance_evidence(
    preview: PdfPreview,
    *,
    source_type: SourceType,
) -> tuple[StatementBalanceEvidence, ...]:
    """Recover raw balance values with page, line, method, and OCR confidence."""
    method = (
        ExtractionMethod.OCR
        if source_type is SourceType.OCR_PDF
        else ExtractionMethod.PDF_TEXT
    )
    parser_by_page = {
        candidate.source_identity.page_number: candidate.provenance.parser
        for candidate in preview.candidates
    }
    evidence: list[StatementBalanceEvidence] = []
    for page in preview.pages:
        source_lines: tuple[tuple[int, str, float | None], ...]
        if isinstance(page, OcrPageExtraction):
            source_lines = tuple(
                (line.line_number, line.raw_text, line.confidence)
                for line in page.lines
            )
        else:
            source_lines = tuple(
                (line_number, raw_text, None)
                for line_number, raw_text in enumerate(
                    page.raw_text.splitlines(),
                    start=1,
                )
            )
        for line_number, raw_text, confidence in source_lines:
            match = _BALANCE_LINE.search(raw_text.strip())
            if match is None:
                continue
            kind = match.group("kind").casefold()
            raw_amount = match.group("amount")
            issues: tuple[ImportIssue, ...] = ()
            try:
                amount = parse_amount_value(raw_amount, f"{kind} balance")
            except TransactionNormalisationError:
                amount = None
                issues = (
                    ImportIssue(
                        code=f"invalid_{kind}_balance",
                        message=f"the detected {kind} balance could not be validated",
                        severity=IssueSeverity.WARNING,
                    ),
                )
            evidence.append(
                StatementBalanceEvidence(
                    field=StatementBalanceField(kind),
                    raw_amount_text=raw_amount,
                    amount=amount,
                    source_identity=SourceRecordIdentity(
                        source_type=source_type,
                        source_document_hash=preview.file_hash,
                        page_number=page.page_number,
                        page_record_number=line_number,
                    ),
                    provenance=ExtractionProvenance(
                        source_type=source_type,
                        method=method,
                        page_number=page.page_number,
                        confidence=confidence,
                        parser=parser_by_page.get(page.page_number),
                    ),
                    line_number=line_number,
                    confidence=confidence,
                    issues=issues,
                )
            )
    return tuple(evidence)


def _validate_balance_evidence(
    balances: StatementBalances | None,
    evidence: tuple[StatementBalanceEvidence, ...],
) -> None:
    if balances is None:
        return
    evidence_values = {
        (item.field, item.amount) for item in evidence if item.amount is not None
    }
    expected_values = {
        (StatementBalanceField.OPENING, balances.opening_balance),
        (StatementBalanceField.CLOSING, balances.closing_balance),
    }
    expected_values = {item for item in expected_values if item[1] is not None}
    if not expected_values.issubset(evidence_values):
        raise StatementReviewError(
            StatementReviewErrorCode.BALANCE_EVIDENCE_MISSING,
            "parsed statement balances must retain matching raw PDF evidence",
        )


def prepare_statement_review(
    preview: PdfPreview,
    *,
    ocr_confidence_threshold: float = DEFAULT_OCR_CONFIDENCE_THRESHOLD,
) -> StatementReview:
    """Build a targeted, non-persistent review from one exact PDF preview."""
    if not 0 < ocr_confidence_threshold <= 1:
        raise StatementReviewError(
            StatementReviewErrorCode.INVALID_THRESHOLD,
            "OCR confidence threshold must be greater than zero and at most one",
        )
    source_type = (
        SourceType.OCR_PDF
        if isinstance(preview, OcrPdfPreview)
        else SourceType.DIGITAL_PDF
    )
    rows = tuple(
        StatementReviewRow(
            source_identity=candidate.source_identity,
            source_fingerprint=candidate.source_fingerprint,
            original=candidate.original,
            extracted_draft=candidate.draft,
            working_draft=candidate.draft,
            provenance=candidate.provenance,
            source_line_numbers=(
                candidate.line_numbers
                if isinstance(candidate, OcrTransactionCandidate)
                else ()
            ),
            field_confidences=(
                candidate.field_confidences
                if isinstance(candidate, OcrTransactionCandidate)
                else ()
            ),
            issues=candidate.issues,
            review_reasons=_review_reasons(
                candidate,
                ocr_confidence_threshold=ocr_confidence_threshold,
            ),
        )
        for candidate in preview.candidates
    )
    balances = preview.statement_balances
    balance_evidence = _balance_evidence(preview, source_type=source_type)
    _validate_balance_evidence(balances, balance_evidence)
    return StatementReview(
        file_hash=preview.file_hash,
        source_type=source_type,
        statement_coverage=preview.statement_coverage,
        balances=balances,
        balance_evidence=balance_evidence,
        document_issues=preview.document_issues,
        rows=rows,
        reconciliation=reconcile_statement(
            balances,
            (row.working_draft.amount for row in rows),
        ),
        ocr_confidence_threshold=ocr_confidence_threshold,
        requires_date_format_confirmation=any(
            _candidate_has_ambiguous_date(candidate) for candidate in preview.candidates
        ),
        requires_debit_credit_sign_confirmation=any(
            _uses_debit_credit(candidate) for candidate in preview.candidates
        ),
    )


def _reviews_by_fingerprint(
    review: StatementReview,
    approval: StatementApproval,
) -> dict[str, RowReview]:
    decisions: dict[str, RowReview] = {}
    known = {row.source_fingerprint for row in review.rows}
    for row_review in approval.row_reviews:
        fingerprint = row_review.source_fingerprint
        if fingerprint in decisions:
            raise StatementReviewError(
                StatementReviewErrorCode.DUPLICATE_DECISION,
                "a statement row cannot have more than one review decision",
            )
        if fingerprint not in known:
            raise StatementReviewError(
                StatementReviewErrorCode.UNKNOWN_ROW,
                "the approval references a row outside this statement review",
            )
        decisions[fingerprint] = row_review
    return decisions


def _canonical_transaction(draft: TransactionDraft) -> CanonicalTransaction:
    try:
        return CanonicalTransaction.model_validate(draft.model_dump(exclude_none=True))
    except ValidationError as error:
        raise StatementReviewError(
            StatementReviewErrorCode.INVALID_CONFIRMED_ROW,
            "a confirmed statement row is not a complete canonical transaction",
        ) from error


def _date_from_confirmed_format(value: str, date_format: DateFormat) -> date:
    cleaned = value.strip()
    if date_format is DateFormat.ISO:
        raise StatementReviewError(
            StatementReviewErrorCode.DATE_FORMAT_CONFLICT,
            "an ambiguous slash date cannot use the ISO date interpretation",
        )
    year_format = "%y" if len(cleaned.rsplit("/", maxsplit=1)[-1]) == 2 else "%Y"
    prefix = "%d/%m/" if date_format is DateFormat.DAY_FIRST else "%m/%d/"
    try:
        return datetime.strptime(cleaned, f"{prefix}{year_format}").date()
    except ValueError as error:
        raise StatementReviewError(
            StatementReviewErrorCode.DATE_FORMAT_CONFLICT,
            "the source date cannot use the confirmed date interpretation",
        ) from error


def _apply_confirmed_date_format(
    row: StatementReviewRow,
    draft: TransactionDraft,
    date_format: DateFormat | None,
) -> TransactionDraft:
    updates: dict[str, object] = {}
    date_values = (
        ("transaction_date", row.original.transaction_date_text),
        ("posting_date", row.original.posting_date_text),
    )
    for field_name, raw_value in date_values:
        if not _has_ambiguous_date(raw_value):
            continue
        if getattr(draft, field_name) != getattr(row.extracted_draft, field_name):
            continue
        assert raw_value is not None
        assert date_format is not None
        updates[field_name] = _date_from_confirmed_format(raw_value, date_format)
    return draft.model_copy(update=updates) if updates else draft


def _validate_protected_correction(
    row: StatementReviewRow,
    draft: TransactionDraft,
) -> None:
    if any(
        getattr(draft, field_name) != getattr(row.extracted_draft, field_name)
        for field_name in _PROTECTED_CORRECTION_FIELDS
    ):
        raise StatementReviewError(
            StatementReviewErrorCode.PROTECTED_FIELD_CHANGED,
            "statement row corrections cannot change account, currency, "
            "category, or financial role",
        )


def _confirmed_statement_coverage(
    review: StatementReview,
    approval: StatementApproval,
) -> StatementCoverage | None:
    confirmed = approval.confirmed_statement_coverage
    if confirmed is None and (
        review.statement_coverage is not None or review.balance_evidence
    ):
        raise StatementReviewError(
            StatementReviewErrorCode.COVERAGE_UNCONFIRMED,
            "confirm or correct the statement period before approving balances",
        )
    return confirmed


def _validate_transactions_within_coverage(
    coverage: StatementCoverage | None,
    rows: list[ApprovedReviewRow],
) -> None:
    if coverage is None:
        return
    for row in rows:
        transaction_date = row.transaction.transaction_date
        outside_bounds = (
            transaction_date < coverage.statement_start_date
            or transaction_date > coverage.statement_end_date
        )
        inside_gap = any(
            gap.start_date <= transaction_date <= gap.end_date
            for gap in coverage.missing_periods
        )
        if outside_bounds or inside_gap:
            raise StatementReviewError(
                StatementReviewErrorCode.TRANSACTION_OUTSIDE_COVERAGE,
                "a confirmed transaction falls outside the confirmed "
                "statement coverage",
            )


def _confirmed_balances(
    review: StatementReview,
    approval: StatementApproval,
) -> StatementBalances | None:
    evidence_fields = {item.field for item in review.balance_evidence}
    confirmed = approval.confirmed_balances
    if not evidence_fields:
        if confirmed is not None:
            raise StatementReviewError(
                StatementReviewErrorCode.UNEXPECTED_BALANCE_CONFIRMATION,
                "balances cannot be added when the PDF contained no balance evidence",
            )
        return None
    if confirmed is None:
        raise StatementReviewError(
            StatementReviewErrorCode.BALANCES_UNCONFIRMED,
            "confirm or correct every extracted statement balance before approval",
        )
    confirmed_fields = {
        field
        for field, amount in (
            (StatementBalanceField.OPENING, confirmed.opening_balance),
            (StatementBalanceField.CLOSING, confirmed.closing_balance),
        )
        if amount is not None
    }
    if confirmed_fields != evidence_fields:
        raise StatementReviewError(
            StatementReviewErrorCode.BALANCE_FIELDS_CHANGED,
            "confirmed balances must match the opening and closing fields "
            "found in the PDF",
        )
    return confirmed


def approve_statement_review(
    review: StatementReview,
    approval: StatementApproval,
) -> ApprovedStatement:
    """Approve exact source bytes and release only canonical reviewed rows."""
    if approval.file_hash != review.file_hash:
        raise StatementReviewError(
            StatementReviewErrorCode.FILE_CHANGED,
            "statement approval does not match the reviewed source bytes",
        )
    if review.requires_date_format_confirmation and approval.date_format is None:
        raise StatementReviewError(
            StatementReviewErrorCode.DATE_FORMAT_UNCONFIRMED,
            "confirm the source date format before approving this statement",
        )
    if (
        review.requires_debit_credit_sign_confirmation
        and approval.sign_convention
        is not AmountSignConvention.DEBIT_NEGATIVE_CREDIT_POSITIVE
    ):
        raise StatementReviewError(
            StatementReviewErrorCode.SIGN_CONVENTION_UNCONFIRMED,
            "confirm debit as negative and credit as positive before approval",
        )

    decisions = _reviews_by_fingerprint(review, approval)
    confirmed_coverage = _confirmed_statement_coverage(review, approval)
    confirmed_balances = _confirmed_balances(review, approval)
    approved_rows: list[ApprovedReviewRow] = []
    rejected_rows: list[StatementReviewRow] = []
    rejected: list[str] = []
    final_amounts: list[Decimal | None] = []
    for row in review.rows:
        row_review = decisions.get(row.source_fingerprint)
        if row.requires_review and row_review is None:
            raise StatementReviewError(
                StatementReviewErrorCode.UNCERTAIN_ROW_UNRESOLVED,
                "every uncertain statement row requires an explicit decision",
            )
        if row_review is not None and row_review.decision is RowDecision.REJECT:
            rejected.append(row.source_fingerprint)
            rejected_rows.append(row)
            continue
        working = (
            row_review.corrected_draft
            if row_review is not None and row_review.corrected_draft is not None
            else row.working_draft
        )
        _validate_protected_correction(row, working)
        working = _apply_confirmed_date_format(row, working, approval.date_format)
        transaction = _canonical_transaction(working)
        final_amounts.append(transaction.amount)
        approved_rows.append(
            ApprovedReviewRow(
                source_identity=row.source_identity,
                source_fingerprint=row.source_fingerprint,
                original=row.original,
                extracted_draft=row.extracted_draft,
                provenance=row.provenance,
                source_line_numbers=row.source_line_numbers,
                field_confidences=row.field_confidences,
                issues=row.issues,
                review_reasons=row.review_reasons,
                row_decision=(row_review.decision if row_review is not None else None),
                transaction=transaction,
                was_edited=working != row.extracted_draft,
            )
        )

    _validate_transactions_within_coverage(confirmed_coverage, approved_rows)
    reconciliation = reconcile_statement(confirmed_balances, final_amounts)
    if (
        reconciliation.status is ReconciliationStatus.MISMATCH
        and not approval.acknowledge_balance_mismatch
    ):
        raise StatementReviewError(
            StatementReviewErrorCode.BALANCE_MISMATCH_UNACKNOWLEDGED,
            "acknowledge the unexplained balance difference before approval",
        )
    return ApprovedStatement(
        file_hash=review.file_hash,
        source_type=review.source_type,
        approved_at=approval.approved_at,
        date_format=approval.date_format,
        sign_convention=approval.sign_convention,
        statement_coverage=confirmed_coverage,
        coverage_was_edited=confirmed_coverage != review.statement_coverage,
        balances=confirmed_balances,
        balance_evidence=review.balance_evidence,
        balance_was_edited=confirmed_balances != review.balances,
        document_issues=review.document_issues,
        rows=tuple(approved_rows),
        rejected_rows=tuple(rejected_rows),
        rejected_source_fingerprints=tuple(rejected),
        reconciliation=reconciliation,
    )
