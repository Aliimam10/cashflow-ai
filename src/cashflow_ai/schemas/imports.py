"""Statement document, extraction, provenance, and review contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    model_validator,
)

from cashflow_ai.schemas.transactions import TransactionDraft

Confidence = Annotated[float, Field(ge=0, le=1)]


class SourceType(StrEnum):
    """Supported Version 1 statement source types."""

    CSV = "csv"
    DIGITAL_PDF = "digital_pdf"
    OCR_PDF = "ocr_pdf"


class ExtractionMethod(StrEnum):
    """Method used to obtain a provisional transaction row."""

    CSV_ROW = "csv_row"
    PDF_TEXT = "pdf_text"
    PDF_TABLE = "pdf_table"
    OCR = "ocr"


class ReviewStatus(StrEnum):
    """User-review state for an extracted candidate."""

    PENDING = "pending"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class VerificationStatus(StrEnum):
    """Trust state of an imported document or derived record."""

    UNVERIFIED = "unverified"
    NEEDS_REVIEW = "needs_review"
    VERIFIED = "verified"
    REJECTED = "rejected"


class IssueSeverity(StrEnum):
    """Severity of an extraction or validation issue."""

    WARNING = "warning"
    ERROR = "error"


class TransactionField(StrEnum):
    """Fields that extraction confidence or issues can reference."""

    TRANSACTION_DATE = "transaction_date"
    POSTING_DATE = "posting_date"
    DESCRIPTION = "description"
    MERCHANT = "merchant"
    AMOUNT = "amount"
    BALANCE_AFTER = "balance_after"
    CURRENCY = "currency"
    TRANSACTION_TYPE = "transaction_type"
    DIRECTION = "direction"


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class SourceRegion(_ContractModel):
    """Bounding box in PDF page coordinates."""

    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class ParserIdentity(_ContractModel):
    """Named and versioned parser used to produce extracted values."""

    name: str = Field(min_length=1, max_length=100)
    version: str = Field(
        min_length=1,
        max_length=50,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]*$",
    )


class ExtractionProvenance(_ContractModel):
    """Where and how a provisional row was extracted."""

    source_type: SourceType
    method: ExtractionMethod
    page_number: PositiveInt | None = None
    region: SourceRegion | None = None
    confidence: Confidence | None = None
    parser: ParserIdentity | None = None

    @model_validator(mode="after")
    def validate_source_method(self) -> ExtractionProvenance:
        """Require extraction metadata appropriate to its source type."""
        allowed_methods = {
            SourceType.CSV: {ExtractionMethod.CSV_ROW},
            SourceType.DIGITAL_PDF: {
                ExtractionMethod.PDF_TEXT,
                ExtractionMethod.PDF_TABLE,
            },
            SourceType.OCR_PDF: {ExtractionMethod.OCR},
        }
        if self.method not in allowed_methods[self.source_type]:
            msg = "extraction method does not match the statement source type"
            raise ValueError(msg)
        if self.source_type is SourceType.CSV and (
            self.page_number is not None or self.region is not None
        ):
            msg = "CSV provenance cannot contain PDF page coordinates"
            raise ValueError(msg)
        if self.source_type is not SourceType.CSV and self.page_number is None:
            msg = "PDF provenance requires a page number"
            raise ValueError(msg)
        if self.source_type is SourceType.OCR_PDF and self.confidence is None:
            msg = "OCR provenance requires recognition confidence"
            raise ValueError(msg)
        return self


class FieldConfidence(_ContractModel):
    """Recognition confidence for one provisional transaction field."""

    field: TransactionField
    confidence: Confidence
    raw_value: str | None = None


class ImportIssue(_ContractModel):
    """Stable extraction or validation warning presented for review."""

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    message: str = Field(min_length=1, max_length=500)
    severity: IssueSeverity
    field: TransactionField | None = None


class ImportDocument(_ContractModel):
    """Metadata for one uploaded statement document."""

    source_document_id: UUID
    import_batch_id: UUID
    source_type: SourceType
    source_filename: str = Field(min_length=1, max_length=255)
    file_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str = Field(min_length=1, max_length=100)
    byte_size: PositiveInt
    uploaded_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED

    @model_validator(mode="after")
    def validate_mime_type(self) -> ImportDocument:
        """Require a detected MIME type compatible with the source type."""
        if self.source_type is SourceType.CSV:
            allowed = {"text/csv", "application/csv", "text/plain"}
            if self.mime_type not in allowed:
                msg = "CSV source has an unsupported MIME type"
                raise ValueError(msg)
        elif self.mime_type != "application/pdf":
            msg = "PDF source must use the application/pdf MIME type"
            raise ValueError(msg)
        return self


class ImportCandidate(_ContractModel):
    """One preserved provisional row awaiting validation and user review."""

    candidate_id: UUID
    source_document_id: UUID
    import_batch_id: UUID
    source_row_number: PositiveInt | None = None
    raw_payload: dict[str, JsonValue] = Field(min_length=1)
    provenance: ExtractionProvenance
    draft: TransactionDraft
    field_confidences: tuple[FieldConfidence, ...] = ()
    issues: tuple[ImportIssue, ...] = ()
    review_status: ReviewStatus = ReviewStatus.PENDING
    user_confirmed: bool = False
    reviewed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_review_and_source(self) -> ImportCandidate:
        """Require source identity, unique confidence, and valid review state."""
        if (
            self.provenance.source_type is SourceType.CSV
            and self.source_row_number is None
        ):
            msg = "CSV candidates require a source row number"
            raise ValueError(msg)
        fields = [item.field for item in self.field_confidences]
        if len(fields) != len(set(fields)):
            msg = "field confidence entries must be unique"
            raise ValueError(msg)

        if self.review_status is ReviewStatus.CONFIRMED:
            if not self.user_confirmed or self.reviewed_at is None:
                msg = "confirmed candidates require user confirmation and review time"
                raise ValueError(msg)
        elif self.user_confirmed or self.reviewed_at is not None:
            msg = "confirmation metadata is only valid for confirmed candidates"
            raise ValueError(msg)
        return self
