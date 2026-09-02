"""Balance observation and conservative financial-data freshness services."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.invalidation import invalidate_derived_results_in_session
from cashflow_ai.persistence.base import utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    BalanceSnapshotRecord,
    StatementCoverageRecord,
)
from cashflow_ai.persistence.repositories import (
    AccountRepository,
    BalanceSnapshotRepository,
    StatementRepository,
    TransactionRepository,
)
from cashflow_ai.schemas.freshness import (
    FinancialDataFreshness,
    FinancialDataMode,
    FreshnessPolicy,
    FreshnessWarningCode,
    VerifiedBalanceEvidence,
)
from cashflow_ai.schemas.imports import VerificationStatus
from cashflow_ai.schemas.invalidation import SourceDataChangeType
from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.statements import (
    BalanceSnapshot,
    BalanceSnapshotSource,
    CoverageStatus,
    DateRange,
)
from cashflow_ai.schemas.transactions import Currency, Identifier


class BalanceServiceErrorCode(StrEnum):
    """Stable failures exposed by the balance application boundary."""

    ACCOUNT_NOT_FOUND = "account_not_found"
    ACCOUNT_INACTIVE = "account_inactive"
    ACCOUNT_CURRENCY_MISMATCH = "account_currency_mismatch"


class BalanceServiceError(ValueError):
    """Controlled balance-service failure without private financial values."""

    def __init__(self, code: BalanceServiceErrorCode, message: str) -> None:
        """Store a stable public code without balance or transaction values."""
        super().__init__(message)
        self.code = code


class ManualBalanceEntry(BaseModel):
    """Explicit current-balance observation supplied by the local user."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    account_id: Identifier
    balance: Money
    currency: Currency = Currency.GBP
    as_of_date: date
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def validate_observation_date(self) -> ManualBalanceEntry:
        """Prevent a manual observation from post-dating its recording time."""
        if self.as_of_date > self.recorded_at.date():
            msg = "manual balance date cannot be after its recording date"
            raise ValueError(msg)
        return self


def _balance_contract(record: BalanceSnapshotRecord) -> BalanceSnapshot:
    return BalanceSnapshot(
        snapshot_id=UUID(record.id),
        account_id=record.account_id,
        balance=record.balance,
        currency=Currency(record.currency),
        as_of_date=record.as_of_date,
        recorded_at=record.recorded_at,
        source=BalanceSnapshotSource(record.source),
        verification_status=VerificationStatus(record.verification_status),
        source_document_id=None,
    )


def record_manual_balance(
    factory: sessionmaker[Session],
    entry: ManualBalanceEntry,
) -> BalanceSnapshot:
    """Persist a verified balance observation without creating a transaction."""
    with session_scope(factory) as session:
        account = AccountRepository(session).get(entry.account_id)
        if account is None:
            raise BalanceServiceError(
                BalanceServiceErrorCode.ACCOUNT_NOT_FOUND,
                "account does not exist",
            )
        if not account.is_active:
            raise BalanceServiceError(
                BalanceServiceErrorCode.ACCOUNT_INACTIVE,
                "inactive accounts cannot receive manual balance observations",
            )
        if account.currency != entry.currency.value:
            raise BalanceServiceError(
                BalanceServiceErrorCode.ACCOUNT_CURRENCY_MISMATCH,
                "balance currency must match the account currency",
            )

        record = BalanceSnapshotRepository(session).add(
            BalanceSnapshotRecord(
                account_id=entry.account_id,
                import_batch_id=None,
                balance=entry.balance,
                currency=entry.currency.value,
                as_of_date=entry.as_of_date,
                recorded_at=entry.recorded_at,
                source=BalanceSnapshotSource.MANUAL.value,
                verification_status=VerificationStatus.VERIFIED.value,
            )
        )
        invalidate_derived_results_in_session(
            session,
            account_id=entry.account_id,
            change_type=SourceDataChangeType.CURRENT_BALANCE_CHANGED,
            changed_at=utc_now(),
        )
        return _balance_contract(record)


def _known_segments(
    records: Iterable[StatementCoverageRecord],
) -> tuple[DateRange, ...]:
    segments: list[DateRange] = []
    one_day = timedelta(days=1)
    for record in records:
        start = record.statement_start_date
        end = record.statement_end_date

        status = CoverageStatus(record.coverage_status)
        if status in {CoverageStatus.PARTIAL, CoverageStatus.UNKNOWN}:
            continue
        if status in {CoverageStatus.COMPLETE, CoverageStatus.OVERLAPPING}:
            segments.append(DateRange(start_date=start, end_date=end))
            continue

        cursor = start
        for item in record.missing_periods_json:
            missing = DateRange.model_validate(item)
            if cursor < missing.start_date:
                segments.append(
                    DateRange(start_date=cursor, end_date=missing.start_date - one_day)
                )
            cursor = missing.end_date + one_day
        if cursor <= end:
            segments.append(DateRange(start_date=cursor, end_date=end))
    return tuple(segments)


