"""Duplicate, repeated-file, and statement-overlap contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.normalisation import Sha256Digest
from cashflow_ai.schemas.statements import DateRange, StatementCoverage
from cashflow_ai.schemas.transactions import Identifier


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
