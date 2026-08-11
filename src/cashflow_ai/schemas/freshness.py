"""Contracts for balance evidence and deterministic financial-data freshness."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.statements import BalanceSnapshotSource, DateRange
from cashflow_ai.schemas.transactions import Currency, Identifier


class _FreshnessModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class FinancialDataMode(StrEnum):
    """Whether trusted evidence is sufficient for current-looking outputs."""

    ARCHIVE = "archive"
    ACTIVE_FORECASTING = "active_forecasting"


class FreshnessWarningCode(StrEnum):
    """Stable reasons that financial data remains in archive mode."""

    ACCOUNT_INACTIVE = "account_inactive"
    NO_VERIFIED_TRANSACTIONS = "no_verified_transactions"
    TRANSACTIONS_STALE = "transactions_stale"
    NO_VERIFIED_BALANCE = "no_verified_balance"
    BALANCE_STALE = "balance_stale"
    NO_VERIFIED_COVERAGE = "no_verified_coverage"
    COVERAGE_STALE = "coverage_stale"
    INSUFFICIENT_CONTIGUOUS_COVERAGE = "insufficient_contiguous_coverage"
    LATEST_TRANSACTION_OUTSIDE_CONTIGUOUS_COVERAGE = (
        "latest_transaction_outside_contiguous_coverage"
    )


class FreshnessPolicy(_FreshnessModel):
    """Explicit age and continuity limits used for one freshness assessment."""

    max_transaction_age_days: int = Field(ge=0)
    max_balance_age_days: int = Field(ge=0)
    max_coverage_age_days: int = Field(ge=0)
    minimum_contiguous_coverage_days: int = Field(ge=1)


class VerifiedBalanceEvidence(_FreshnessModel):
    """Latest selected verified balance observation for an account."""

    balance: Money
    currency: Currency
    as_of_date: date
    recorded_at: AwareDatetime
    source: BalanceSnapshotSource


class FinancialDataFreshness(_FreshnessModel):
    """Point-in-time evidence ages, coverage, warnings, and operating mode."""

    account_id: Identifier
    assessed_on: date
    mode: FinancialDataMode
    latest_transaction_date: date | None
    latest_verified_balance: VerifiedBalanceEvidence | None
    transaction_age_days: int | None = Field(default=None, ge=0)
    balance_age_days: int | None = Field(default=None, ge=0)
    data_freshness_days: int | None = Field(default=None, ge=0)
    latest_contiguous_coverage: DateRange | None
    contiguous_coverage_days: int = Field(ge=0)
    coverage_age_days: int | None = Field(default=None, ge=0)
    warnings: tuple[FreshnessWarningCode, ...] = ()

    @model_validator(mode="after")
    def validate_evidence_consistency(self) -> FinancialDataFreshness:
        """Keep evidence, ages, coverage length, and mode mutually consistent."""
        if (self.latest_transaction_date is None) != (
            self.transaction_age_days is None
        ):
            msg = (
                "transaction date and age must either both be present or both be absent"
            )
            raise ValueError(msg)
        if (self.latest_verified_balance is None) != (self.balance_age_days is None):
            msg = (
                "verified balance and age must either both be present or both be absent"
            )
            raise ValueError(msg)

        evidence_ages = tuple(
            age
            for age in (self.transaction_age_days, self.balance_age_days)
            if age is not None
        )
        expected_freshness = min(evidence_ages, default=None)
        if self.data_freshness_days != expected_freshness:
            msg = "data freshness must be the age of the newest verified evidence"
            raise ValueError(msg)

        if self.latest_contiguous_coverage is None:
            if self.contiguous_coverage_days != 0 or self.coverage_age_days is not None:
                msg = "absent coverage must have zero length and no age"
                raise ValueError(msg)
        else:
            expected_days = (
                self.latest_contiguous_coverage.end_date
                - self.latest_contiguous_coverage.start_date
            ).days + 1
            if self.contiguous_coverage_days != expected_days:
                msg = "coverage length must include both boundary dates"
                raise ValueError(msg)
            if self.coverage_age_days is None:
                msg = "present coverage requires a coverage age"
                raise ValueError(msg)

        if self.mode is FinancialDataMode.ACTIVE_FORECASTING and self.warnings:
            msg = "active forecasting mode cannot contain freshness warnings"
            raise ValueError(msg)
        if self.mode is FinancialDataMode.ARCHIVE and not self.warnings:
            msg = "archive mode requires at least one freshness warning"
            raise ValueError(msg)
        return self
