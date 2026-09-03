"""Duplicate, repeated-file, and statement-overlap contracts."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.imports import ReviewStatus, VerificationStatus
from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.normalisation import Sha256Digest
from cashflow_ai.schemas.statements import DateRange, StatementCoverage
from cashflow_ai.schemas.transactions import Currency, Identifier, TransactionDraft


class DuplicateStatus(StrEnum):
    """Confidence class for a transaction-pair comparison."""

    UNIQUE = "unique"
    PROBABLE = "probable"
    EXACT = "exact"


class DuplicateAction(StrEnum):
    """Safe downstream action for a duplicate assessment."""

    KEEP = "keep"
    REVIEW = "review"
    SKIP = "skip"


class DuplicateReviewDecision(StrEnum):
    """Explicit resolution choices for a probable duplicate candidate."""

    KEEP = "keep"
    REJECT = "reject"


class DuplicateReason(StrEnum):
    """Evidence contributing to a duplicate assessment."""

    SAME_SOURCE_RECORD = "same_source_record"
    SAME_EXTERNAL_ID = "same_external_id"
    SAME_CANONICAL_FINGERPRINT = "same_canonical_fingerprint"
    SAME_AMOUNT = "same_amount"
    CLOSE_DATE = "close_date"
    SIMILAR_DESCRIPTION = "similar_description"
    DIFFERENT_EXTERNAL_ID = "different_external_id"
    DIFFERENT_ACCOUNT = "different_account"
    INSUFFICIENT_MATCH = "insufficient_match"


class DuplicateFacts(BaseModel):
    """Minimal persisted or in-memory fields used for duplicate matching."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_fingerprint: Sha256Digest
    canonical_fingerprint: Sha256Digest
    account_id: Identifier
    transaction_date: date
    amount: Money
    description: str = Field(min_length=1, max_length=500)
    merchant: str | None = Field(default=None, min_length=1, max_length=500)
    external_id: Identifier | None = None


class DuplicateAssessment(BaseModel):
    """Explainable comparison between an incoming and an existing transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incoming_source_fingerprint: Sha256Digest
    existing_source_fingerprint: Sha256Digest
    status: DuplicateStatus
    action: DuplicateAction
    score: float = Field(ge=0, le=1)
    reasons: tuple[DuplicateReason, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_action(self) -> DuplicateAssessment:
        """Tie automatic skipping exclusively to exact duplicates."""
        expected = {
            DuplicateStatus.UNIQUE: DuplicateAction.KEEP,
            DuplicateStatus.PROBABLE: DuplicateAction.REVIEW,
            DuplicateStatus.EXACT: DuplicateAction.SKIP,
        }
        if self.action is not expected[self.status]:
            msg = "duplicate action does not match duplicate status"
            raise ValueError(msg)
        return self


class DuplicateCandidateSnapshot(BaseModel):
    """Versioned canonical draft retained only to resolve a probable candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    draft: TransactionDraft


class DuplicateTransactionSummary(BaseModel):
    """Minimal canonical transaction values needed for side-by-side review."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    transaction_id: Identifier | None = None
    account_id: Identifier
    transaction_date: date
    description: str = Field(min_length=1, max_length=500)
    amount: Decimal
    currency: Currency


class ProbableDuplicateReviewItem(BaseModel):
    """One unresolved raw candidate and the existing row it may duplicate."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    raw_transaction_id: Identifier
    import_batch_id: Identifier
    account_id: Identifier
    source_row_number: int | None = Field(default=None, ge=1)
    original_date_text: str
    original_description: str = Field(min_length=1, max_length=500)
    original_amount_text: str | None
    candidate: DuplicateTransactionSummary | None
    existing_transaction: DuplicateTransactionSummary | None
    score: float = Field(ge=0, le=1)
    reasons: tuple[DuplicateReason, ...] = Field(min_length=1)
    can_keep: bool

    @model_validator(mode="after")
    def validate_keep_readiness(self) -> ProbableDuplicateReviewItem:
        """Allow keeping only a complete retained candidate with a comparison row."""
        ready = self.candidate is not None and self.existing_transaction is not None
        if self.can_keep != ready:
            raise ValueError(
                "duplicate keep readiness does not match retained evidence"
            )
        return self


class DuplicateReviewRequest(BaseModel):
    """One explicit decision and its caller-observed aware time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: DuplicateReviewDecision
    decided_at: datetime

    @model_validator(mode="after")
    def validate_aware_time(self) -> DuplicateReviewRequest:
        """Reject ambiguous local timestamps at the API boundary."""
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("duplicate review time must be timezone-aware")
        return self


class DuplicateReviewResult(BaseModel):
    """Result of resolving one probable raw row without changing source evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_transaction_id: Identifier
    decision: DuplicateReviewDecision
    review_status: ReviewStatus
    kept_transaction_id: Identifier | None = None
    import_verification_status: VerificationStatus

    @model_validator(mode="after")
    def validate_result(self) -> DuplicateReviewResult:
        """Bind keeping to a confirmed raw row and a verified transaction ID."""
        kept = self.decision is DuplicateReviewDecision.KEEP
        if kept != (self.kept_transaction_id is not None):
            raise ValueError("only a kept duplicate candidate has a transaction ID")
        expected = ReviewStatus.CONFIRMED if kept else ReviewStatus.REJECTED
        if self.review_status is not expected:
            raise ValueError("duplicate decision does not match raw review status")
        return self


class RepeatedFileAssessment(BaseModel):
    """Whether an uploaded document hash has already been seen."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_hash: Sha256Digest
    repeated: bool


class StatementRecord(BaseModel):
    """Document identity and coverage used for overlap detection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_document_hash: Sha256Digest
    account_id: Identifier
    coverage: StatementCoverage


class StatementOverlapStatus(StrEnum):
    """Relationship between two statements for the same account."""

    NONE = "none"
    PARTIAL = "partial"
    EXACT = "exact"


class StatementOverlapAssessment(BaseModel):
    """Explainable date-range overlap between two statement records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incoming_document_hash: Sha256Digest
    existing_document_hash: Sha256Digest
    status: StatementOverlapStatus
    overlap_range: DateRange | None = None
    overlap_days: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_overlap(self) -> StatementOverlapAssessment:
        """Require a range and positive duration exactly when overlap exists."""
        if self.status is StatementOverlapStatus.NONE:
            if self.overlap_range is not None or self.overlap_days != 0:
                msg = "non-overlapping statements cannot contain an overlap range"
                raise ValueError(msg)
        elif self.overlap_range is None or self.overlap_days < 1:
            msg = "overlapping statements require a range and positive day count"
            raise ValueError(msg)
        return self
