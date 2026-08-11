"""Statement reconciliation and explicit extraction-review contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from cashflow_ai.schemas.imports import (
    Confidence,
    ExtractionProvenance,
    FieldConfidence,
    ImportIssue,
    SourceType,
)
from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.normalisation import (
    OriginalTransactionValues,
    Sha256Digest,
    SourceRecordIdentity,
)
from cashflow_ai.schemas.statements import StatementBalances, StatementCoverage
from cashflow_ai.schemas.transactions import CanonicalTransaction, TransactionDraft

ConfidenceThreshold = Annotated[float, Field(gt=0, le=1)]


class _ReviewContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class ReconciliationStatus(StrEnum):
    """Outcome of comparing statement balances with extracted transactions."""

    RECONCILED = "reconciled"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


class DateFormat(StrEnum):
    """Date interpretation explicitly confirmed for ambiguous source text."""

    DAY_FIRST = "day_first"
    MONTH_FIRST = "month_first"
    ISO = "iso"


class AmountSignConvention(StrEnum):
    """Source amount convention explicitly confirmed for the statement."""

    SIGNED_AMOUNT = "signed_amount"
    DEBIT_NEGATIVE_CREDIT_POSITIVE = "debit_negative_credit_positive"


class ReviewReason(StrEnum):
    """Stable reason that an extracted transaction needs targeted review."""

    EXTRACTION_ERROR = "extraction_error"
    LOW_OCR_CONFIDENCE = "low_ocr_confidence"


class RowDecision(StrEnum):
    """Explicit decision for a targeted statement-review row."""

    CONFIRM = "confirm"
    REJECT = "reject"


class StatementBalanceField(StrEnum):
    """Reported statement balance represented by extraction evidence."""

    OPENING = "opening"
    CLOSING = "closing"


class StatementBalanceEvidence(_ReviewContract):
    """Raw PDF evidence and provenance for one reported balance value."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )

    field: StatementBalanceField
    raw_amount_text: str = Field(min_length=1)
    amount: Money | None
    source_identity: SourceRecordIdentity
    provenance: ExtractionProvenance
    line_number: PositiveInt
    confidence: Confidence | None = None
    issues: tuple[ImportIssue, ...] = ()

    @model_validator(mode="after")
    def validate_pdf_lineage(self) -> StatementBalanceEvidence:
        """Bind balance evidence to one PDF page and explain invalid values."""
        if self.source_identity.source_type not in {
            SourceType.DIGITAL_PDF,
            SourceType.OCR_PDF,
        }:
            msg = "statement balance evidence requires PDF lineage"
            raise ValueError(msg)
        if self.provenance.source_type is not self.source_identity.source_type:
            msg = "balance provenance must match its source identity"
            raise ValueError(msg)
        if self.provenance.page_number != self.source_identity.page_number:
            msg = "balance provenance must match its source page"
            raise ValueError(msg)
        if self.source_identity.page_record_number != self.line_number:
            msg = "balance source identity must match its source line"
            raise ValueError(msg)
        if self.amount is None and not self.issues:
            msg = "an unparsed balance requires a structured issue"
            raise ValueError(msg)
        return self


class StatementReconciliation(_ReviewContract):
    """Arithmetic evidence for one extracted statement."""

    status: ReconciliationStatus
    opening_balance: Money | None
    signed_transaction_total: Money
    expected_closing_balance: Money | None
    closing_balance: Money | None
    unexplained_difference: Money | None
    tolerance: Money = Field(default=Decimal("0.01"), ge=0)
    unusable_transaction_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_status_values(self) -> StatementReconciliation:
        """Keep availability and arithmetic fields internally consistent."""
        arithmetic = (
            self.opening_balance,
            self.expected_closing_balance,
            self.closing_balance,
            self.unexplained_difference,
        )
        if self.status is ReconciliationStatus.UNAVAILABLE:
            if any(value is not None for value in arithmetic):
                msg = "unavailable reconciliation cannot claim balance arithmetic"
                raise ValueError(msg)
        elif any(value is None for value in arithmetic):
            msg = "available reconciliation requires complete balance arithmetic"
            raise ValueError(msg)
        return self


class StatementReviewRow(_ReviewContract):
    """Preserved extraction evidence plus the editable working transaction."""

    source_identity: SourceRecordIdentity
    source_fingerprint: Sha256Digest
    original: OriginalTransactionValues
    extracted_draft: TransactionDraft
    working_draft: TransactionDraft
    provenance: ExtractionProvenance
    source_line_numbers: tuple[PositiveInt, ...] = ()
    field_confidences: tuple[FieldConfidence, ...] = ()
    issues: tuple[ImportIssue, ...] = ()
    review_reasons: frozenset[ReviewReason] = frozenset()

    @property
    def requires_review(self) -> bool:
        """Return whether this row belongs in the targeted review queue."""
        return bool(self.review_reasons)

    @property
    def was_edited(self) -> bool:
        """Return whether the working values differ from extracted values."""
        return self.working_draft != self.extracted_draft

    @model_validator(mode="after")
    def validate_source_lineage(self) -> StatementReviewRow:
        """Keep row source identity, provenance, and OCR lines coherent."""
        if self.provenance.source_type is not self.source_identity.source_type:
            msg = "row provenance must match its source identity"
            raise ValueError(msg)
        if self.provenance.page_number != self.source_identity.page_number:
            msg = "row provenance must match its source page"
            raise ValueError(msg)
        if self.source_identity.source_type is SourceType.OCR_PDF:
            if not self.source_line_numbers:
                msg = "OCR review rows require source line numbers"
                raise ValueError(msg)
        elif self.source_line_numbers:
            msg = "digital-PDF review rows cannot claim OCR source lines"
            raise ValueError(msg)
        return self


