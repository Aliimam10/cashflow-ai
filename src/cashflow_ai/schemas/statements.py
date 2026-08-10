"""Statement coverage, balance, lineage-context, and flag contracts."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.imports import VerificationStatus
from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.transactions import Currency, Identifier

Note = Annotated[str, Field(min_length=1, max_length=2_000)]


class _StatementModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class DateRange(_StatementModel):
    """Inclusive date interval with validated ordering."""

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_order(self) -> DateRange:
        """Require the range to end on or after its start."""
        if self.end_date < self.start_date:
            msg = "date range end must not precede its start"
            raise ValueError(msg)
        return self


class CoverageStatus(StrEnum):
    """Known completeness of a statement or combined statement range."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    GAPPED = "gapped"
    OVERLAPPING = "overlapping"
    UNKNOWN = "unknown"


class StatementCoverage(_StatementModel):
    """Statement date extent and explicitly unknown periods."""

    statement_start_date: date
    statement_end_date: date
    status: CoverageStatus
    missing_periods: tuple[DateRange, ...] = ()

    @model_validator(mode="after")
    def validate_coverage(self) -> StatementCoverage:
        """Require coherent statement boundaries and missing periods."""
        if self.statement_end_date < self.statement_start_date:
            msg = "statement end date must not precede its start date"
            raise ValueError(msg)
        if self.status is CoverageStatus.COMPLETE and self.missing_periods:
            msg = "complete coverage cannot contain missing periods"
            raise ValueError(msg)
        if self.status is CoverageStatus.GAPPED and not self.missing_periods:
            msg = "gapped coverage requires at least one missing period"
            raise ValueError(msg)

        previous: DateRange | None = None
        for missing in self.missing_periods:
            if (
                missing.start_date < self.statement_start_date
                or missing.end_date > self.statement_end_date
            ):
                msg = "missing periods must fall inside statement coverage"
                raise ValueError(msg)
            if previous is not None and missing.start_date <= previous.end_date:
                msg = "missing periods must be chronological and non-overlapping"
                raise ValueError(msg)
            previous = missing
        return self


class StatementBalances(_StatementModel):
    """Opening and closing balances reported by a statement."""

    currency: Currency = Currency.GBP
    opening_balance: Money | None = None
    closing_balance: Money | None = None

    @model_validator(mode="after")
    def require_reported_balance(self) -> StatementBalances:
        """Require at least one balance reported by the source statement."""
        if self.opening_balance is None and self.closing_balance is None:
            msg = "at least one statement balance is required"
            raise ValueError(msg)
        return self


class BalanceSnapshotSource(StrEnum):
    """Origin of a balance observation."""

    STATEMENT_OPENING = "statement_opening"
    STATEMENT_CLOSING = "statement_closing"
    RUNNING_BALANCE = "running_balance"
    MANUAL = "manual"


class BalanceSnapshot(_StatementModel):
    """An account balance observation that is not a transaction."""

    snapshot_id: UUID
    account_id: Identifier
    balance: Money
    currency: Currency = Currency.GBP
    as_of_date: date
    recorded_at: AwareDatetime
    source: BalanceSnapshotSource
    verification_status: VerificationStatus
    source_document_id: UUID | None = None

    @model_validator(mode="after")
    def validate_source_document(self) -> BalanceSnapshot:
        """Tie imported snapshots, but not manual snapshots, to a document."""
        if self.source is BalanceSnapshotSource.MANUAL:
            if self.source_document_id is not None:
                msg = "manual balance snapshots cannot reference a source document"
                raise ValueError(msg)
        elif self.source_document_id is None:
            msg = "imported balance snapshots require a source document"
            raise ValueError(msg)
        return self


class StatementFlag(StrEnum):
    """Explicit structured context supplied for a statement."""

    CONTAINS_INTERNAL_TRANSFERS = "contains_internal_transfers"
    CONTAINS_REFUNDS = "contains_refunds"
    CONTAINS_REIMBURSEMENTS = "contains_reimbursements"
    CONTAINS_UNUSUAL_ONE_OFF_EXPENSES = "contains_unusual_one_off_expenses"
    CONTAINS_CASH_WITHDRAWALS = "contains_cash_withdrawals"
    MAY_CONTAIN_MISSING_DATES_OR_PAGES = "may_contain_missing_dates_or_pages"
    HISTORICAL_ARCHIVE = "historical_archive"
    OTHER_CONTEXT = "other_context"


class ImportContext(_StatementModel):
    """User-supplied statement metadata that does not mutate transactions."""

    account_id: Identifier
    coverage: StatementCoverage
    balances: StatementBalances | None = None
    flags: frozenset[StatementFlag] = frozenset()
    note: Note | None = None

    @model_validator(mode="after")
    def validate_other_context(self) -> ImportContext:
        """Require an explanatory note when the generic other flag is used."""
        if StatementFlag.OTHER_CONTEXT in self.flags and self.note is None:
            msg = "other context flag requires a free-text note"
            raise ValueError(msg)
        return self
