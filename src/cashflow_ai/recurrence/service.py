"""Deterministic recurring-payment detection over verified coverage only."""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence.base import new_id
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RecurringPaymentCandidateRecord,
    RecurringPaymentMemberRecord,
    RecurringSeriesRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas.categorisation import normalise_rule_text
from cashflow_ai.schemas.recurrence import (
    RecurrenceDetectionPolicy,
    RecurrenceFrequency,
    RecurrenceReview,
    RecurrenceReviewAction,
    RecurrenceReviewResult,
    RecurrenceStatus,
    RecurringPaymentCandidate,
)


class RecurrenceServiceErrorCode(StrEnum):
    """Stable privacy-safe recurrence failures."""

    PROFILE_NOT_FOUND = "profile_not_found"
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    CANDIDATE_ALREADY_REVIEWED = "candidate_already_reviewed"


class RecurrenceServiceError(ValueError):
    """Controlled recurrence failure without private transaction text."""

    def __init__(self, code: RecurrenceServiceErrorCode, message: str) -> None:
        """Store a public failure code and safe message."""
        super().__init__(message)
        self.code = code


_FREQUENCIES = (
    (RecurrenceFrequency.WEEKLY, 7),
    (RecurrenceFrequency.FORTNIGHTLY, 14),
    (RecurrenceFrequency.MONTHLY, 30),
    (RecurrenceFrequency.QUARTERLY, 91),
    (RecurrenceFrequency.ANNUAL, 365),
)
_MONTH_STEPS = {
    RecurrenceFrequency.MONTHLY: 1,
    RecurrenceFrequency.QUARTERLY: 3,
    RecurrenceFrequency.ANNUAL: 12,
}


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _advance(value: date, frequency: RecurrenceFrequency) -> date:
    months = _MONTH_STEPS.get(frequency)
    if months is not None:
        return _add_months(value, months)
    days = 7 if frequency is RecurrenceFrequency.WEEKLY else 14
    return value + timedelta(days=days)


def _known_ranges(session: Session, account_id: str) -> tuple[tuple[date, date], ...]:
    statement = (
        select(StatementCoverageRecord)
        .join(ImportContextRecord)
        .join(ImportBatchRecord)
        .where(
            ImportBatchRecord.account_id == account_id,
            ImportBatchRecord.verification_status == "verified",
        )
        .order_by(StatementCoverageRecord.statement_start_date)
    )
    ranges: list[tuple[date, date]] = []
    for record in session.scalars(statement):
        if record.coverage_status in {"partial", "unknown"}:
            continue
        pieces = [(record.statement_start_date, record.statement_end_date)]
        if record.coverage_status == "gapped":
            for missing in record.missing_periods_json:
                gap_start = date.fromisoformat(str(missing["start_date"]))
                gap_end = date.fromisoformat(str(missing["end_date"]))
                next_pieces: list[tuple[date, date]] = []
                for start, end in pieces:
                    if gap_end < start or gap_start > end:
                        next_pieces.append((start, end))
                    else:
                        if start < gap_start:
                            next_pieces.append((start, gap_start - timedelta(days=1)))
                        if gap_end < end:
                            next_pieces.append((gap_end + timedelta(days=1), end))
                pieces = next_pieces
        ranges.extend(pieces)
    return tuple(ranges)


def _covered(value: date, ranges: tuple[tuple[date, date], ...]) -> bool:
    return any(start <= value <= end for start, end in ranges)


def _frequency(
    gaps: tuple[int, ...], tolerance: int
) -> tuple[RecurrenceFrequency, int] | None:
    typical = float(median(gaps))
    frequency, interval = min(_FREQUENCIES, key=lambda item: abs(typical - item[1]))
    if max(abs(gap - interval) for gap in gaps) > tolerance:
        return None
    return frequency, interval