def _merge_segments(segments: Iterable[DateRange]) -> tuple[DateRange, ...]:
    ordered = sorted(segments, key=lambda item: (item.start_date, item.end_date))
    merged: list[DateRange] = []
    for segment in ordered:
        if not merged:
            merged.append(segment)
            continue
        previous = merged[-1]
        distance = (segment.start_date - previous.end_date).days
        if distance <= 1:
            merged[-1] = DateRange(
                start_date=previous.start_date,
                end_date=max(previous.end_date, segment.end_date),
            )
        else:
            merged.append(segment)
    return tuple(merged)


def _verified_balance_evidence(
    record: BalanceSnapshotRecord | None,
) -> VerifiedBalanceEvidence | None:
    if record is None:
        return None
    return VerifiedBalanceEvidence(
        balance=record.balance,
        currency=Currency(record.currency),
        as_of_date=record.as_of_date,
        recorded_at=record.recorded_at,
        source=BalanceSnapshotSource(record.source),
    )


def assess_financial_data_freshness(
    factory: sessionmaker[Session],
    *,
    account_id: str,
    as_of_date: date,
    policy: FreshnessPolicy,
) -> FinancialDataFreshness:
    """Assess trusted evidence at a cutoff without producing a forecast."""
    with session_scope(factory) as session:
        account = AccountRepository(session).get(account_id)
        if account is None:
            raise BalanceServiceError(
                BalanceServiceErrorCode.ACCOUNT_NOT_FOUND,
                "account does not exist",
            )

        latest_transaction_date = TransactionRepository(session).latest_verified_date(
            account_id,
            as_of_date=as_of_date,
        )
        balance_record = BalanceSnapshotRepository(session).latest_verified_for_account(
            account_id, as_of_date=as_of_date
        )
        coverage_records = StatementRepository(
            session
        ).list_verified_coverages_for_account(account_id, as_of_date=as_of_date)

    latest_balance = _verified_balance_evidence(balance_record)
    transaction_age = (
        (as_of_date - latest_transaction_date).days
        if latest_transaction_date is not None
        else None
    )
    balance_age = (
        (as_of_date - latest_balance.as_of_date).days
        if latest_balance is not None
        else None
    )
    evidence_ages = tuple(
        age for age in (transaction_age, balance_age) if age is not None
    )
    data_freshness = min(evidence_ages, default=None)

    merged_coverage = _merge_segments(_known_segments(coverage_records))
    latest_coverage = max(
        merged_coverage,
        key=lambda item: (item.end_date, item.start_date),
        default=None,
    )
    coverage_days = (
        (latest_coverage.end_date - latest_coverage.start_date).days + 1
        if latest_coverage is not None
        else 0
    )
    coverage_age = (
        (as_of_date - latest_coverage.end_date).days
        if latest_coverage is not None
        else None
    )

    warnings: list[FreshnessWarningCode] = []
    if not account.is_active:
        warnings.append(FreshnessWarningCode.ACCOUNT_INACTIVE)
    if transaction_age is None:
        warnings.append(FreshnessWarningCode.NO_VERIFIED_TRANSACTIONS)
    elif transaction_age > policy.max_transaction_age_days:
        warnings.append(FreshnessWarningCode.TRANSACTIONS_STALE)
    if balance_age is None:
        warnings.append(FreshnessWarningCode.NO_VERIFIED_BALANCE)
    elif balance_age > policy.max_balance_age_days:
        warnings.append(FreshnessWarningCode.BALANCE_STALE)
    if latest_coverage is None:
        warnings.append(FreshnessWarningCode.NO_VERIFIED_COVERAGE)
    else:
        if coverage_age is not None and coverage_age > policy.max_coverage_age_days:
            warnings.append(FreshnessWarningCode.COVERAGE_STALE)
        if coverage_days < policy.minimum_contiguous_coverage_days:
            warnings.append(FreshnessWarningCode.INSUFFICIENT_CONTIGUOUS_COVERAGE)
        if latest_transaction_date is not None and not (
            latest_coverage.start_date
            <= latest_transaction_date
            <= latest_coverage.end_date
        ):
            warnings.append(
                FreshnessWarningCode.LATEST_TRANSACTION_OUTSIDE_CONTIGUOUS_COVERAGE
            )

    mode = (
        FinancialDataMode.ARCHIVE if warnings else FinancialDataMode.ACTIVE_FORECASTING
    )
    return FinancialDataFreshness(
        account_id=account_id,
        assessed_on=as_of_date,
        mode=mode,
        latest_transaction_date=latest_transaction_date,
        latest_verified_balance=latest_balance,
        transaction_age_days=transaction_age,
        balance_age_days=balance_age,
        data_freshness_days=data_freshness,
        latest_contiguous_coverage=latest_coverage,
        contiguous_coverage_days=coverage_days,
        coverage_age_days=coverage_age,
        warnings=tuple(warnings),
    )
