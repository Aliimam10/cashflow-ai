"""Leakage-safe daily/weekly forecasting data and simple baselines."""

from __future__ import annotations

import calendar
import math
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import NamedTuple

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

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
from cashflow_ai.schemas.forecast_models import ForecastInferenceRow
from cashflow_ai.schemas.forecasting import (
    BaselineMetrics,
    DailyForecastObservation,
    ExpandingWindowFold,
    ForecastBaselineEvaluation,
    ForecastBaselineName,
    ForecastDataset,
    ForecastDatasetPlan,
    ForecastDayStatus,
    ForecastFeatureRow,
    RecurringOutflowProjection,
    WeeklyForecastTarget,
)

_ZERO = Decimal("0.00")


class _RecurrenceMemberEvidence(NamedTuple):
    """One trusted recurring member and its point-in-time availability."""

    transaction_id: str
    transaction_date: date
    identified_at: datetime
    known_at: datetime


class ForecastingDataErrorCode(StrEnum):
    """Stable failures for dataset and baseline construction."""

    PROFILE_NOT_FOUND = "profile_not_found"
    ACCOUNT_SCOPE_NOT_FOUND = "account_scope_not_found"
    TOO_FEW_COMPLETE_WEEKS = "too_few_complete_weeks"
    INVALID_EVALUATION_POLICY = "invalid_evaluation_policy"


class ForecastingDataError(ValueError):
    """Controlled forecasting-data failure without private source text."""

    def __init__(self, code: ForecastingDataErrorCode, message: str) -> None:
        """Store a stable code with a privacy-safe message."""
        super().__init__(message)
        self.code = code


def _dates(start: date, end: date) -> tuple[date, ...]:
    return tuple(
        start + timedelta(days=offset) for offset in range((end - start).days + 1)
    )


def _coverage_dates(
    session: Session,
    account_id: str,
    *,
    cutoff: date,
    knowledge_cutoff_at: datetime,
) -> dict[date, datetime]:
    statement = (
        select(
            StatementCoverageRecord,
            ImportBatchRecord.imported_at,
            ImportContextRecord.created_at,
        )
        .select_from(StatementCoverageRecord)
        .join(
            ImportContextRecord,
            ImportContextRecord.id == StatementCoverageRecord.import_context_id,
        )
        .join(
            ImportBatchRecord,
            ImportBatchRecord.id == ImportContextRecord.import_batch_id,
        )
        .where(
            ImportBatchRecord.account_id == account_id,
            ImportBatchRecord.verification_status == "verified",
            ImportBatchRecord.imported_at <= knowledge_cutoff_at,
            ImportContextRecord.created_at <= knowledge_cutoff_at,
            StatementCoverageRecord.statement_start_date <= cutoff,
        )
    )
    covered: dict[date, datetime] = {}
    for record, imported_at, confirmed_at in session.execute(statement).tuples():
        if record.coverage_status in {"partial", "unknown"}:
            continue
        available_at = max(imported_at, confirmed_at)
        end = min(record.statement_end_date, cutoff)
        record_dates = set(_dates(record.statement_start_date, end))
        if record.coverage_status == "gapped":
            for missing in record.missing_periods_json:
                gap_start = date.fromisoformat(str(missing["start_date"]))
                gap_end = min(date.fromisoformat(str(missing["end_date"])), cutoff)
                if gap_end >= gap_start:
                    record_dates.difference_update(_dates(gap_start, gap_end))
        for covered_date in record_dates:
            existing = covered.get(covered_date)
            if existing is None or available_at < existing:
                covered[covered_date] = available_at
    return covered