def _project(
    record: RecurringPaymentCandidateRecord, dates: tuple[date, ...]
) -> RecurringPaymentCandidate:
    return RecurringPaymentCandidate(
        candidate_id=record.id,
        account_id=record.account_id,
        merchant_group=record.merchant_group,
        expected_amount=record.expected_amount,
        frequency=RecurrenceFrequency(record.frequency),
        interval_days=record.interval_days,
        occurrence_dates=dates,
        next_expected_date=record.next_expected_date,
        confidence=float(record.confidence),
        covered_missed_count=record.covered_missed_count,
        status=RecurrenceStatus(record.status),
    )


def detect_recurring_payments(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
    as_of_date: date,
    policy: RecurrenceDetectionPolicy,
) -> tuple[RecurringPaymentCandidate, ...]:
    """Detect and persist reviewable patterns without treating gaps as absences."""
    with session_scope(factory) as session:
        if session.get(UserProfileRecord, user_profile_id) is None:
            raise RecurrenceServiceError(
                RecurrenceServiceErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        statement = (
            select(VerifiedTransactionRecord)
            .join(AccountRecord)
            .where(
                AccountRecord.user_profile_id == user_profile_id,
                VerifiedTransactionRecord.transaction_date <= as_of_date,
                VerifiedTransactionRecord.financial_role_id.notin_(
                    ("unknown", "excluded")
                ),
            )
            .order_by(
                VerifiedTransactionRecord.account_id,
                VerifiedTransactionRecord.transaction_date,
                VerifiedTransactionRecord.id,
            )
        )
        grouped: dict[
            tuple[str, str, str, str, str], list[VerifiedTransactionRecord]
        ] = defaultdict(list)
        for transaction in session.scalars(statement):
            merchant_group = normalise_rule_text(transaction.merchant or "")
            if merchant_group:
                grouped[
                    (
                        transaction.account_id,
                        merchant_group,
                        transaction.currency,
                        transaction.direction,
                        transaction.financial_role_id,
                    )
                ].append(transaction)
        results: list[RecurringPaymentCandidate] = []
        for (
            account_id,
            merchant_group,
            _currency,
            _direction,
            _role,
        ), transactions in sorted(grouped.items()):
            if len(transactions) < policy.minimum_occurrences:
                continue
            dates = tuple(item.transaction_date for item in transactions)
            if len(set(dates)) != len(dates):
                continue
            amounts = tuple(item.amount for item in transactions)
            expected_amount = sum(amounts, Decimal("0")) / len(amounts)
            if (
                max(abs(item - expected_amount) for item in amounts)
                > policy.maximum_amount_variation
            ):
                continue
            gaps = tuple((right - left).days for left, right in pairwise(dates))
            matched = _frequency(gaps, policy.maximum_interval_variation_days)
            if matched is None:
                continue
            frequency, interval = matched
            interval_score = max(
                0.0, 1 - max(abs(gap - interval) for gap in gaps) / max(interval, 1)
            )
            amount_denominator = max(abs(expected_amount), Decimal("0.01"))
            amount_score = max(
                0.0,
                1
                - float(
                    max(abs(item - expected_amount) for item in amounts)
                    / amount_denominator
                ),
            )
            occurrence_score = min(
                1.0, len(transactions) / max(policy.minimum_occurrences + 2, 1)
            )
            next_date = _advance(dates[-1], frequency)
            covered_missed = 0
            ranges = _known_ranges(session, account_id)
            while next_date <= as_of_date:
                if _covered(next_date, ranges):
                    covered_missed += 1
                next_date = _advance(next_date, frequency)
            confidence = max(
                0.0,
                min(
                    1.0,
                    (interval_score + amount_score + occurrence_score) / 3
                    - covered_missed * 0.15,
                ),
            )
            if confidence < policy.minimum_confidence:
                continue
            existing = session.scalar(
                select(RecurringPaymentCandidateRecord)
                .where(
                    RecurringPaymentCandidateRecord.account_id == account_id,
                    RecurringPaymentCandidateRecord.merchant_group == merchant_group,
                    RecurringPaymentCandidateRecord.frequency == frequency.value,
                )
                .order_by(RecurringPaymentCandidateRecord.detected_at.desc())
                .limit(1)
            )
            if existing is not None and existing.status == "cancelled":
                continue
            if existing is None:
                existing = RecurringPaymentCandidateRecord(
                    id=new_id(),
                    account_id=account_id,
                    merchant_group=merchant_group,
                    expected_amount=expected_amount,
                    frequency=frequency.value,
                    interval_days=interval,
                    next_expected_date=next_date,
                    confidence=Decimal(str(round(confidence, 6))),
                    covered_missed_count=covered_missed,
                    status="pending",
                )
                session.add(existing)
                session.flush()
            elif existing.status == "pending":
                existing.expected_amount = expected_amount
                existing.next_expected_date = next_date
                existing.confidence = Decimal(str(round(confidence, 6)))
                existing.covered_missed_count = covered_missed
            member_ids = set(
                session.scalars(
                    select(RecurringPaymentMemberRecord.verified_transaction_id).where(
                        RecurringPaymentMemberRecord.candidate_id == existing.id
                    )
                )
            )
            session.add_all(
                RecurringPaymentMemberRecord(
                    candidate_id=existing.id, verified_transaction_id=item.id
                )
                for item in transactions
                if item.id not in member_ids
            )
            session.flush()
            member_dates = tuple(
                session.scalars(
                    select(VerifiedTransactionRecord.transaction_date)
                    .join(
                        RecurringPaymentMemberRecord,
                        RecurringPaymentMemberRecord.verified_transaction_id
                        == VerifiedTransactionRecord.id,
                    )
                    .where(RecurringPaymentMemberRecord.candidate_id == existing.id)
                    .order_by(VerifiedTransactionRecord.transaction_date)
                )
            )
            results.append(_project(existing, member_dates))
        session.flush()
        return tuple(results)


def review_recurring_payment(
    factory: sessionmaker[Session], *, review: RecurrenceReview
) -> RecurrenceReviewResult:
    """Confirm or cancel a candidate atomically without inferring user intent."""
    with session_scope(factory) as session:
        candidate = session.scalar(
            select(RecurringPaymentCandidateRecord)
            .join(AccountRecord)
            .where(
                RecurringPaymentCandidateRecord.id == review.candidate_id,
                AccountRecord.user_profile_id == review.user_profile_id,
            )
        )
        if candidate is None:
            raise RecurrenceServiceError(
                RecurrenceServiceErrorCode.CANDIDATE_NOT_FOUND,
                "recurring-payment candidate is unavailable to this profile",
            )
        if candidate.status != "pending":
            raise RecurrenceServiceError(
                RecurrenceServiceErrorCode.CANDIDATE_ALREADY_REVIEWED,
                "recurring-payment candidate has already been reviewed",
            )
        series_id: str | None = None
        if review.action is RecurrenceReviewAction.CONFIRM:
            role_id = session.scalar(
                select(VerifiedTransactionRecord.financial_role_id)
                .join(
                    RecurringPaymentMemberRecord,
                    RecurringPaymentMemberRecord.verified_transaction_id
                    == VerifiedTransactionRecord.id,
                )
                .where(RecurringPaymentMemberRecord.candidate_id == candidate.id)
                .order_by(VerifiedTransactionRecord.transaction_date)
                .limit(1)
            )
            assert role_id is not None
            series = RecurringSeriesRecord(
                id=new_id(),
                account_id=candidate.account_id,
                merchant_pattern=candidate.merchant_group,
                expected_amount=candidate.expected_amount,
                interval_days=candidate.interval_days,
                financial_role_id=role_id,
                is_active=True,
                created_at=review.reviewed_at,
            )
            session.add(series)
            session.flush()
            series_id = series.id
            candidate.recurring_series_id = series.id
            candidate.status = "confirmed"
        else:
            candidate.status = "cancelled"
        candidate.reviewed_at = review.reviewed_at
        session.flush()
        return RecurrenceReviewResult(
            candidate_id=candidate.id,
            status=RecurrenceStatus(candidate.status),
            recurring_series_id=series_id,
        )
