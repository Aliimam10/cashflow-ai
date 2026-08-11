"""Contracts for non-persistent OCR statement extraction previews."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from cashflow_ai.schemas.imports import (
    Confidence,
    ExtractionMethod,
    ExtractionProvenance,
    FieldConfidence,
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


class _OcrContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class OcrLineExtraction(_OcrContract):
    """One ordered OCR text line and its aggregate word confidence."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )

    line_number: PositiveInt
    raw_text: str = Field(min_length=1)
    confidence: Confidence
    word_count: PositiveInt


class OcrPageExtraction(_OcrContract):
    """Rendered-page metadata and raw locally recognised text."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )

    page_number: PositiveInt
    pixel_width: PositiveInt
    pixel_height: PositiveInt
    render_dpi: PositiveInt
    rotation_applied_degrees: Literal[0, 90, 180, 270]
    orientation_confidence: Confidence | None = None
    threshold_applied: bool
    raw_text: str = Field(min_length=1)
    confidence: Confidence
    lines: tuple[OcrLineExtraction, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ordered_raw_lines(self) -> OcrPageExtraction:
        """Require a complete ordered line sequence matching the raw text."""
        line_numbers = tuple(line.line_number for line in self.lines)
        if line_numbers != tuple(range(1, len(self.lines) + 1)):
            msg = "OCR page lines must be numbered consecutively"
            raise ValueError(msg)
        if self.raw_text != "\n".join(line.raw_text for line in self.lines):
            msg = "OCR page raw text must preserve its ordered lines"
            raise ValueError(msg)
        return self


class OcrTransactionCandidate(_OcrContract):
    """One OCR-derived transaction awaiting explicit user review."""

    original: OriginalTransactionValues
    draft: TransactionDraft
    source_identity: SourceRecordIdentity
    source_fingerprint: Sha256Digest
    canonical_fingerprint: Sha256Digest | None = None
    provenance: ExtractionProvenance
    line_numbers: tuple[PositiveInt, ...] = Field(min_length=1)
    field_confidences: tuple[FieldConfidence, ...] = ()
    issues: tuple[ImportIssue, ...] = ()
    review_status: Literal[ReviewStatus.NEEDS_REVIEW] = ReviewStatus.NEEDS_REVIEW

    @model_validator(mode="after")
    def validate_ocr_lineage_and_result(self) -> OcrTransactionCandidate:
        """Keep OCR lineage aligned and invalid candidates explainable."""
        if (
            self.source_identity.source_type is not SourceType.OCR_PDF
            or self.provenance.source_type is not SourceType.OCR_PDF
            or self.provenance.method is not ExtractionMethod.OCR
        ):
            msg = "OCR candidates require OCR-PDF lineage"
            raise ValueError(msg)
        if self.provenance.page_number != self.source_identity.page_number:
            msg = "OCR candidate provenance must match its source page"
            raise ValueError(msg)
        if tuple(sorted(set(self.line_numbers))) != self.line_numbers:
            msg = "OCR candidate line numbers must be unique and ordered"
            raise ValueError(msg)
        confidence_fields = tuple(item.field for item in self.field_confidences)
        if len(confidence_fields) != len(set(confidence_fields)):
            msg = "OCR field confidence entries must be unique"
            raise ValueError(msg)
        if self.canonical_fingerprint is None and not self.issues:
            msg = "non-canonical OCR candidates require a review issue"
            raise ValueError(msg)
        return self


class OcrPdfPreview(_OcrContract):
    """Review-only result for one locally processed scanned PDF."""

    source_filename: str = Field(min_length=1, max_length=255)
    byte_size: PositiveInt
    file_hash: Sha256Digest
    page_count: PositiveInt
    pages: tuple[OcrPageExtraction, ...] = Field(min_length=1)
    statement_coverage: StatementCoverage | None = None
    statement_balances: StatementBalances | None = None
    candidates: tuple[OcrTransactionCandidate, ...] = Field(min_length=1)
    document_issues: tuple[ImportIssue, ...] = ()
    requires_user_confirmation: Literal[True] = True
    temporary_artifacts_retained: Literal[False] = False

    @model_validator(mode="after")
    def validate_pages_and_candidate_lines(self) -> OcrPdfPreview:
        """Require complete pages and candidate references within those pages."""
        page_numbers = tuple(page.page_number for page in self.pages)
        if self.page_count != len(self.pages) or page_numbers != tuple(
            range(1, self.page_count + 1)
        ):
            msg = "OCR preview pages must cover the complete document in order"
            raise ValueError(msg)

        lines_by_page = {
            page.page_number: {line.line_number for line in page.lines}
            for page in self.pages
        }
        for candidate in self.candidates:
            page_number = candidate.source_identity.page_number
            if page_number not in lines_by_page or not set(
                candidate.line_numbers
            ).issubset(lines_by_page[page_number]):
                msg = "OCR candidate references a line outside the preview"
                raise ValueError(msg)
        return self