def _financial_roles_as_of(
    session: Session,
    transaction_ids: tuple[str, ...],
    *,
    knowledge_cutoff_at: datetime,
) -> dict[str, tuple[str, datetime]]:
    """Return only explicit user-confirmed roles known at the supplied cutoff."""
    if not transaction_ids:
        return {}
    statement = (
        select(FinancialRoleAuditRecord)
        .where(
            FinancialRoleAuditRecord.verified_transaction_id.in_(transaction_ids),
            FinancialRoleAuditRecord.changed_at <= knowledge_cutoff_at,
        )
        .order_by(
            FinancialRoleAuditRecord.changed_at,
            FinancialRoleAuditRecord.id,
        )
    )
    latest: dict[str, tuple[str, datetime]] = {}
    for audit in session.scalars(statement):
        latest[audit.verified_transaction_id] = (audit.new_role_id, audit.changed_at)
    return latest


def _confirmed_expense_candidates(
    session: Session,
    account_ids: tuple[str, ...],
    *,
    knowledge_cutoff_at: datetime,
) -> tuple[tuple[RecurringSeriesRecord, RecurringPaymentCandidateRecord], ...]:
    """Return user-confirmed expense recurrences available by the cutoff."""
    return tuple(
        session.execute(
            select(RecurringSeriesRecord, RecurringPaymentCandidateRecord)
            .join(
                RecurringPaymentCandidateRecord,
                RecurringPaymentCandidateRecord.recurring_series_id
                == RecurringSeriesRecord.id,
            )
            .where(
                RecurringSeriesRecord.account_id.in_(account_ids),
                RecurringSeriesRecord.is_active.is_(True),
                RecurringSeriesRecord.financial_role_id == "expense",
                RecurringSeriesRecord.created_at <= knowledge_cutoff_at,
                RecurringPaymentCandidateRecord.status == "confirmed",
                RecurringPaymentCandidateRecord.detected_at <= knowledge_cutoff_at,
                RecurringPaymentCandidateRecord.knowledge_cutoff_at
                <= knowledge_cutoff_at,
                RecurringPaymentCandidateRecord.evidence_as_of_date
                <= knowledge_cutoff_at.date(),
                RecurringPaymentCandidateRecord.reviewed_at.is_not(None),
                RecurringPaymentCandidateRecord.reviewed_at <= knowledge_cutoff_at,
            )
        ).tuples()
    )


def _advance_recurrence(value: date, frequency: str) -> date:
    """Advance fixed-week or calendar recurrence while preserving month-end."""
    if frequency == "weekly":
        return value + timedelta(days=7)
    if frequency == "fortnightly":
        return value + timedelta(days=14)
    months = {"monthly": 1, "quarterly": 3, "annual": 12}[frequency]
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    next_month_end = calendar.monthrange(year, month)[1]
    day = (
        next_month_end
        if value.day == calendar.monthrange(value.year, value.month)[1]
        else min(value.day, next_month_end)
    )
    return date(year, month, day)


