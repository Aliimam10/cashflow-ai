"""Typed HTTP contracts for review-gated digital-PDF imports."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from cashflow_ai.schemas.normalisation import Sha256Digest
from cashflow_ai.schemas.reconciliation import StatementReview


class _PdfApiContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class DigitalPdfReviewState(StrEnum):
    """Safe result states returned by digital-PDF review."""

    READY = "ready"
    MAPPING_REQUIRED = "mapping_required"
    UNSUPPORTED_LAYOUT = "unsupported_layout"


class DigitalPdfColumnRole(StrEnum):
    """Canonical roles a user may assign to reconstructed PDF columns."""

    TRANSACTION_DATE = "transaction_date"
    DESCRIPTION = "description"
    SIGNED_AMOUNT = "signed_amount"
    DEBIT_AMOUNT = "debit_amount"
    CREDIT_AMOUNT = "credit_amount"
    RUNNING_BALANCE = "running_balance"


class DigitalPdfColumn(_PdfApiContract):
    """One reconstructed column offered for explicit user mapping."""

    column_id: str = Field(min_length=1, max_length=100)
    header_text: str = Field(min_length=1, max_length=500)
    role_hint: DigitalPdfColumnRole | None = None


class DigitalPdfMappingRow(_PdfApiContract):
    """One bounded preview row aligned with the returned column order."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )

    page_number: PositiveInt
    page_record_number: PositiveInt
    values: tuple[str, ...]


class DigitalPdfMappingPreview(_PdfApiContract):
    """Bounded spatial-table evidence used only to choose column roles."""

    file_hash: Sha256Digest
    structure_digest: Sha256Digest
    page_count: PositiveInt
    columns: tuple[DigitalPdfColumn, ...] = Field(min_length=3)
    sample_rows: tuple[DigitalPdfMappingRow, ...] = Field(min_length=1)
    total_rows: PositiveInt
    truncated: bool

    @model_validator(mode="after")
    def validate_shape(self) -> DigitalPdfMappingPreview:
        """Keep sample values aligned and truncation claims internally coherent."""
        if any(len(row.values) != len(self.columns) for row in self.sample_rows):
            raise ValueError("PDF mapping rows must align with the returned columns")
        if len(self.sample_rows) > self.total_rows:
            raise ValueError("PDF mapping samples cannot exceed the total row count")
        if self.truncated != (len(self.sample_rows) < self.total_rows):
            raise ValueError("PDF mapping truncation must match the sampled row count")
        return self


class DigitalPdfColumnMapping(_PdfApiContract):
    """File-bound user mapping for one spatially reconstructed PDF table."""

    file_hash: Sha256Digest
    structure_digest: Sha256Digest
    transaction_date: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=100)
    signed_amount: str | None = Field(default=None, min_length=1, max_length=100)
    debit_amount: str | None = Field(default=None, min_length=1, max_length=100)
    credit_amount: str | None = Field(default=None, min_length=1, max_length=100)
    running_balance: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_mapping(self) -> DigitalPdfColumnMapping:
        """Require unique columns and exactly one supported amount layout."""
        separate_amounts = (
            self.debit_amount is not None or self.credit_amount is not None
        )
        if separate_amounts and (
            self.debit_amount is None or self.credit_amount is None
        ):
            raise ValueError("debit and credit columns must be mapped together")
        if (self.signed_amount is None) == (not separate_amounts):
            raise ValueError(
                "map either one signed amount or separate debit and credit columns"
            )
        selected = tuple(
            value
            for value in (
                self.transaction_date,
                self.description,
                self.signed_amount,
                self.debit_amount,
                self.credit_amount,
                self.running_balance,
            )
            if value is not None
        )
        if len(selected) != len(set(selected)):
            raise ValueError("each PDF column can have only one mapped role")
        return self


class DigitalPdfReviewResult(_PdfApiContract):
    """Discriminated outcome from stateless digital-PDF review."""

    state: DigitalPdfReviewState
    file_hash: Sha256Digest
    reason_code: str = Field(min_length=1, max_length=100)
    guidance: str = Field(min_length=1, max_length=500)
    review: StatementReview | None = None
    mapping_preview: DigitalPdfMappingPreview | None = None
    recommended_format: Literal["csv"] | None = None

    @model_validator(mode="after")
    def validate_state_payload(self) -> DigitalPdfReviewResult:
        """Require exactly the payload appropriate for the declared state."""
        if self.state is DigitalPdfReviewState.READY:
            if self.review is None or self.mapping_preview is not None:
                raise ValueError("ready PDF review requires review data only")
            if self.review.file_hash != self.file_hash:
                raise ValueError("ready PDF review must match the exact file hash")
            if self.recommended_format is not None:
                raise ValueError("ready PDF review cannot recommend a fallback")
        elif self.state is DigitalPdfReviewState.MAPPING_REQUIRED:
            if self.review is not None or self.mapping_preview is None:
                raise ValueError(
                    "mapping-required PDF review requires mapping evidence"
                )
            if self.mapping_preview.file_hash != self.file_hash:
                raise ValueError("PDF mapping evidence must match the exact file hash")
            if self.recommended_format is not None:
                raise ValueError(
                    "mapping-required PDF review cannot recommend fallback"
                )
        elif (
            self.review is not None
            or self.mapping_preview is not None
            or self.recommended_format != "csv"
        ):
            raise ValueError("unsupported PDF review must recommend CSV only")
        return self


__all__ = [
    "DigitalPdfColumn",
    "DigitalPdfColumnMapping",
    "DigitalPdfColumnRole",
    "DigitalPdfMappingPreview",
    "DigitalPdfMappingRow",
    "DigitalPdfReviewResult",
    "DigitalPdfReviewState",
]
