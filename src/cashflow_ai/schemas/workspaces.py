"""Typed contracts for the review-gated statement workspace."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    model_validator,
)

from cashflow_ai.schemas.csv_imports import CsvColumnMapping
from cashflow_ai.schemas.normalisation import Sha256Digest
from cashflow_ai.schemas.pdf_api import DigitalPdfColumnMapping
from cashflow_ai.schemas.statements import CoverageStatus, DateRange
from cashflow_ai.schemas.transactions import (
    CategoryId,
    Currency,
    FinancialRole,
    Identifier,
)

WorkspaceText = Annotated[str, Field(min_length=1, max_length=500)]
WorkspaceMappingCell = Annotated[str, Field(max_length=1_000)]
WorkspaceMappingRow = Annotated[tuple[WorkspaceMappingCell, ...], Field(max_length=20)]
_POSITIVE_ROLES = {
    FinancialRole.INCOME,
    FinancialRole.REFUND,
    FinancialRole.REIMBURSEMENT,
    FinancialRole.TRANSFER_IN,
}
_NEGATIVE_ROLES = {
    FinancialRole.EXPENSE,
    FinancialRole.TRANSFER_OUT,
    FinancialRole.CASH_WITHDRAWAL,
}


def _role_matches_amount(amount: Decimal, role: FinancialRole) -> bool:
    if amount > 0:
        return role not in _NEGATIVE_ROLES
    return role not in _POSITIVE_ROLES


class WorkspaceRetentionMode(StrEnum):
    """How long approved canonical rows should remain available."""

    SAVED = "saved"
    TEMPORARY = "temporary"


class WorkspaceStatus(StrEnum):
    """Lifecycle of one isolated statement workspace."""

    DRAFT = "draft"
    FINALIZED = "finalized"


class WorkspaceSourceType(StrEnum):
    """Detected upload format, including safely rejected unknown files."""

    CSV = "csv"
    DIGITAL_PDF = "digital_pdf"
    UNSUPPORTED = "unsupported"


class WorkspaceSourceReviewState(StrEnum):
    """Whether one uploaded source can contribute editable rows."""

    READY = "ready"
    MAPPING_REQUIRED = "mapping_required"
    UNSUPPORTED = "unsupported"


class WorkspaceRowReviewState(StrEnum):
    """Explicit review state of one editable candidate row."""

    READY = "ready"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class WorkspaceDuplicateDecision(StrEnum):
    """User decision for a probable cross-file duplicate."""

    KEEP = "keep"
    REJECT = "reject"


class _WorkspaceContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class WorkspaceCreateRequest(_WorkspaceContract):
    """Create an empty, isolated GBP statement workspace."""

    retention_mode: WorkspaceRetentionMode = WorkspaceRetentionMode.SAVED
    account_name: str = Field(
        default="My account", min_length=1, max_length=100, repr=False
    )
    currency: Literal[Currency.GBP] = Currency.GBP


class WorkspaceFileMapping(_WorkspaceContract):
    """Exact-file-bound mapping for one source in a combined upload."""

    file_hash: Sha256Digest
    source_type: WorkspaceSourceType
    csv_mapping: CsvColumnMapping | None = None
    pdf_mapping: DigitalPdfColumnMapping | None = None

    @model_validator(mode="after")
    def validate_mapping_kind(self) -> WorkspaceFileMapping:
        """Require exactly the mapping appropriate for the source type."""
        if self.source_type is WorkspaceSourceType.CSV:
            if self.csv_mapping is None or self.pdf_mapping is not None:
                raise ValueError("CSV sources require only a CSV column mapping")
        elif self.source_type is WorkspaceSourceType.DIGITAL_PDF:
            if self.pdf_mapping is None or self.csv_mapping is not None:
                raise ValueError("digital PDFs require only a PDF column mapping")
        else:
            raise ValueError("unsupported files cannot receive a column mapping")
        if (
            self.pdf_mapping is not None
            and self.pdf_mapping.file_hash != self.file_hash
        ):
            raise ValueError("PDF mapping must match the exact workspace file")
        return self


class WorkspaceSourceFile(_WorkspaceContract):
    """Bounded, non-persistent review summary for one uploaded statement."""

    source_id: Identifier
    source_type: WorkspaceSourceType
    display_name: str = Field(min_length=1, max_length=255, repr=False)
    file_hash: Sha256Digest = Field(repr=False)
    state: WorkspaceSourceReviewState
    reason_code: str = Field(min_length=1, max_length=100)
    guidance: str = Field(min_length=1, max_length=500)
    row_count: int = Field(ge=0)
    parser_name: str | None = Field(default=None, min_length=1, max_length=100)
    parser_version: str | None = Field(default=None, min_length=1, max_length=50)
    layout_version: str | None = Field(default=None, min_length=1, max_length=100)
    warning_codes: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = ()
    excluded_transaction_rows: int = Field(default=0, ge=0)
    page_count: PositiveInt | None = None
    mapping_structure_digest: Sha256Digest | None = None
    mapping_columns: tuple[Annotated[str, Field(min_length=1, max_length=255)], ...] = (
        Field(default=(), max_length=20)
    )
    mapping_sample_rows: tuple[WorkspaceMappingRow, ...] = Field(
        default=(), max_length=20, repr=False
    )
    suggested_period: DateRange | None = None

    @model_validator(mode="after")
    def validate_state_payload(self) -> WorkspaceSourceFile:
        """Expose mapping evidence only when an explicit mapping is required."""
        if (
            self.source_type is WorkspaceSourceType.UNSUPPORTED
            and self.state is not WorkspaceSourceReviewState.UNSUPPORTED
        ):
            raise ValueError("unknown file types must remain unsupported")
        has_mapping = bool(self.mapping_columns or self.mapping_sample_rows)
        if self.state is WorkspaceSourceReviewState.MAPPING_REQUIRED:
            if not self.mapping_columns or not self.mapping_sample_rows:
                raise ValueError("mapping-required sources need bounded evidence")
            if any(
                len(row) != len(self.mapping_columns)
                for row in self.mapping_sample_rows
            ):
                raise ValueError("mapping sample rows must align with columns")
            if (
                self.source_type is WorkspaceSourceType.DIGITAL_PDF
                and self.mapping_structure_digest is None
            ):
                raise ValueError("PDF mappings require a structure digest")
        elif has_mapping:
            raise ValueError(
                "mapping evidence belongs only to mapping-required sources"
            )
        elif self.mapping_structure_digest is not None:
            raise ValueError(
                "structure digests belong only to mapping-required sources"
            )
        if self.state is WorkspaceSourceReviewState.READY and self.row_count < 1:
            raise ValueError("ready sources must contribute at least one row")
        if self.state is not WorkspaceSourceReviewState.READY and self.row_count:
            raise ValueError("unready sources cannot claim editable rows")
        return self


class WorkspaceTransactionRow(_WorkspaceContract):
    """One spreadsheet-editable candidate without persisted source payloads."""

    row_id: Identifier
    source_id: Identifier | None = None
    source_type: WorkspaceSourceType | None = None
    source_record_number: PositiveInt | None = None
    page_number: PositiveInt | None = None
    transaction_date: date | None = None
    posting_date: date | None = None
    description: str | None = Field(default=None, max_length=500, repr=False)
    merchant: str | None = Field(default=None, max_length=500, repr=False)
    amount: Decimal | None = Field(
        default=None, max_digits=18, decimal_places=2, repr=False
    )
    balance_after: Decimal | None = Field(
        default=None, max_digits=18, decimal_places=2, repr=False
    )
    currency: Literal[Currency.GBP] = Currency.GBP
    category_id: CategoryId = "other"
    financial_role: FinancialRole = FinancialRole.UNKNOWN
    external_id: Identifier | None = Field(default=None, repr=False)
    transaction_type: Identifier | None = Field(default=None, repr=False)
    review_state: WorkspaceRowReviewState
    probable_duplicate_of: Identifier | None = None
    issue_codes: tuple[str, ...] = ()
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_review_state(self) -> WorkspaceTransactionRow:
        """Require finalized-looking values only for usable rows."""
        if self.source_type is WorkspaceSourceType.UNSUPPORTED:
            raise ValueError("unsupported sources cannot contribute transaction rows")
        if (self.source_id is None) != (self.source_type is None):
            raise ValueError(
                "workspace row source identity and type must stay together"
            )
        if self.source_id is None and (
            self.source_record_number is not None or self.page_number is not None
        ):
            raise ValueError("detached workspace rows cannot retain source coordinates")
        complete = (
            self.transaction_date is not None
            and bool(self.description and self.description.strip())
            and self.amount is not None
            and self.amount != 0
        )
        if (
            self.review_state
            in {
                WorkspaceRowReviewState.READY,
                WorkspaceRowReviewState.CONFIRMED,
            }
            and not complete
        ):
            raise ValueError(
                "usable workspace rows require date, description and amount"
            )
        if (
            self.review_state is WorkspaceRowReviewState.CONFIRMED
            and self.amount is not None
            and not _role_matches_amount(self.amount, self.financial_role)
        ):
            raise ValueError("financial role must match the confirmed amount sign")
        if self.review_state is WorkspaceRowReviewState.REJECTED:
            if self.probable_duplicate_of is not None:
                raise ValueError("rejected rows cannot retain a duplicate decision")
        elif self.probable_duplicate_of is not None and not self.issue_codes:
            raise ValueError("probable duplicates require a visible issue code")
        return self


class WorkspaceRowRevision(_WorkspaceContract):
    """User-authoritative editable fields for one existing server row."""

    row_id: Identifier
    expected_revision: int = Field(ge=1)
    transaction_date: date | None = None
    description: WorkspaceText | None = Field(default=None, repr=False)
    amount: Decimal | None = Field(
        default=None, max_digits=18, decimal_places=2, repr=False
    )
    balance_after: Decimal | None = Field(
        default=None, max_digits=18, decimal_places=2, repr=False
    )
    category_id: CategoryId = "other"
    financial_role: FinancialRole = FinancialRole.UNKNOWN
    review_state: Literal[
        WorkspaceRowReviewState.CONFIRMED,
        WorkspaceRowReviewState.REJECTED,
    ]
    duplicate_decision: WorkspaceDuplicateDecision | None = None

    @model_validator(mode="after")
    def validate_usable_revision(self) -> WorkspaceRowRevision:
        """Reject zero values and inconsistent probable-duplicate actions."""
        if self.review_state is WorkspaceRowReviewState.CONFIRMED and (
            self.transaction_date is None
            or self.description is None
            or self.amount is None
            or self.amount == 0
        ):
            raise ValueError(
                "confirmed workspace rows require date, description and non-zero amount"
            )
        if (
            self.review_state is WorkspaceRowReviewState.CONFIRMED
            and self.amount is not None
            and not _role_matches_amount(self.amount, self.financial_role)
        ):
            raise ValueError("financial role must match the confirmed amount sign")
        if (
            self.review_state is WorkspaceRowReviewState.REJECTED
            and self.duplicate_decision is WorkspaceDuplicateDecision.KEEP
        ):
            raise ValueError("a kept duplicate row cannot be rejected")
        return self


class WorkspaceEditRequest(_WorkspaceContract):
    """Optimistic, atomic edits to the combined transaction table."""

    expected_workspace_revision: int = Field(ge=1)
    rows: tuple[WorkspaceRowRevision, ...] = Field(min_length=1, repr=False)

    @model_validator(mode="after")
    def validate_unique_rows(self) -> WorkspaceEditRequest:
        """Reject ambiguous requests that revise one row more than once."""
        if len({row.row_id for row in self.rows}) != len(self.rows):
            raise ValueError("each workspace row may be edited only once per request")
        return self


class WorkspaceSourceRemoveRequest(_WorkspaceContract):
    """Optimistic confirmation to discard one draft source and its rows."""

    expected_workspace_revision: int = Field(ge=1)


class WorkspaceCoverageConfirmation(_WorkspaceContract):
    """User-confirmed combined statement coverage."""

    start_date: date
    end_date: date
    status: CoverageStatus
    missing_periods: tuple[DateRange, ...] = ()
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_coverage(self) -> WorkspaceCoverageConfirmation:
        """Keep explicit missing periods inside a valid coverage range."""
        if self.end_date < self.start_date:
            raise ValueError("workspace coverage end cannot precede its start")
        if any(
            gap.start_date < self.start_date or gap.end_date > self.end_date
            for gap in self.missing_periods
        ):
            raise ValueError("workspace missing periods must remain inside coverage")
        if self.status is CoverageStatus.GAPPED and not self.missing_periods:
            raise ValueError("gapped workspace coverage requires missing periods")
        if self.status is not CoverageStatus.GAPPED and self.missing_periods:
            raise ValueError("missing periods require gapped workspace coverage")
        for previous, current in zip(
            self.missing_periods,
            self.missing_periods[1:],
            strict=False,
        ):
            if current.start_date <= previous.end_date:
                raise ValueError(
                    "workspace missing periods must be chronological and "
                    "non-overlapping"
                )
        return self


class WorkspaceBalanceConfirmation(_WorkspaceContract):
    """Optional latest balance anchoring workspace forecasts."""

    balance: Decimal = Field(max_digits=18, decimal_places=2, repr=False)
    as_of_date: date
    currency: Literal[Currency.GBP] = Currency.GBP
    confirmed: Literal[True]


class WorkspaceFinalizeRequest(_WorkspaceContract):
    """Explicit approval required before any downstream result is available."""

    expected_workspace_revision: int = Field(ge=1)
    statement_confirmed: Literal[True]
    date_interpretation_confirmed: Literal[True]
    sign_convention_confirmed: Literal[True]
    source_exclusions_confirmed: bool = False
    coverage: WorkspaceCoverageConfirmation
    balance: WorkspaceBalanceConfirmation | None = Field(default=None, repr=False)


class StatementWorkspace(_WorkspaceContract):
    """Client-visible workspace state without uploaded file bytes."""

    workspace_id: Identifier
    retention_mode: WorkspaceRetentionMode
    status: WorkspaceStatus
    account_name: str = Field(min_length=1, max_length=100, repr=False)
    currency: Literal[Currency.GBP] = Currency.GBP
    revision: int = Field(ge=1)
    sources: tuple[WorkspaceSourceFile, ...] = Field(default=(), repr=False)
    rows: tuple[WorkspaceTransactionRow, ...] = Field(default=(), repr=False)
    coverage: WorkspaceCoverageConfirmation | None = None
    balance: WorkspaceBalanceConfirmation | None = Field(default=None, repr=False)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    finalized_at: AwareDatetime | None = None

    @property
    def unresolved_row_count(self) -> int:
        """Count rows that cannot yet enter the finalized canonical table."""
        return sum(
            row.review_state
            not in {
                WorkspaceRowReviewState.CONFIRMED,
                WorkspaceRowReviewState.REJECTED,
            }
            or row.probable_duplicate_of is not None
            for row in self.rows
        )

    @model_validator(mode="after")
    def validate_lifecycle(self) -> StatementWorkspace:
        """Keep draft and finalized evidence consistent with lifecycle state."""
        timestamps = tuple(
            value
            for value in (self.created_at, self.updated_at, self.finalized_at)
            if value is not None
        )
        if any(value.utcoffset() != timedelta(0) for value in timestamps):
            raise ValueError("workspace timestamps must use UTC")
        if self.updated_at < self.created_at:
            raise ValueError("workspace updates cannot precede creation")
        if self.status is WorkspaceStatus.FINALIZED:
            if self.finalized_at is None or self.coverage is None:
                raise ValueError("finalized workspaces require time and coverage")
            if self.finalized_at < self.updated_at:
                raise ValueError(
                    "workspace finalization cannot precede its last update"
                )
            if self.unresolved_row_count:
                raise ValueError("finalized workspaces cannot contain unresolved rows")
            if self.sources or any(
                row.review_state is not WorkspaceRowReviewState.CONFIRMED
                or row.source_id is not None
                for row in self.rows
            ):
                raise ValueError(
                    "finalized workspaces contain only detached confirmed rows"
                )
        elif (
            self.finalized_at is not None
            or self.coverage is not None
            or self.balance is not None
        ):
            raise ValueError("draft workspaces cannot claim finalization evidence")
        return self


class WorkspaceImportReview(_WorkspaceContract):
    """Combined result after reviewing all supplied files."""

    workspace: StatementWorkspace
    accepted_files: int = Field(ge=0)
    mapping_required_files: int = Field(ge=0)
    unsupported_files: int = Field(ge=0)
    exact_duplicates_removed: int = Field(ge=0)
    probable_duplicates: int = Field(ge=0)


class WorkspaceFinalizeResult(_WorkspaceContract):
    """Successful approval result for a canonical workspace."""

    workspace: StatementWorkspace
    included_rows: int = Field(ge=1)
    rejected_rows: int = Field(ge=0)
    persisted: bool

    @model_validator(mode="after")
    def validate_finalized_projection(self) -> WorkspaceFinalizeResult:
        """Keep result metadata tied to its canonical finalized workspace."""
        should_persist = self.workspace.retention_mode is WorkspaceRetentionMode.SAVED
        if (
            self.workspace.status is not WorkspaceStatus.FINALIZED
            or self.included_rows != len(self.workspace.rows)
            or self.persisted is not should_persist
        ):
            raise ValueError("finalization result does not match its workspace")
        return self


class WorkspaceDeleteRequest(_WorkspaceContract):
    """Explicit destructive confirmation for one local workspace."""

    confirmed: Literal[True]


class WorkspaceDeleteResult(_WorkspaceContract):
    """Data-minimised acknowledgement of workspace deletion."""

    workspace_id: Identifier
    deleted: Literal[True]


class WorkspaceDeleteAllRequest(_WorkspaceContract):
    """Explicit destructive confirmation for every statement workspace."""

    confirmed: Literal[True]


class WorkspaceDeleteAllResult(_WorkspaceContract):
    """Acknowledge a global workspace-data erasure without returning identities."""

    active_workspaces_deleted: int = Field(ge=0)
    saved_workspaces_deleted: int = Field(ge=0)
    deleted: Literal[True]


class WorkspaceCsvDownload(_WorkspaceContract):
    """In-memory canonical CSV export returned without a filesystem write."""

    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(repr=False)
    row_count: int = Field(ge=1)


__all__ = [
    "StatementWorkspace",
    "WorkspaceBalanceConfirmation",
    "WorkspaceCoverageConfirmation",
    "WorkspaceCreateRequest",
    "WorkspaceCsvDownload",
    "WorkspaceDeleteAllRequest",
    "WorkspaceDeleteAllResult",
    "WorkspaceDeleteRequest",
    "WorkspaceDeleteResult",
    "WorkspaceDuplicateDecision",
    "WorkspaceEditRequest",
    "WorkspaceFileMapping",
    "WorkspaceFinalizeRequest",
    "WorkspaceFinalizeResult",
    "WorkspaceImportReview",
    "WorkspaceRetentionMode",
    "WorkspaceRowReviewState",
    "WorkspaceRowRevision",
    "WorkspaceSourceFile",
    "WorkspaceSourceRemoveRequest",
    "WorkspaceSourceReviewState",
    "WorkspaceSourceType",
    "WorkspaceStatus",
    "WorkspaceTransactionRow",
]