def _recurrence_member_dates(
    session: Session,
    candidate_ids: tuple[str, ...],
    *,
    knowledge_cutoff_at: datetime,
) -> dict[str, tuple[_RecurrenceMemberEvidence, ...]]:
    """Return trusted recurrence evidence and when each membership became known."""
    if not candidate_ids:
        return {}
    statement = (
        select(
            RecurringPaymentMemberRecord.candidate_id,
            RecurringPaymentMemberRecord.verified_transaction_id,
            VerifiedTransactionRecord.transaction_date,
            RecurringPaymentMemberRecord.identified_at,
            VerifiedTransactionRecord.verified_at,
            ImportBatchRecord.imported_at,
            RawTransactionRecord.created_at,
        )
        .join(
            VerifiedTransactionRecord,
            VerifiedTransactionRecord.id
            == RecurringPaymentMemberRecord.verified_transaction_id,
        )
        .join(
            RecurringPaymentCandidateRecord,
            RecurringPaymentCandidateRecord.id
            == RecurringPaymentMemberRecord.candidate_id,
        )
        .join(
            RawTransactionRecord,
            RawTransactionRecord.id == VerifiedTransactionRecord.raw_transaction_id,
        )
        .join(
            ImportBatchRecord,
            ImportBatchRecord.id == RawTransactionRecord.import_batch_id,
        )
        .where(
            RecurringPaymentMemberRecord.candidate_id.in_(candidate_ids),
            RecurringPaymentCandidateRecord.account_id
            == VerifiedTransactionRecord.account_id,
            ImportBatchRecord.account_id == VerifiedTransactionRecord.account_id,
            RecurringPaymentMemberRecord.identified_at <= knowledge_cutoff_at,
            VerifiedTransactionRecord.verified_at <= knowledge_cutoff_at,
            ImportBatchRecord.imported_at <= knowledge_cutoff_at,
            RawTransactionRecord.created_at <= knowledge_cutoff_at,
            VerifiedTransactionRecord.transaction_date <= knowledge_cutoff_at.date(),
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
            RecurringPaymentMemberRecord.candidate_id,
            VerifiedTransactionRecord.transaction_date,
            VerifiedTransactionRecord.id,
        )
    )
    grouped: dict[str, list[_RecurrenceMemberEvidence]] = defaultdict(list)
    for (
        candidate_id,
        transaction_id,
        transaction_date,
        identified_at,
        verified_at,
        imported_at,
        raw_created_at,
    ) in session.execute(statement).tuples():
        grouped[candidate_id].append(
            _RecurrenceMemberEvidence(
                transaction_id=transaction_id,
                transaction_date=transaction_date,
                identified_at=identified_at,
                known_at=max(
                    identified_at,
                    verified_at,
                    imported_at,
                    raw_created_at,
                ),
            )
        )
    return {key: tuple(values) for key, values in grouped.items()}


def _payday_distances(value: date, payday_days: tuple[int, ...]) -> tuple[int, int]:
    candidates: list[date] = []
    for month_delta in (-1, 0, 1):
        month_index = value.month - 1 + month_delta
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        maximum = calendar.monthrange(year, month)[1]
        candidates.extend(date(year, month, min(day, maximum)) for day in payday_days)
    previous = max(item for item in candidates if item <= value)
    upcoming = min(item for item in candidates if item >= value)
    return (value - previous).days, (upcoming - value).days


def _weekly_targets(
    daily: tuple[DailyForecastObservation, ...],
    recurring_by_week: dict[date, Decimal],
) -> tuple[WeeklyForecastTarget, ...]:
    by_date = {item.observation_date: item for item in daily}
    if not daily:
        return ()
    first = daily[0].observation_date
    monday = first + timedelta(days=(-first.weekday()) % 7)
    targets: list[WeeklyForecastTarget] = []
    while monday + timedelta(days=6) <= daily[-1].observation_date:
        week_dates = _dates(monday, monday + timedelta(days=6))
        observations = tuple(by_date[item] for item in week_dates)
        if all(item.status is ForecastDayStatus.COVERED for item in observations):
            availability = tuple(
                item.known_at for item in observations if item.known_at is not None
            )
            assert len(availability) == 7
            targets.append(
                WeeklyForecastTarget(
                    week_start=monday,
                    week_end=monday + timedelta(days=6),
                    discretionary_spending=sum(
                        (item.discretionary_spending or _ZERO for item in observations),
                        start=_ZERO,
                    ),
                    known_recurring_outflow=recurring_by_week.get(monday, _ZERO),
                    known_at=max(availability),
                )
            )
        monday += timedelta(days=7)
    return tuple(targets)


