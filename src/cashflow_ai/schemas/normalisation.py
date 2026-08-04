"""Preserved-source and normalised-transaction contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from cashflow_ai.schemas.imports import ParserIdentity, SourceType
from cashflow_ai.schemas.transactions import TransactionDraft

Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SourceFieldValue(BaseModel):
    """One unmodified heading and value from a source record."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    column: str = Field(min_length=1, max_length=255)
    value: str


class OriginalTransactionValues(BaseModel):
    """Mapped transaction text retained exactly as supplied by the source."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    transaction_date_text: str
    description_text: str
    signed_amount_text: str | None = None
    debit_amount_text: str | None = None
    credit_amount_text: str | None = None
    posting_date_text: str | None = None
    running_balance_text: str | None = None
    currency_text: str | None = None
    external_id_text: str | None = None
    transaction_type_text: str | None = None
    raw_fields: tuple[SourceFieldValue, ...] = Field(min_length=1)


class SourceRecordIdentity(BaseModel):
    """Immutable document and row/page location for one extracted record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: SourceType
    source_document_hash: Sha256Digest
    source_row_number: PositiveInt | None = None
    page_number: PositiveInt | None = None
    page_record_number: PositiveInt | None = None

    @model_validator(mode="after")
    def validate_source_location(self) -> SourceRecordIdentity:
        """Require a CSV row or an unambiguous record position on a PDF page."""
        if self.source_type is SourceType.CSV:
            if self.source_row_number is None:
                msg = "CSV source identity requires a row number"
                raise ValueError(msg)
            if self.page_number is not None or self.page_record_number is not None:
                msg = "CSV source identity cannot contain PDF page location"
                raise ValueError(msg)
        elif (
            self.source_row_number is not None
            or self.page_number is None
            or self.page_record_number is None
        ):
            msg = "PDF source identity requires page and page-record numbers"
            raise ValueError(msg)
        return self


class CalendarFeatures(BaseModel):
    """Deterministic calendar attributes derived from a transaction date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    year: int = Field(ge=1900, le=9999)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    weekday: int = Field(ge=0, le=6, description="Monday is 0 and Sunday is 6")
    iso_week: PositiveInt = Field(le=53)
    is_weekend: bool


class NormalisedTransaction(BaseModel):
    """Cleaned draft plus preserved source, lineage, and matching identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    original: OriginalTransactionValues
    draft: TransactionDraft
    calendar: CalendarFeatures
    parser: ParserIdentity
    source_identity: SourceRecordIdentity
    source_fingerprint: Sha256Digest
    canonical_fingerprint: Sha256Digest

    @model_validator(mode="after")
    def require_matching_fields(self) -> NormalisedTransaction:
        """Ensure a normalised record contains every duplicate-matching field."""
        required = (
            self.draft.transaction_date,
            self.draft.description,
            self.draft.merchant,
            self.draft.amount,
            self.draft.currency,
            self.draft.account_id,
            self.draft.direction,
        )
        if any(value is None for value in required):
            msg = "normalised transaction draft is missing a required cleaned field"
            raise ValueError(msg)
        return self


class NormalisationErrorCode(StrEnum):
    """Stable reasons why a mapped transaction cannot be normalised."""

    MISSING_VALUE = "missing_value"
    INVALID_DATE = "invalid_date"
    INVALID_AMOUNT = "invalid_amount"
    CONFLICTING_AMOUNTS = "conflicting_amounts"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    INVALID_ROW = "invalid_row"
