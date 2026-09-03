"""Public contracts for the local FastAPI boundary."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from cashflow_ai.schemas.accounts import AccountType
from cashflow_ai.schemas.imports import SourceType, VerificationStatus
from cashflow_ai.schemas.statements import ImportContext
from cashflow_ai.schemas.transactions import (
    CategoryId,
    Currency,
    Direction,
    FinancialRole,
    Identifier,
)


class _ApiContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class HealthResponse(_ApiContract):
    """Process liveness without database or OCR claims."""

    status: Literal["ok"] = "ok"
    version: str


class ReadinessResponse(_ApiContract):
    """Whether the local database connection and required schema are usable."""

    status: Literal["ready", "not_ready"]
    database_connection: bool
    database_schema: bool


class ApiValidationIssue(_ApiContract):
    """Data-minimised request-validation location and reason."""

    location: tuple[str, ...]
    error_type: str
    message: str


class ApiProblem(_ApiContract):
    """Stable privacy-safe error response."""

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    message: str = Field(min_length=1, max_length=500)
    page_numbers: tuple[int, ...] = ()
    validation_issues: tuple[ApiValidationIssue, ...] = ()


class Pagination(_ApiContract):
    """Bounded offset pagination accepted by collection endpoints."""

    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class Page[PageItemT](_ApiContract):
    """One stable collection slice and the size of the complete result."""

    items: tuple[PageItemT, ...]
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_slice(self) -> Page[PageItemT]:
        """Keep the returned slice within its declared limit and total."""
        if len(self.items) > self.limit or len(self.items) > max(
            self.total - self.offset, 0
        ):
            raise ValueError("page items exceed the declared result window")
        return self


class UserProfileCreate(_ApiContract):
    """Create the one local user profile."""

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    base_currency: Currency = Currency.GBP
    timezone: str = Field(default="UTC", min_length=1, max_length=100)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Require an IANA timezone without changing the supplied identifier."""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA name") from error
        return value


class UserProfileResponse(_ApiContract):
    """Local user profile returned without credentials or secrets."""

    profile_id: Identifier
    display_name: str | None
    base_currency: Currency
    timezone: str
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AccountCreate(_ApiContract):
    """Create a supported current/checking or savings account."""

    name: str = Field(min_length=1, max_length=100)
    account_type: AccountType
    currency: Currency = Currency.GBP
    institution_label: str | None = Field(default=None, min_length=1, max_length=100)


class AccountResponse(_ApiContract):
    """Local account metadata without bank credentials or account numbers."""

    account_id: Identifier
    user_profile_id: Identifier
    name: str
    account_type: AccountType
    currency: Currency
    institution_label: str | None
    is_active: bool
    created_at: AwareDatetime


class TransactionResponse(_ApiContract):
    """Verified transaction view that excludes the auditable raw payload."""

    transaction_id: Identifier
    account_id: Identifier
    transaction_date: date
    posting_date: date | None
    description: str
    merchant: str | None
    amount: Decimal
    balance_after: Decimal | None
    currency: Currency
    external_id: str | None
    transaction_type: str | None
    direction: Direction
    category_id: CategoryId | None
    financial_role: FinancialRole
    verified_at: AwareDatetime


class TransactionSearchRequest(_ApiContract):
    """Server-side filters for one profile's verified transaction history."""

    user_profile_id: Identifier
    account_ids: tuple[Identifier, ...] | None = Field(
        default=None, min_length=1, max_length=20
    )
    start_date: date | None = None
    end_date: date | None = None
    search_text: str | None = Field(default=None, min_length=1, max_length=100)
    category_ids: tuple[CategoryId, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )
    financial_roles: tuple[FinancialRole, ...] | None = Field(
        default=None, min_length=1, max_length=20
    )

    @model_validator(mode="after")
    def validate_filters(self) -> TransactionSearchRequest:
        """Require ordered dates and unique filter selections."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("transaction search end date cannot precede start date")
        for values in (self.account_ids, self.category_ids, self.financial_roles):
            if values is not None and len(values) != len(set(values)):
                raise ValueError("transaction search filters must be unique")
        return self


class ImportContextResponse(_ApiContract):
    """Stored statement context and coverage for one confirmed import."""

    import_batch_id: Identifier
    source_type: SourceType
    source_filename: str
    verification_status: VerificationStatus
    imported_at: AwareDatetime
    context: ImportContext


class OcrStatusResponse(_ApiContract):
    """Availability of the optional local-only Tesseract adapter."""

    engine: Literal["tesseract"] = "tesseract"
    execution: Literal["local_only"] = "local_only"
    available: bool
    message: str


class PdfSourceType(StrEnum):
    """PDF extraction paths accepted by review and confirmation routes."""

    DIGITAL_PDF = SourceType.DIGITAL_PDF.value
    OCR_PDF = SourceType.OCR_PDF.value


__all__ = [
    "AccountCreate",
    "AccountResponse",
    "ApiProblem",
    "ApiValidationIssue",
    "HealthResponse",
    "ImportContextResponse",
    "OcrStatusResponse",
    "Page",
    "Pagination",
    "PdfSourceType",
    "ReadinessResponse",
    "TransactionResponse",
    "TransactionSearchRequest",
    "UserProfileCreate",
    "UserProfileResponse",
]
