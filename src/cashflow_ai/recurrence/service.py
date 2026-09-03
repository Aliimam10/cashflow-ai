"""Deterministic recurring-payment detection over verified coverage only."""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from statistics import median
from typing import NamedTuple

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence.base import new_id, utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    FinancialRoleAuditRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
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
from cashflow_ai.schemas.transactions import Currency, Direction, FinancialRole


class RecurrenceServiceErrorCode(StrEnum):
    """Stable privacy-safe recurrence failures."""

    PROFILE_NOT_FOUND = "profile_not_found"
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    CANDIDATE_ALREADY_REVIEWED = "candidate_already_reviewed"
    INVALID_KNOWLEDGE_CUTOFF = "invalid_knowledge_cutoff"
    INVALID_REVIEW_TIMESTAMP = "invalid_review_timestamp"


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


def _add_months(value: date, months: int, *, preserve_month_end: bool = False) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    month_end = calendar.monthrange(year, month)[1]
    day = month_end if preserve_month_end else min(value.day, month_end)
    return date(year, month, day)


def _scheduled_date(
    anchor: date, frequency: RecurrenceFrequency, occurrence_index: int
) -> date:
    months = _MONTH_STEPS.get(frequency)
    if months is not None:
        preserve_month_end = (
            anchor.day == calendar.monthrange(anchor.year, anchor.month)[1]
        )
        return _add_months(
            anchor,
            months * occurrence_index,
            preserve_month_end=preserve_month_end,
        )
    interval = 7 if frequency is RecurrenceFrequency.WEEKLY else 14
    return anchor + timedelta(days=interval * occurrence_index)


