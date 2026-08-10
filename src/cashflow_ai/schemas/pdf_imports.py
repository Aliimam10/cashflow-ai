"""Contracts for non-persistent digital-PDF statement extraction previews."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from cashflow_ai.schemas.imports import (
    ExtractionProvenance,
    ImportIssue,
    ReviewStatus,
    SourceType,
)
from cashflow_ai.schemas.normalisation import (
    OriginalTransactionValues,
    Sha256Digest,
    SourceRecordIdentity,
)
from cashflow_ai.schemas.statements import StatementBalances, StatementCoverage
from cashflow_ai.schemas.transactions import TransactionDraft


class PdfExtractionLayout(StrEnum):
    """Layout path used to produce a digital-PDF candidate."""

    TABLE = "table"
    GENERIC_TEXT = "generic_text"


class _PdfContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class PdfPageExtraction(_PdfContract):
    """Embedded text and table count extracted from one PDF page."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )

    page_number: PositiveInt
    raw_text: str = Field(min_length=1)
    embedded_character_count: PositiveInt
    tables_found: int = Field(ge=0)


class PdfTransactionCandidate(_PdfContract):
    """One PDF-derived row that must be reviewed before persistence."""

    original: OriginalTransactionValues
    draft: TransactionDraft
    source_identity: SourceRecordIdentity
    source_fingerprint: Sha256Digest
    canonical_fingerprint: Sha256Digest | None = None
    provenance: ExtractionProvenance
    issues: tuple[ImportIssue, ...] = ()
    review_status: Literal[ReviewStatus.NEEDS_REVIEW] = ReviewStatus.NEEDS_REVIEW

    @model_validator(mode="after")
    def validate_pdf_lineage_and_result(self) -> PdfTransactionCandidate:
        """Keep PDF page identity aligned and invalid candidates explainable."""
        if (
            self.source_identity.source_type is not SourceType.DIGITAL_PDF
            or self.provenance.source_type is not SourceType.DIGITAL_PDF
        ):
            msg = "text-PDF candidates require digital-PDF lineage"
            raise ValueError(msg)
        if self.provenance.page_number != self.source_identity.page_number:
            msg = "candidate provenance must match its source page"
            raise ValueError(msg)
        if self.canonical_fingerprint is None and not self.issues:
            msg = "non-canonical PDF candidates require a review issue"
            raise ValueError(msg)
        return self


class TextPdfPreview(_PdfContract):
    """Reviewable extraction result for one embedded-text PDF statement."""

    source_filename: str = Field(min_length=1, max_length=255)
    byte_size: PositiveInt
    file_hash: Sha256Digest
    page_count: PositiveInt
    pages: tuple[PdfPageExtraction, ...] = Field(min_length=1)
    layouts: frozenset[PdfExtractionLayout] = Field(min_length=1)
    statement_coverage: StatementCoverage | None = None
    statement_balances: StatementBalances | None = None
    candidates: tuple[PdfTransactionCandidate, ...] = Field(min_length=1)
    document_issues: tuple[ImportIssue, ...] = ()
    requires_user_confirmation: Literal[True] = True

    @model_validator(mode="after")
    def validate_pages_and_candidates(self) -> TextPdfPreview:
        """Require complete, ordered pages and in-range candidate locations."""
        page_numbers = tuple(page.page_number for page in self.pages)
        if self.page_count != len(self.pages) or page_numbers != tuple(
            range(1, self.page_count + 1)
        ):
            msg = "PDF preview pages must cover the complete document in order"
            raise ValueError(msg)
        if any(
            candidate.source_identity.page_number not in page_numbers
            for candidate in self.candidates
        ):
            msg = "PDF candidate references a page outside the preview"
            raise ValueError(msg)
        return self
