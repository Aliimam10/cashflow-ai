"""Contracts for coverage-aware recurring-payment detection and review."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.transactions import Identifier


class _RecurrenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RecurrenceFrequency(StrEnum):
    """Supported calendar or fixed-day payment frequencies."""

    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class RecurrenceStatus(StrEnum):
    """User-review lifecycle for a detected pattern."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class RecurrenceReviewAction(StrEnum):
    """Explicit user action for one detected candidate."""

    CONFIRM = "confirm"
    CANCEL = "cancel"


class RecurrenceDetectionPolicy(_RecurrenceModel):
    """Explicit thresholds; no product constants are hidden in the detector."""

    minimum_occurrences: int = Field(ge=2)
    maximum_amount_variation: Money = Field(ge=0)
    maximum_interval_variation_days: int = Field(ge=0, le=31)
    minimum_confidence: float = Field(ge=0, le=1)


class RecurringPaymentCandidate(_RecurrenceModel):
    """Detected series projection without raw transaction descriptions."""

    candidate_id: Identifier
    account_id: Identifier
    merchant_group: str = Field(min_length=1, max_length=500)
    expected_amount: Money
    frequency: RecurrenceFrequency
    interval_days: int = Field(gt=0)
    occurrence_dates: tuple[date, ...] = Field(min_length=2)
    next_expected_date: date
    confidence: float = Field(ge=0, le=1)
    covered_missed_count: int = Field(ge=0)
    status: RecurrenceStatus

    @model_validator(mode="after")
    def validate_dates(self) -> RecurringPaymentCandidate:
        """Require unique chronological evidence and a future next date."""
        if tuple(sorted(set(self.occurrence_dates))) != self.occurrence_dates:
            raise ValueError("occurrence dates must be unique and chronological")
        if self.next_expected_date <= self.occurrence_dates[-1]:
            raise ValueError("next expected date must follow the latest occurrence")
        return self


class RecurrenceReview(_RecurrenceModel):
    """Explicit confirmation or cancellation of a pending candidate."""

    user_profile_id: Identifier
    candidate_id: Identifier
    action: RecurrenceReviewAction
    reviewed_at: datetime

    @model_validator(mode="after")
    def validate_timestamp(self) -> RecurrenceReview:
        """Require an auditable timezone-aware review time."""
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("reviewed_at must be timezone-aware")
        return self


class RecurrenceReviewResult(_RecurrenceModel):
    """Result of an atomic recurrence review."""

    candidate_id: Identifier
    status: RecurrenceStatus
    recurring_series_id: Identifier | None = None


__all__ = [
    "RecurrenceDetectionPolicy",
    "RecurrenceFrequency",
    "RecurrenceReview",
    "RecurrenceReviewAction",
    "RecurrenceReviewResult",
    "RecurrenceStatus",
    "RecurringPaymentCandidate",
]