def build_forecast_feature_rows(
    targets: tuple[WeeklyForecastTarget, ...], payday_days: tuple[int, ...]
) -> tuple[ForecastFeatureRow, ...]:
    """Build features only when the preceding eight target weeks are consecutive."""
    rows: list[ForecastFeatureRow] = []
    for index in range(8, len(targets)):
        history = targets[index - 8 : index]
        current = targets[index]
        forecast_origin = datetime.combine(current.week_start, time.min, tzinfo=UTC)
        expected_dates = tuple(
            current.week_start - timedelta(days=7 * offset)
            for offset in range(8, 0, -1)
        )
        if tuple(item.week_start for item in history) != expected_dates or any(
            item.known_at >= forecast_origin for item in history
        ):
            continue
        values = tuple(item.discretionary_spending for item in history)
        since, until = _payday_distances(current.week_start, payday_days)
        rows.append(
            ForecastFeatureRow(
                week_start=current.week_start,
                forecast_origin_at=forecast_origin,
                target=current.discretionary_spending,
                lag_1=values[-1],
                lag_2=values[-2],
                lag_4=values[-4],
                rolling_mean_4=sum(values[-4:], start=_ZERO) / 4,
                rolling_mean_8=sum(values, start=_ZERO) / 8,
                days_since_payday=since,
                days_until_payday=until,
                month=current.week_start.month,
                week_of_year=current.week_start.isocalendar().week,
                known_recurring_outflow=current.known_recurring_outflow,
                target_known_at=current.known_at,
            )
        )
    return tuple(rows)


def validate_forecast_dataset(dataset: ForecastDataset) -> None:
    """Reject unordered, forged, or inconsistently derived forecast input."""
    weekly_dates = tuple(item.week_start for item in dataset.weekly_targets)
    row_dates = tuple(item.week_start for item in dataset.feature_rows)
    if any(later <= earlier for earlier, later in pairwise(weekly_dates)):
        raise ForecastingDataError(
            ForecastingDataErrorCode.INVALID_EVALUATION_POLICY,
            "weekly forecast targets must be strictly chronological",
        )
    if any(later <= earlier for earlier, later in pairwise(row_dates)):
        raise ForecastingDataError(
            ForecastingDataErrorCode.INVALID_EVALUATION_POLICY,
            "forecast feature rows must be strictly chronological",
        )
    by_week = {item.week_start: item for item in dataset.weekly_targets}
    if any(
        (target := by_week.get(row.week_start)) is None
        or target.discretionary_spending != row.target
        or target.known_recurring_outflow != row.known_recurring_outflow
        for row in dataset.feature_rows
    ):
        raise ForecastingDataError(
            ForecastingDataErrorCode.INVALID_EVALUATION_POLICY,
            "forecast feature rows must align with their weekly targets",
        )
    expected_rows = build_forecast_feature_rows(
        dataset.weekly_targets, dataset.plan.payday_days
    )
    if dataset.feature_rows != expected_rows:
        raise ForecastingDataError(
            ForecastingDataErrorCode.INVALID_EVALUATION_POLICY,
            "forecast features must be derived from the supplied past weekly targets",
        )