def _known_ranges(
    session: Session,
    account_id: str,
    *,
    knowledge_cutoff_at: datetime,
) -> tuple[tuple[date, date], ...]:
    statement = (
        select(StatementCoverageRecord)
        .join(ImportContextRecord)
        .join(ImportBatchRecord)
        .where(
            ImportBatchRecord.account_id == account_id,
            ImportBatchRecord.verification_status == "verified",
            ImportBatchRecord.imported_at <= knowledge_cutoff_at,
            ImportContextRecord.created_at <= knowledge_cutoff_at,
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


class _FrequencyMatch(NamedTuple):
    frequency: RecurrenceFrequency
    interval_days: int
    maximum_error_days: int
    skipped_dates: tuple[date, ...]
    last_occurrence_index: int


def _frequency(dates: tuple[date, ...], tolerance: int) -> _FrequencyMatch | None:
    candidates: list[tuple[int, int, float, int, _FrequencyMatch]] = []
    for frequency_order, (frequency, interval) in enumerate(_FREQUENCIES):
        previous_index = 0
        errors: list[int] = []
        skipped_dates: list[date] = []
        for observed in dates[1:]:
            index = previous_index + 1
            best: tuple[int, int] | None = None
            while True:
                expected = _scheduled_date(dates[0], frequency, index)
                if expected > observed + timedelta(days=tolerance):
                    break
                error = abs((observed - expected).days)
                if error <= tolerance and (best is None or error < best[0]):
                    best = (error, index)
                index += 1
            if best is None:
                break
            error, matched_index = best
            errors.append(error)
            skipped_dates.extend(
                _scheduled_date(dates[0], frequency, skipped_index)
                for skipped_index in range(previous_index + 1, matched_index)
            )
            previous_index = matched_index
        if len(errors) != len(dates) - 1:
            continue
        match = _FrequencyMatch(
            frequency=frequency,
            interval_days=interval,
            maximum_error_days=max(errors),
            skipped_dates=tuple(skipped_dates),
            last_occurrence_index=previous_index,
        )
        candidates.append(
            (
                len(skipped_dates),
                max(errors),
                float(median(errors)),
                frequency_order,
                match,
            )
        )
    return min(candidates)[-1] if candidates else None


def _project(
    record: RecurringPaymentCandidateRecord,
    dates: tuple[date, ...],
) -> RecurringPaymentCandidate:
    return RecurringPaymentCandidate(
        candidate_id=record.id,
        account_id=record.account_id,
        merchant_group=record.merchant_group,
        currency=Currency(record.currency),
        direction=Direction(record.direction),
        financial_role=FinancialRole(record.financial_role_id),
        expected_amount=record.expected_amount,
        frequency=RecurrenceFrequency(record.frequency),
        interval_days=record.interval_days,
        occurrence_dates=dates,
        next_expected_date=record.next_expected_date,
        confidence=float(record.confidence),
        covered_missed_count=record.covered_missed_count,
        status=RecurrenceStatus(record.status),
        evidence_as_of_date=record.evidence_as_of_date,
        knowledge_cutoff_at=record.knowledge_cutoff_at,
    )


def _validate_cutoff(as_of_date: date, knowledge_cutoff_at: datetime) -> None:
    complete_at = datetime.combine(
        as_of_date + timedelta(days=1),
        time.min,
        tzinfo=UTC,
    )
    if (
        knowledge_cutoff_at.tzinfo is None
        or knowledge_cutoff_at.utcoffset() is None
        or knowledge_cutoff_at.astimezone(UTC) < complete_at
    ):
        raise RecurrenceServiceError(
            RecurrenceServiceErrorCode.INVALID_KNOWLEDGE_CUTOFF,
            "knowledge cutoff must be timezone-aware and follow the complete UTC "
            "evidence date",
        )


def _latest_roles_as_of(
    session: Session,
    transaction_ids: tuple[str, ...],
    knowledge_cutoff_at: datetime,
) -> dict[str, str]:
    if not transaction_ids:
        return {}
    statement = (
        select(FinancialRoleAuditRecord)
        .where(
            FinancialRoleAuditRecord.verified_transaction_id.in_(transaction_ids),
            FinancialRoleAuditRecord.changed_at <= knowledge_cutoff_at,
        )
        .order_by(FinancialRoleAuditRecord.changed_at, FinancialRoleAuditRecord.id)
    )
    roles: dict[str, str] = {}
    for audit in session.scalars(statement):
        roles[audit.verified_transaction_id] = audit.new_role_id
    return roles


def detect_recurring_payments(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
    as_of_date: date,
    knowledge_cutoff_at: datetime,
    policy: RecurrenceDetectionPolicy,
) -> tuple[RecurringPaymentCandidate, ...]:
    """Detect and persist reviewable patterns without treating gaps as absences."""
    _validate_cutoff(as_of_date, knowledge_cutoff_at)
    with session_scope(factory) as session:
        if session.get(UserProfileRecord, user_profile_id) is None:
            raise RecurrenceServiceError(
                RecurrenceServiceErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        statement = (
            select(VerifiedTransactionRecord)
            .join(AccountRecord)
            .join(
                RawTransactionRecord,
                RawTransactionRecord.id == VerifiedTransactionRecord.raw_transaction_id,
            )
            .join(
                ImportBatchRecord,
                ImportBatchRecord.id == RawTransactionRecord.import_batch_id,
            )
            .where(
                AccountRecord.user_profile_id == user_profile_id,
                VerifiedTransactionRecord.transaction_date <= as_of_date,
                VerifiedTransactionRecord.verified_at <= knowledge_cutoff_at,
                ImportBatchRecord.imported_at <= knowledge_cutoff_at,
                RawTransactionRecord.created_at <= knowledge_cutoff_at,
                ImportBatchRecord.account_id == VerifiedTransactionRecord.account_id,
                RawTransactionRecord.review_status == "confirmed",
                RawTransactionRecord.source_type == ImportBatchRecord.source_type,
                or_(
                    and_(
                        ImportBatchRecord.source_type == "csv",
                        ImportBatchRecord.verification_status.in_(
                            ("verified", "needs_review")
                        ),
                    ),
                    and_(
                        ImportBatchRecord.source_type.in_(("digital_pdf", "ocr_pdf")),
                        ImportBatchRecord.verification_status == "verified",
                    ),
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
        eligible_transactions = tuple(session.scalars(statement))
        roles = _latest_roles_as_of(
            session,
            tuple(item.id for item in eligible_transactions),
            knowledge_cutoff_at,
        )
        for transaction in eligible_transactions:
            merchant_group = normalise_rule_text(transaction.merchant or "")
            financial_role = roles.get(transaction.id, "unknown")
            if merchant_group and financial_role not in {"unknown", "excluded"}:
                grouped[
                    (
                        transaction.account_id,
                        merchant_group,
                        transaction.currency,
                        transaction.direction,
                        financial_role,
                    )
                ].append(transaction)
        results: list[RecurringPaymentCandidate] = []
        for (
            account_id,
            merchant_group,
            currency,
            direction,
            financial_role,
        ), group_transactions in sorted(grouped.items()):
            if len(group_transactions) < policy.minimum_occurrences:
                continue
            dates = tuple(item.transaction_date for item in group_transactions)
            if len(set(dates)) != len(dates):
                continue
            amounts = tuple(item.amount for item in group_transactions)
            expected_amount = (sum(amounts, Decimal("0")) / len(amounts)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if (
                max(abs(item - expected_amount) for item in amounts)
                > policy.maximum_amount_variation
            ):
                continue
            ranges = _known_ranges(
                session,
                account_id,
                knowledge_cutoff_at=knowledge_cutoff_at,
            )
            matched = _frequency(dates, policy.maximum_interval_variation_days)
            if matched is None:
                continue
            covered_skipped = sum(
                _covered(expected_date, ranges)
                for expected_date in matched.skipped_dates
            )
            if covered_skipped > policy.maximum_skipped_occurrences:
                continue
            frequency = matched.frequency
            interval = matched.interval_days
            interval_score = max(
                0.0,
                1 - matched.maximum_error_days / max(interval, 1),
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
                1.0,
                len(group_transactions) / max(policy.minimum_occurrences + 2, 1),
            )
            next_index = matched.last_occurrence_index + 1
            next_date = _scheduled_date(dates[0], frequency, next_index)
            covered_missed = covered_skipped
            while next_date <= as_of_date:
                if _covered(next_date, ranges):
                    covered_missed += 1
                next_index += 1
                next_date = _scheduled_date(dates[0], frequency, next_index)
            if covered_missed > policy.maximum_skipped_occurrences:
                continue
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
                    RecurringPaymentCandidateRecord.currency == currency,
                    RecurringPaymentCandidateRecord.direction == direction,
                    RecurringPaymentCandidateRecord.financial_role_id == financial_role,
                    RecurringPaymentCandidateRecord.frequency == frequency.value,
                )
                .order_by(
                    RecurringPaymentCandidateRecord.detected_at.desc(),
                    RecurringPaymentCandidateRecord.id.desc(),
                )
                .limit(1)
            )
            persisted_evidence_is_future = (
                existing is not None
                and existing.knowledge_cutoff_at > knowledge_cutoff_at
            )
            review_is_future = (
                existing is not None
                and existing.reviewed_at is not None
                and existing.reviewed_at > knowledge_cutoff_at
            )
            stored_state_is_future = persisted_evidence_is_future or review_is_future
            if (
                existing is not None
                and existing.status == "cancelled"
                and not stored_state_is_future
            ):
                continue
            if existing is None:
                existing = RecurringPaymentCandidateRecord(
                    id=new_id(),
                    account_id=account_id,
                    merchant_group=merchant_group,
                    currency=currency,
                    direction=direction,
                    financial_role_id=financial_role,
                    expected_amount=expected_amount,
                    frequency=frequency.value,
                    interval_days=interval,
                    next_expected_date=next_date,
                    confidence=Decimal(str(round(confidence, 6))),
                    covered_missed_count=covered_missed,
                    status="pending",
                    evidence_as_of_date=as_of_date,
                    knowledge_cutoff_at=knowledge_cutoff_at,
                )
                session.add(existing)
                session.flush()
            elif stored_state_is_future:
                transient = RecurringPaymentCandidate(
                    candidate_id=existing.id,
                    account_id=account_id,
                    merchant_group=merchant_group,
                    currency=Currency(currency),
                    direction=Direction(direction),
                    financial_role=FinancialRole(financial_role),
                    expected_amount=expected_amount,
                    frequency=frequency,
                    interval_days=interval,
                    occurrence_dates=dates,
                    next_expected_date=next_date,
                    confidence=confidence,
                    covered_missed_count=covered_missed,
                    status=RecurrenceStatus.PENDING,
                    evidence_as_of_date=as_of_date,
                    knowledge_cutoff_at=knowledge_cutoff_at,
                )
                results.append(transient)
                continue
            else:
                existing.expected_amount = expected_amount
                existing.interval_days = interval
                existing.next_expected_date = next_date
                existing.confidence = Decimal(str(round(confidence, 6)))
                existing.covered_missed_count = covered_missed
                existing.evidence_as_of_date = as_of_date
                existing.knowledge_cutoff_at = knowledge_cutoff_at
            member_ids = set(
                session.scalars(
                    select(RecurringPaymentMemberRecord.verified_transaction_id).where(
                        RecurringPaymentMemberRecord.candidate_id == existing.id
                    )
                )
            )
            session.add_all(
                RecurringPaymentMemberRecord(
                    candidate_id=existing.id,
                    verified_transaction_id=item.id,
                    identified_at=knowledge_cutoff_at,
                )
                for item in group_transactions
                if item.id not in member_ids
            )
            session.flush()
            results.append(_project(existing, dates))
        session.flush()
        return tuple(results)


def list_recurring_payment_candidates(
    factory: sessionmaker[Session], *, user_profile_id: str
) -> tuple[RecurringPaymentCandidate, ...]:
    """List persisted review candidates without refreshing detection evidence."""
    with session_scope(factory) as session:
        if session.get(UserProfileRecord, user_profile_id) is None:
            raise RecurrenceServiceError(
                RecurrenceServiceErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        records = tuple(
            session.scalars(
                select(RecurringPaymentCandidateRecord)
                .join(AccountRecord)
                .where(AccountRecord.user_profile_id == user_profile_id)
                .order_by(
                    RecurringPaymentCandidateRecord.next_expected_date,
                    RecurringPaymentCandidateRecord.id,
                )
            )
        )
        results: list[RecurringPaymentCandidate] = []
        for record in records:
            dates = tuple(
                session.scalars(
                    select(VerifiedTransactionRecord.transaction_date)
                    .join(
                        RecurringPaymentMemberRecord,
                        RecurringPaymentMemberRecord.verified_transaction_id
                        == VerifiedTransactionRecord.id,
                    )
                    .where(RecurringPaymentMemberRecord.candidate_id == record.id)
                    .order_by(
                        VerifiedTransactionRecord.transaction_date,
                        VerifiedTransactionRecord.id,
                    )
                )
            )
            results.append(_project(record, dates))
        return tuple(results)


def review_recurring_payment(
    factory: sessionmaker[Session], *, review: RecurrenceReview
) -> RecurrenceReviewResult:
    """Confirm or cancel a candidate atomically without inferring user intent."""
    received_at = utc_now()
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise RecurrenceServiceError(
            RecurrenceServiceErrorCode.INVALID_REVIEW_TIMESTAMP,
            "server review receipt time must be timezone-aware",
        )
    received_at = received_at.astimezone(UTC)
    if review.reviewed_at.astimezone(UTC) > received_at:
        raise RecurrenceServiceError(
            RecurrenceServiceErrorCode.INVALID_REVIEW_TIMESTAMP,
            "reported review timestamp cannot be in the future",
        )
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
        evidence_times = (
            candidate.detected_at,
            candidate.knowledge_cutoff_at,
            *session.scalars(
                select(RecurringPaymentMemberRecord.identified_at).where(
                    RecurringPaymentMemberRecord.candidate_id == candidate.id
                )
            ),
        )
        if any(received_at < evidence_at for evidence_at in evidence_times):
            raise RecurrenceServiceError(
                RecurrenceServiceErrorCode.INVALID_REVIEW_TIMESTAMP,
                "review receipt time cannot precede candidate evidence",
            )
        series_id: str | None = None
        if review.action is RecurrenceReviewAction.CONFIRM:
            series = RecurringSeriesRecord(
                id=new_id(),
                account_id=candidate.account_id,
                merchant_pattern=candidate.merchant_group,
                expected_amount=candidate.expected_amount,
                interval_days=candidate.interval_days,
                financial_role_id=candidate.financial_role_id,
                is_active=True,
                created_at=received_at,
            )
            session.add(series)
            session.flush()
            series_id = series.id
            candidate.recurring_series_id = series.id
            candidate.status = "confirmed"
        else:
            candidate.status = "cancelled"
        candidate.reviewed_at = received_at
        session.flush()
        return RecurrenceReviewResult(
            candidate_id=candidate.id,
            status=RecurrenceStatus(candidate.status),
            recurring_series_id=series_id,
        )
