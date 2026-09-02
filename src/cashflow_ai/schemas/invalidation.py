"""Typed contracts for derived-data revisions and safe recomputation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.transactions import Identifier


class _InvalidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceDataChangeType(StrEnum):
    """Financial source changes that can invalidate derived results."""

    OCR_CORRECTED = "ocr_corrected"
    TRANSACTION_AMOUNT_CHANGED = "transaction_amount_changed"
    FINANCIAL_ROLE_CHANGED = "financial_role_changed"
    CATEGORY_CHANGED = "category_changed"
    TRANSFER_CONFIRMED = "transfer_confirmed"
    STATEMENT_ADDED = "statement_added"
    IMPORT_DELETED = "import_deleted"
    CURRENT_BALANCE_CHANGED = "current_balance_changed"


class DerivedOutputType(StrEnum):
    """Derived result families governed by revision metadata."""

    ANALYTICS = "analytics"
    RECURRING_SERIES = "recurring_series"
    ANOMALY_ALERTS = "anomaly_alerts"
    BUDGETS = "budgets"
    FORECASTS = "forecasts"
    SCENARIOS = "scenarios"
    MODEL_PERFORMANCE_COMPARISONS = "model_performance_comparisons"


class DerivedResultStatus(StrEnum):
    """Whether a result exists and matches its required source revision."""

    UNAVAILABLE = "unavailable"
    CURRENT = "current"
    STALE = "stale"


class FinancialDataRevision(_InvalidationModel):
    """Latest monotonic source-data revision for one local account."""

    account_id: Identifier
    revision: int = Field(ge=0)
    last_change_type: SourceDataChangeType | None
    changed_at: AwareDatetime | None

    @model_validator(mode="after")
    def validate_origin(self) -> FinancialDataRevision:
        """Revision zero has no source-change event; later revisions always do."""
        if (self.revision == 0) != (
            self.last_change_type is None and self.changed_at is None
        ):
            raise ValueError("financial revision origin is inconsistent")
        return self


class DerivedResultFreshness(_InvalidationModel):
    """Data-minimised freshness state for one derived output family."""

    account_id: Identifier
    output_type: DerivedOutputType
    status: DerivedResultStatus
    required_revision: int = Field(ge=0)
    computed_revision: int | None = Field(default=None, ge=0)
    generated_at: AwareDatetime | None
    invalidated_at: AwareDatetime | None
    invalidated_by: SourceDataChangeType | None

    @model_validator(mode="after")
    def validate_state(self) -> DerivedResultFreshness:
        """Tie each status to complete and internally ordered revision evidence."""
        invalidation_present = (
            self.invalidated_at is not None or self.invalidated_by is not None
        )
        invalidation_complete = (
            self.invalidated_at is not None and self.invalidated_by is not None
        )
        if self.status is DerivedResultStatus.CURRENT:
            if (
                self.computed_revision != self.required_revision
                or self.generated_at is None
                or self.invalidated_at is not None
                or self.invalidated_by is not None
            ):
                raise ValueError("current derived result has inconsistent evidence")
        elif self.status is DerivedResultStatus.STALE:
            if (
                self.computed_revision is None
                or self.computed_revision >= self.required_revision
                or self.generated_at is None
                or not invalidation_complete
            ):
                raise ValueError("stale derived result has inconsistent evidence")
        elif (
            self.computed_revision is not None
            or self.generated_at is not None
            or (self.required_revision > 0 and not invalidation_complete)
            or (self.required_revision == 0 and invalidation_present)
        ):
            raise ValueError("unavailable derived result has inconsistent evidence")
        return self


class DerivedComputationToken(_InvalidationModel):
    """Revision captured before one potentially long recomputation."""

    account_id: Identifier
    output_type: DerivedOutputType
    required_revision: int = Field(ge=0)
    started_at: AwareDatetime


class DerivedInvalidation(_InvalidationModel):
    """Revision change and exact derived families affected by it."""

    revision: FinancialDataRevision
    affected_outputs: tuple[DerivedOutputType, ...] = Field(min_length=1)
    freshness: tuple[DerivedResultFreshness, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_outputs(self) -> DerivedInvalidation:
        """Require one ordered freshness record for every affected output."""
        identities = tuple(item.output_type for item in self.freshness)
        if identities != self.affected_outputs or len(set(identities)) != len(
            identities
        ):
            raise ValueError("invalidation freshness does not match affected outputs")
        if any(
            item.account_id != self.revision.account_id
            or item.required_revision != self.revision.revision
            or item.status is DerivedResultStatus.CURRENT
            for item in self.freshness
        ):
            raise ValueError("invalidation contains current or mis-scoped evidence")
        return self


class DerivedRefreshResult[PayloadT](_InvalidationModel):
    """Transient recomputed payload paired with its committed freshness metadata."""

    payload: PayloadT
    freshness: DerivedResultFreshness

    @model_validator(mode="after")
    def validate_current(self) -> DerivedRefreshResult[PayloadT]:
        """A successful refresh can expose only a current result."""
        if self.freshness.status is not DerivedResultStatus.CURRENT:
            raise ValueError("refreshed derived result must be current")
        return self


__all__ = [
    "DerivedComputationToken",
    "DerivedInvalidation",
    "DerivedOutputType",
    "DerivedRefreshResult",
    "DerivedResultFreshness",
    "DerivedResultStatus",
    "FinancialDataRevision",
    "SourceDataChangeType",
]