def build_next_forecast_inference_row(dataset: ForecastDataset) -> ForecastInferenceRow:
    """Build target-free features for the next Monday from known weekly history."""
    if len(dataset.weekly_targets) < 8:
        raise ForecastingDataError(
            ForecastingDataErrorCode.TOO_FEW_COMPLETE_WEEKS,
            "eight consecutive known weeks are required for next-week inference",
        )
    history = dataset.weekly_targets[-8:]
    if not all(
        current.week_start - previous.week_start == timedelta(weeks=1)
        for previous, current in pairwise(history)
    ):
        raise ForecastingDataError(
            ForecastingDataErrorCode.TOO_FEW_COMPLETE_WEEKS,
            "the latest eight known weeks must be consecutive for inference",
        )
    week_start = history[-1].week_start + timedelta(weeks=1)
    forecast_origin = datetime.combine(week_start, time.min, tzinfo=UTC)
    if any(item.known_at >= forecast_origin for item in history):
        raise ForecastingDataError(
            ForecastingDataErrorCode.TOO_FEW_COMPLETE_WEEKS,
            "the latest eight weekly outcomes must be known before forecast origin",
        )
    recurring = dataset.next_recurring_outflow
    if recurring is None or recurring.week_start != week_start:
        raise ForecastingDataError(
            ForecastingDataErrorCode.INVALID_EVALUATION_POLICY,
            "next-week recurring outflow requires cutoff-bound projection evidence",
        )
    if recurring.known_at >= forecast_origin:
        raise ForecastingDataError(
            ForecastingDataErrorCode.INVALID_EVALUATION_POLICY,
            "recurring outflow must be known before the forecast origin",
        )
    values = tuple(item.discretionary_spending for item in history)
    since, until = _payday_distances(week_start, dataset.plan.payday_days)
    return ForecastInferenceRow(
        week_start=week_start,
        forecast_origin_at=forecast_origin,
        lag_1=values[-1],
        lag_2=values[-2],
        lag_4=values[-4],
        rolling_mean_4=sum(values[-4:], start=_ZERO) / 4,
        rolling_mean_8=sum(values, start=_ZERO) / 8,
        days_since_payday=since,
        days_until_payday=until,
        month=week_start.month,
        week_of_year=week_start.isocalendar().week,
        known_recurring_outflow=recurring.amount,
        recurring_outflow_known_at=recurring.known_at,
    )


