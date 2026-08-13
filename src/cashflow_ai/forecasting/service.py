"""Leakage-safe daily/weekly forecasting data and simple baselines."""

from __future__ import annotations

import calendar
import math
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from statistics import mean

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

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
    WeeklyForecastTarget,
)

_ZERO = Decimal("0.00")


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
    session: Session, account_id: str, *, cutoff: date
) -> frozenset[date]:
    statement = (
        select(StatementCoverageRecord)
        .join(ImportContextRecord)
        .join(ImportBatchRecord)
        .where(
            ImportBatchRecord.account_id == account_id,
            ImportBatchRecord.verification_status == "verified",
            StatementCoverageRecord.statement_start_date <= cutoff,
        )
    )
    covered: set[date] = set()
    for record in session.scalars(statement):
        if record.coverage_status in {"partial", "unknown"}:
            continue
        end = min(record.statement_end_date, cutoff)
        covered.update(_dates(record.statement_start_date, end))
        if record.coverage_status == "gapped":
            for missing in record.missing_periods_json:
                gap_start = date.fromisoformat(str(missing["start_date"]))
                gap_end = min(date.fromisoformat(str(missing["end_date"])), cutoff)
                if gap_end >= gap_start:
                    covered.difference_update(_dates(gap_start, gap_end))
    return frozenset(covered)


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
            targets.append(
                WeeklyForecastTarget(
                    week_start=monday,
                    week_end=monday + timedelta(days=6),
                    discretionary_spending=sum(
                        (item.discretionary_spending or _ZERO for item in observations),
                        start=_ZERO,
                    ),
                    known_recurring_outflow=recurring_by_week.get(monday, _ZERO),
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
        expected_dates = tuple(
            current.week_start - timedelta(days=7 * offset)
            for offset in range(8, 0, -1)
        )
        if tuple(item.week_start for item in history) != expected_dates:
            continue
        values = tuple(item.discretionary_spending for item in history)
        since, until = _payday_distances(current.week_start, payday_days)
        rows.append(
            ForecastFeatureRow(
                week_start=current.week_start,
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
            )
        )
    return tuple(rows)


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
        coverage_sets = tuple(
            _coverage_dates(session, item.id, cutoff=plan.period.end_date)
            for item in accounts
        )
        covered = set.intersection(*(set(item) for item in coverage_sets))
        recurring_member = (
            exists()
            .where(
                RecurringPaymentMemberRecord.verified_transaction_id
                == VerifiedTransactionRecord.id,
                RecurringPaymentMemberRecord.candidate_id
                == RecurringPaymentCandidateRecord.id,
                RecurringPaymentCandidateRecord.status == "confirmed",
            )
            .correlate(VerifiedTransactionRecord)
        )
        transactions = tuple(
            session.scalars(
                select(VerifiedTransactionRecord)
                .where(
                    VerifiedTransactionRecord.account_id.in_(plan.account_ids),
                    VerifiedTransactionRecord.transaction_date.between(
                        plan.period.start_date, plan.period.end_date
                    ),
                    VerifiedTransactionRecord.verified_at <= plan.knowledge_cutoff_at,
                    VerifiedTransactionRecord.financial_role_id == "expense",
                    ~recurring_member,
                )
                .order_by(VerifiedTransactionRecord.transaction_date)
            )
        )
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
                    if value in covered
                    else ForecastDayStatus.UNKNOWN
                ),
                discretionary_spending=spend[value] if value in covered else None,
                transaction_count=counts[value] if value in covered else None,
            )
            for value in _dates(plan.period.start_date, plan.period.end_date)
        )
        recurring_by_week: dict[date, Decimal] = defaultdict(lambda: _ZERO)
        recurring_rows = session.execute(
            select(RecurringSeriesRecord, RecurringPaymentCandidateRecord)
            .join(
                RecurringPaymentCandidateRecord,
                RecurringPaymentCandidateRecord.recurring_series_id
                == RecurringSeriesRecord.id,
            )
            .where(
                RecurringSeriesRecord.account_id.in_(plan.account_ids),
                RecurringSeriesRecord.is_active.is_(True),
                RecurringSeriesRecord.created_at <= plan.knowledge_cutoff_at,
                RecurringPaymentCandidateRecord.status == "confirmed",
            )
        )
        for series, candidate in recurring_rows:
            occurrence = candidate.next_expected_date
            if plan.period.start_date <= occurrence <= plan.period.end_date:
                week = occurrence - timedelta(days=occurrence.weekday())
                recurring_by_week[week] += abs(series.expected_amount or _ZERO)
        weekly = _weekly_targets(daily, recurring_by_week)
        return ForecastDataset(
            plan=plan,
            daily_calendar=daily,
            weekly_targets=weekly,
            feature_rows=build_forecast_feature_rows(weekly, plan.payday_days),
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


def evaluate_forecast_baselines(
    dataset: ForecastDataset,
    *,
    initial_training_weeks: int,
    final_test_weeks: int,
) -> ForecastBaselineEvaluation:
    """Evaluate required baselines on future complete weeks without shuffling."""
    rows = dataset.feature_rows
    if initial_training_weeks < 1 or final_test_weeks < 1:
        raise ForecastingDataError(
            ForecastingDataErrorCode.INVALID_EVALUATION_POLICY,
            "training and final test sizes must be positive",
        )
    if len(rows) < initial_training_weeks + final_test_weeks:
        raise ForecastingDataError(
            ForecastingDataErrorCode.TOO_FEW_COMPLETE_WEEKS,
            "not enough consecutive fully covered weeks for evaluation",
        )
    split = len(rows) - final_test_weeks
    training, testing = rows[:split], rows[split:]
    folds = tuple(
        ExpandingWindowFold(
            training_week_starts=tuple(item.week_start for item in rows[:index]),
            test_week_starts=(rows[index].week_start,),
        )
        for index in range(initial_training_weeks, split)
    )
    if not folds:
        folds = (
            ExpandingWindowFold(
                training_week_starts=tuple(item.week_start for item in training),
                test_week_starts=tuple(item.week_start for item in testing),
            ),
        )
    actual = tuple(item.target for item in testing)
    historical = Decimal(str(mean(item.target for item in training)))
    recent = Decimal(str(mean(item.target for item in training[-4:])))
    by_week = {item.week_start: item.target for item in rows}
    seasonal = tuple(
        by_week.get(item.week_start - timedelta(weeks=52), historical)
        for item in testing
    )
    zeros = tuple(_ZERO for _item in testing)
    metrics = (
        _metrics(
            ForecastBaselineName.HISTORICAL_MEAN,
            actual,
            tuple(historical for _item in testing),
        ),
        _metrics(
            ForecastBaselineName.RECENT_ROLLING_MEAN,
            actual,
            tuple(recent for _item in testing),
        ),
        _metrics(ForecastBaselineName.SEASONAL_NAIVE, actual, seasonal),
        _metrics(ForecastBaselineName.RECURRING_ONLY, actual, zeros),
        _metrics(ForecastBaselineName.ZERO_DISCRETIONARY, actual, zeros),
    )
    return ForecastBaselineEvaluation(
        final_training_week_starts=tuple(item.week_start for item in training),
        final_test_week_starts=tuple(item.week_start for item in testing),
        expanding_folds=folds,
        metrics=metrics,
    )