class StatementReview(_ReviewContract):
    """One non-persistent statement review bound to exact source bytes."""

    file_hash: Sha256Digest
    source_type: SourceType
    statement_coverage: StatementCoverage | None
    balances: StatementBalances | None
    balance_evidence: tuple[StatementBalanceEvidence, ...] = ()
    document_issues: tuple[ImportIssue, ...] = ()
    rows: tuple[StatementReviewRow, ...] = Field(min_length=1)
    reconciliation: StatementReconciliation
    ocr_confidence_threshold: ConfidenceThreshold
    requires_date_format_confirmation: bool
    requires_debit_credit_sign_confirmation: bool
    requires_statement_approval: Literal[True] = True

    @property
    def uncertain_rows(self) -> tuple[StatementReviewRow, ...]:
        """Return only rows needing a targeted user decision."""
        return tuple(row for row in self.rows if row.requires_review)

    @model_validator(mode="after")
    def validate_exact_source_binding(self) -> StatementReview:
        """Bind every review item to the exact PDF hash and source adapter."""
        if self.source_type not in {SourceType.DIGITAL_PDF, SourceType.OCR_PDF}:
            msg = "statement review requires PDF source data"
            raise ValueError(msg)
        identities = (
            *(row.source_identity for row in self.rows),
            *(evidence.source_identity for evidence in self.balance_evidence),
        )
        if any(
            identity.source_document_hash != self.file_hash
            or identity.source_type is not self.source_type
            for identity in identities
        ):
            msg = "all review evidence must match the exact statement source"
            raise ValueError(msg)
        account_ids = {
            row.extracted_draft.account_id
            for row in self.rows
            if row.extracted_draft.account_id is not None
        }
        currencies = {
            row.extracted_draft.currency
            for row in self.rows
            if row.extracted_draft.currency is not None
        }
        if len(account_ids) != 1:
            msg = "one statement review must target exactly one account"
            raise ValueError(msg)
        if len(currencies) != 1:
            msg = "one statement review must use exactly one currency"
            raise ValueError(msg)
        return self


class RowReview(_ReviewContract):
    """One explicit user decision, optionally with corrected values."""

    source_fingerprint: Sha256Digest
    decision: RowDecision
    corrected_draft: TransactionDraft | None = None

    @model_validator(mode="after")
    def validate_correction(self) -> RowReview:
        """Prevent rejected rows from carrying unused corrected values."""
        if self.decision is RowDecision.REJECT and self.corrected_draft is not None:
            msg = "rejected rows cannot include a corrected transaction"
            raise ValueError(msg)
        return self


class StatementApproval(_ReviewContract):
    """Explicit approval choices for the exact reviewed statement."""

    file_hash: Sha256Digest
    approved_at: datetime
    statement_approved: Literal[True]
    date_format: DateFormat | None = None
    sign_convention: AmountSignConvention | None = None
    confirmed_statement_coverage: StatementCoverage | None = None
    confirmed_balances: StatementBalances | None = None
    acknowledge_balance_mismatch: bool = False
    row_reviews: tuple[RowReview, ...] = ()

    @model_validator(mode="after")
    def require_aware_approval_time(self) -> StatementApproval:
        """Require an explicit timezone for the audit timestamp."""
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            msg = "statement approval time must be timezone-aware"
            raise ValueError(msg)
        return self


class ApprovedReviewRow(_ReviewContract):
    """Trusted canonical transaction retaining immutable source evidence."""

    source_identity: SourceRecordIdentity
    source_fingerprint: Sha256Digest
    original: OriginalTransactionValues
    extracted_draft: TransactionDraft
    provenance: ExtractionProvenance
    source_line_numbers: tuple[PositiveInt, ...] = ()
    field_confidences: tuple[FieldConfidence, ...] = ()
    issues: tuple[ImportIssue, ...] = ()
    review_reasons: frozenset[ReviewReason] = frozenset()
    row_decision: RowDecision | None = None
    transaction: CanonicalTransaction
    was_edited: bool


class ApprovedStatement(_ReviewContract):
    """Only statement-review output eligible for trusted downstream use."""

    file_hash: Sha256Digest
    source_type: SourceType
    approved_at: datetime
    date_format: DateFormat | None
    sign_convention: AmountSignConvention | None
    statement_coverage: StatementCoverage | None
    coverage_was_edited: bool
    balances: StatementBalances | None
    balance_evidence: tuple[StatementBalanceEvidence, ...]
    balance_was_edited: bool
    document_issues: tuple[ImportIssue, ...]
    rows: tuple[ApprovedReviewRow, ...]
    rejected_rows: tuple[StatementReviewRow, ...]
    rejected_source_fingerprints: tuple[Sha256Digest, ...]
    reconciliation: StatementReconciliation