def build_forecast_dataset(
    factory: sessionmaker[Session], *, plan: ForecastDatasetPlan
) -> ForecastDataset:
    """Build a read-only calendar where unknown dates remain null and break lags."""
    with session_scope(factory) as session:
        if session.get(UserProfileRecord, plan.user_profile_id) is None:
            raise ForecastingDataError(
                ForecastingDataErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        accounts = tuple(
            session.scalars(
                select(AccountRecord)
                .where(
                    AccountRecord.user_profile_id == plan.user_profile_id,
                    AccountRecord.id.in_(plan.account_ids),
                )
                .order_by(AccountRecord.id)
            )
        )
        if {item.id for item in accounts} != set(plan.account_ids):
            raise ForecastingDataError(
                ForecastingDataErrorCode.ACCOUNT_SCOPE_NOT_FOUND,
                "one or more forecast accounts are unavailable to this profile",
            )
        coverage_maps = tuple(
            _coverage_dates(
                session,
                item.id,
                cutoff=plan.period.end_date,
                knowledge_cutoff_at=plan.knowledge_cutoff_at,
            )
            for item in accounts
        )
        covered_dates = set.intersection(*(set(item) for item in coverage_maps))
        coverage_available_at = {
            value: max(item[value] for item in coverage_maps) for value in covered_dates
        }
        complete_day_available_at = {
            value: datetime.combine(value, time.max, tzinfo=UTC)
            for value in covered_dates
            if datetime.combine(value, time.max, tzinfo=UTC) <= plan.knowledge_cutoff_at
        }
        recurring_rows = _confirmed_expense_candidates(
            session,
            plan.account_ids,
            knowledge_cutoff_at=plan.knowledge_cutoff_at,
        )
        recurring_candidate_ids = tuple(
            candidate.id for _series, candidate in recurring_rows
        )
        candidate_available_at = {
            candidate.id: max(
                series.created_at,
                candidate.reviewed_at,
                candidate.knowledge_cutoff_at,
            )
            for series, candidate in recurring_rows
            if candidate.reviewed_at is not None
        }
        member_evidence = _recurrence_member_dates(
            session,
            recurring_candidate_ids,
            knowledge_cutoff_at=plan.knowledge_cutoff_at,
        )
        recurring_member_known_at: dict[str, datetime] = {}
        for candidate_id, evidence_items in member_evidence.items():
            for evidence in evidence_items:
                available_at = max(
                    evidence.known_at,
                    candidate_available_at[candidate_id],
                )
                recurring_member_known_at[evidence.transaction_id] = min(
                    available_at,
                    recurring_member_known_at.get(
                        evidence.transaction_id, available_at
                    ),
                )
        recurring_member_ids = frozenset(recurring_member_known_at)
        candidate_rows = tuple(
            session.execute(
                select(
                    VerifiedTransactionRecord,
                    ImportBatchRecord.imported_at,
                    RawTransactionRecord.created_at,
                )
                .join(
                    RawTransactionRecord,
                    RawTransactionRecord.id
                    == VerifiedTransactionRecord.raw_transaction_id,
                )
                .join(
                    ImportBatchRecord,
                    ImportBatchRecord.id == RawTransactionRecord.import_batch_id,
                )
                .where(
                    VerifiedTransactionRecord.account_id.in_(plan.account_ids),
                    ImportBatchRecord.account_id
                    == VerifiedTransactionRecord.account_id,
                    VerifiedTransactionRecord.transaction_date.between(
                        plan.period.start_date, plan.period.end_date
                    ),
                    VerifiedTransactionRecord.verified_at <= plan.knowledge_cutoff_at,
                    ImportBatchRecord.imported_at <= plan.knowledge_cutoff_at,
                    RawTransactionRecord.created_at <= plan.knowledge_cutoff_at,
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
                            ImportBatchRecord.source_type.in_(
                                ("digital_pdf", "ocr_pdf")
                            ),
                            ImportBatchRecord.verification_status == "verified",
                        ),
                    ),
                )
                .order_by(
                    VerifiedTransactionRecord.transaction_date,
                    VerifiedTransactionRecord.id,
                )
            ).tuples()
        )
        candidates = tuple(
            item for item, _imported_at, _raw_created_at in candidate_rows
        )
        source_available_at = {
            item.id: max(imported_at, raw_created_at)
            for item, imported_at, raw_created_at in candidate_rows
        }
        roles = _financial_roles_as_of(
            session,
            tuple(item.id for item in candidates),
            knowledge_cutoff_at=plan.knowledge_cutoff_at,
        )
        transactions = tuple(
            item
            for item in candidates
            if (role_evidence := roles.get(item.id)) is not None
            and role_evidence[0] == "expense"
            and item.id not in recurring_member_ids
        )
        unresolved_dates = {
            item.transaction_date
            for item in candidates
            if roles.get(item.id, ("unknown", item.verified_at))[0] == "unknown"
        }
        transaction_available_at: dict[date, datetime] = {}
        for transaction in candidates:
            role_evidence = roles.get(transaction.id)
            if role_evidence is None:
                continue
            available_at = max(
                transaction.verified_at,
                role_evidence[1],
                source_available_at[transaction.id],
            )
            member_available_at = recurring_member_known_at.get(transaction.id)
            if member_available_at is not None:
                available_at = max(available_at, member_available_at)
            existing = transaction_available_at.get(transaction.transaction_date)
            if existing is None or available_at > existing:
                transaction_available_at[transaction.transaction_date] = available_at
        spend: dict[date, Decimal] = defaultdict(lambda: _ZERO)
        counts: dict[date, int] = defaultdict(int)
        for transaction in transactions:
            spend[transaction.transaction_date] += -transaction.amount
            counts[transaction.transaction_date] += 1
        daily = tuple(
            DailyForecastObservation(
                observation_date=value,
                status=(
                    ForecastDayStatus.COVERED
                    if value in complete_day_available_at
                    and value not in unresolved_dates
                    else ForecastDayStatus.UNKNOWN
                ),
                discretionary_spending=(
                    spend[value]
                    if value in complete_day_available_at
                    and value not in unresolved_dates
                    else None
                ),
                transaction_count=(
                    counts[value]
                    if value in complete_day_available_at
                    and value not in unresolved_dates
                    else None
                ),
                known_at=(
                    max(
                        coverage_available_at[value],
                        complete_day_available_at[value],
                        transaction_available_at.get(
                            value, coverage_available_at[value]
                        ),
                    )
                    if value in complete_day_available_at
                    and value not in unresolved_dates
                    else None
                ),
            )
            for value in _dates(plan.period.start_date, plan.period.end_date)
        )
        recurring_by_week: dict[date, Decimal] = defaultdict(lambda: _ZERO)
        recurring_evidence_by_week: dict[date, list[datetime]] = defaultdict(list)
        for series, candidate in recurring_rows:
            assert candidate.reviewed_at is not None
            recurrence_available_at = candidate_available_at[candidate.id]
            confirmed_evidence = tuple(
                item
                for item in member_evidence.get(candidate.id, ())
                if item.known_at <= recurrence_available_at
            )
            if not confirmed_evidence:
                continue
            projection_start = max(
                plan.period.start_date,
                recurrence_available_at.date(),
            )
            # Anchor the schedule to evidence that existed when the series was
            # confirmed. Starting from the newest member would let a later refresh
            # erase earlier historical projections from a backtest.
            occurrence = _advance_recurrence(
                confirmed_evidence[0].transaction_date,
                candidate.frequency,
            )
            while occurrence < projection_start:
                occurrence = _advance_recurrence(occurrence, candidate.frequency)
            while occurrence <= plan.period.end_date + timedelta(weeks=1):
                week = occurrence - timedelta(days=occurrence.weekday())
                forecast_origin = datetime.combine(week, time.min, tzinfo=UTC)
                if recurrence_available_at < forecast_origin:
                    recurring_by_week[week] += abs(series.expected_amount or _ZERO)
                    recurring_evidence_by_week[week].append(recurrence_available_at)
                occurrence = _advance_recurrence(occurrence, candidate.frequency)
        weekly = _weekly_targets(daily, recurring_by_week)
        next_recurring_outflow = None
        if weekly:
            next_week = weekly[-1].week_start + timedelta(weeks=1)
            next_recurring_outflow = RecurringOutflowProjection(
                week_start=next_week,
                amount=recurring_by_week.get(next_week, _ZERO),
                known_at=max(
                    recurring_evidence_by_week.get(
                        next_week, [plan.knowledge_cutoff_at]
                    )
                ),
            )
        return ForecastDataset(
            plan=plan,
            daily_calendar=daily,
            weekly_targets=weekly,
            feature_rows=build_forecast_feature_rows(weekly, plan.payday_days),
            next_recurring_outflow=next_recurring_outflow,
        )


def _metrics(
    name: ForecastBaselineName,
    actual: tuple[Decimal, ...],
    predicted: tuple[Decimal, ...],
) -> BaselineMetrics:
    errors = tuple(
        prediction - truth for prediction, truth in zip(predicted, actual, strict=True)
    )
    mae = sum((abs(item) for item in errors), start=_ZERO) / len(errors)
    mse = sum((item * item for item in errors), start=_ZERO) / len(errors)
    return BaselineMetrics(
        baseline=name,
        mae=mae,
        rmse=Decimal(str(math.sqrt(float(mse)))),
        bias=sum(errors, start=_ZERO) / len(errors),
        predictions=predicted,
    )


def _baseline_metrics_for_slice(
    rows: tuple[ForecastFeatureRow, ...],
    *,
    start: int,
    end: int,
    targets_by_week: dict[date, WeeklyForecastTarget],
) -> tuple[BaselineMetrics, ...]:
    """Evaluate every baseline as rolling one-step-ahead with revealed history."""
    actual = tuple(item.target for item in rows[start:end])
    predictions: dict[ForecastBaselineName, list[Decimal]] = {
        name: [] for name in ForecastBaselineName
    }
    for index in range(start, end):
        current = rows[index]
        target_history = tuple(
            item
            for item in targets_by_week.values()
            if item.week_start < current.week_start
            and item.known_at < current.forecast_origin_at
        )
        historical = sum(
            (item.discretionary_spending for item in target_history), start=_ZERO
        ) / len(target_history)
        recent = current.rolling_mean_4
        seasonal_target = targets_by_week.get(current.week_start - timedelta(weeks=52))
        seasonal = (
            seasonal_target.discretionary_spending
            if seasonal_target is not None
            and seasonal_target.known_at < current.forecast_origin_at
            else historical
        )
        predictions[ForecastBaselineName.HISTORICAL_MEAN].append(historical)
        predictions[ForecastBaselineName.RECENT_ROLLING_MEAN].append(recent)
        predictions[ForecastBaselineName.SEASONAL_NAIVE].append(seasonal)
        predictions[ForecastBaselineName.RECURRING_ONLY].append(_ZERO)
        predictions[ForecastBaselineName.ZERO_DISCRETIONARY].append(_ZERO)
    return tuple(
        _metrics(name, actual, tuple(predictions[name]))
        for name in ForecastBaselineName
    )


def _are_consecutive(rows: tuple[ForecastFeatureRow, ...]) -> bool:
    return all(
        current.week_start - previous.week_start == timedelta(weeks=1)
        for previous, current in pairwise(rows)
    )


def evaluate_forecast_baselines(
    dataset: ForecastDataset,
    *,
    initial_training_weeks: int,
    final_test_weeks: int,
) -> ForecastBaselineEvaluation:
    """Evaluate required baselines on future complete weeks without shuffling."""
    validate_forecast_dataset(dataset)
    rows = dataset.feature_rows
    if initial_training_weeks < 1 or final_test_weeks < 1:
        raise ForecastingDataError(
            ForecastingDataErrorCode.INVALID_EVALUATION_POLICY,
            "training and final test sizes must be positive",
        )
    if len(rows) <= initial_training_weeks + final_test_weeks:
        raise ForecastingDataError(
            ForecastingDataErrorCode.TOO_FEW_COMPLETE_WEEKS,
            "not enough consecutive fully covered weeks for evaluation",
        )
    split = len(rows) - final_test_weeks
    training, testing = rows[:split], rows[split:]
    if not _are_consecutive(testing):
        raise ForecastingDataError(
            ForecastingDataErrorCode.INVALID_EVALUATION_POLICY,
            "final test weeks must be consecutive and fully covered",
        )
    folds = tuple(
        ExpandingWindowFold(
            training_week_starts=tuple(
                item.week_start
                for item in rows[:index]
                if item.target_known_at < rows[index].forecast_origin_at
            ),
            test_week_starts=(rows[index].week_start,),
        )
        for index in range(initial_training_weeks, split)
        if sum(
            item.target_known_at < rows[index].forecast_origin_at
            for item in rows[:index]
        )
        >= initial_training_weeks
    )
    if not folds:
        raise ForecastingDataError(
            ForecastingDataErrorCode.TOO_FEW_COMPLETE_WEEKS,
            "an independent expanding validation week is required before final test",
        )
    targets_by_week = {item.week_start: item for item in dataset.weekly_targets}
    metrics = _baseline_metrics_for_slice(
        rows,
        start=split,
        end=len(rows),
        targets_by_week=targets_by_week,
    )
    expanding_start = next(
        index
        for index in range(initial_training_weeks, split)
        if sum(
            item.target_known_at < rows[index].forecast_origin_at
            for item in rows[:index]
        )
        >= initial_training_weeks
    )
    expanding_end = split
    expanding_metrics = _baseline_metrics_for_slice(
        rows,
        start=expanding_start,
        end=expanding_end,
        targets_by_week=targets_by_week,
    )
    return ForecastBaselineEvaluation(
        final_training_week_starts=tuple(
            item.week_start
            for item in training
            if item.target_known_at < testing[0].forecast_origin_at
        ),
        final_test_week_starts=tuple(item.week_start for item in testing),
        expanding_folds=folds,
        metrics=metrics,
        expanding_metrics=expanding_metrics,
    )
