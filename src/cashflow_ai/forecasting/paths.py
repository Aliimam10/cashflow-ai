"""Residual-bootstrap uncertainty and daily balance-path construction."""

from __future__ import annotations

import calendar
import math
import random
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import case, desc, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.forecasting.model import (
    TrainedPrimaryForecaster,
    predict_discretionary_spending,
)
from cashflow_ai.forecasting.service import build_next_forecast_inference_row
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    BalanceSnapshotRecord,
    RecurringPaymentCandidateRecord,
    RecurringSeriesRecord,
)
from cashflow_ai.schemas.forecast_models import (
    ForecastInferenceRow,
    feature_vector,
)
from cashflow_ai.schemas.forecast_paths import (
    BalanceForecastPath,
    DailyBalancePathPoint,
    ForecastIntervalMethod,
    ForecastIntervalPerformance,
    ForecastOpeningBalance,
    ForecastPathPlan,
    ForecastPathWarningCode,
    ForecastScenario,
    RecurringForecastOccurrence,
    WeeklySpendingPath,
)
from cashflow_ai.schemas.forecasting import (
    ForecastBaselineName,
    ForecastDataset,
    ForecastDayStatus,
)
from cashflow_ai.schemas.freshness import (
    FinancialDataFreshness,
    FinancialDataMode,
    FreshnessWarningCode,
    VerifiedBalanceEvidence,
)
from cashflow_ai.schemas.money import MONEY_QUANTUM
from cashflow_ai.schemas.statements import BalanceSnapshotSource, DateRange
from cashflow_ai.schemas.transactions import Currency, FinancialRole

_ZERO = Decimal("0.00")
_ONE = Decimal("1")


class ForecastPathErrorCode(StrEnum):
    """Stable privacy-safe balance-path failures."""

    ACCOUNT_NOT_FOUND = "account_not_found"
    ACCOUNT_SCOPE_MISMATCH = "account_scope_mismatch"
    BALANCE_NOT_FOUND = "balance_not_found"
    FORECAST_EVIDENCE_MISALIGNED = "forecast_evidence_misaligned"
    SCENARIO_OUTSIDE_HORIZON = "scenario_outside_horizon"


class ForecastPathError(ValueError):
    """Controlled path-generation failure without private financial text."""

    def __init__(self, code: ForecastPathErrorCode, message: str) -> None:
        """Store a stable error code with a privacy-safe explanation."""
        super().__init__(message)
        self.code = code


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _quantile(values: tuple[Decimal, ...], probability: Decimal) -> Decimal:
    """Return a deterministic linearly interpolated sample quantile."""
    ordered = tuple(sorted(values))
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return (
        ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction
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
    month_end = calendar.monthrange(year, month)[1]
    day = (
        month_end
        if value.day == calendar.monthrange(value.year, value.month)[1]
        else min(value.day, month_end)
    )
    return date(year, month, day)


def _latest_balance(
    session: Session, *, account_id: str, as_of_date: date, cutoff: datetime
) -> BalanceSnapshotRecord | None:
    source_priority = case(
        (BalanceSnapshotRecord.source == "manual", 4),
        (BalanceSnapshotRecord.source == "statement_closing", 3),
        (BalanceSnapshotRecord.source == "running_balance", 2),
        (BalanceSnapshotRecord.source == "statement_opening", 1),
        else_=0,
    )
    return session.scalar(
        select(BalanceSnapshotRecord)
        .where(
            BalanceSnapshotRecord.account_id == account_id,
            BalanceSnapshotRecord.verification_status == "verified",
            BalanceSnapshotRecord.as_of_date <= as_of_date,
            BalanceSnapshotRecord.recorded_at <= cutoff,
        )
        .order_by(
            desc(BalanceSnapshotRecord.as_of_date),
            desc(source_priority),
            desc(BalanceSnapshotRecord.recorded_at),
            desc(BalanceSnapshotRecord.id),
        )
        .limit(1)
    )


def _coverage_segments(
    dataset: ForecastDataset, as_of_date: date
) -> tuple[DateRange, ...]:
    covered = tuple(
        item.observation_date
        for item in dataset.daily_calendar
        if item.observation_date <= as_of_date
        and item.status is ForecastDayStatus.COVERED
        and item.known_at is not None
        and item.known_at <= dataset.plan.knowledge_cutoff_at
    )
    if not covered:
        return ()
    segments: list[DateRange] = []
    start = previous = covered[0]
    for value in covered[1:]:
        if value != previous + timedelta(days=1):
            segments.append(DateRange(start_date=start, end_date=previous))
            start = value
        previous = value
    segments.append(DateRange(start_date=start, end_date=previous))
    return tuple(segments)


def _forecast_freshness(
    *,
    account: AccountRecord,
    balance: BalanceSnapshotRecord,
    dataset: ForecastDataset,
    plan: ForecastPathPlan,
) -> FinancialDataFreshness:
    assessed_on = plan.forecast_start - timedelta(days=1)
    transaction_dates = tuple(
        item.observation_date
        for item in dataset.daily_calendar
        if item.observation_date <= assessed_on
        and item.status is ForecastDayStatus.COVERED
        and item.transaction_count is not None
        and item.transaction_count > 0
        and item.known_at is not None
        and item.known_at <= plan.knowledge_cutoff_at
    )
    latest_transaction = max(transaction_dates, default=None)
    transaction_age = (
        (assessed_on - latest_transaction).days
        if latest_transaction is not None
        else None
    )
    balance_age = (assessed_on - balance.as_of_date).days
    segments = _coverage_segments(dataset, assessed_on)
    latest_coverage = max(
        segments,
        key=lambda item: (item.end_date, item.start_date),
        default=None,
    )
    coverage_days = (
        (latest_coverage.end_date - latest_coverage.start_date).days + 1
        if latest_coverage is not None
        else 0
    )
    coverage_age = (
        (assessed_on - latest_coverage.end_date).days
        if latest_coverage is not None
        else None
    )
    policy = plan.policy.freshness
    warnings: list[FreshnessWarningCode] = []
    if not account.is_active:
        warnings.append(FreshnessWarningCode.ACCOUNT_INACTIVE)
    if transaction_age is None:
        warnings.append(FreshnessWarningCode.NO_VERIFIED_TRANSACTIONS)
    elif transaction_age > policy.max_transaction_age_days:
        warnings.append(FreshnessWarningCode.TRANSACTIONS_STALE)
    if balance_age > policy.max_balance_age_days:
        warnings.append(FreshnessWarningCode.BALANCE_STALE)
    if latest_coverage is None:
        warnings.append(FreshnessWarningCode.NO_VERIFIED_COVERAGE)
    else:
        if coverage_age is not None and coverage_age > policy.max_coverage_age_days:
            warnings.append(FreshnessWarningCode.COVERAGE_STALE)
        if coverage_days < policy.minimum_contiguous_coverage_days:
            warnings.append(FreshnessWarningCode.INSUFFICIENT_CONTIGUOUS_COVERAGE)
        if latest_transaction is not None and not (
            latest_coverage.start_date <= latest_transaction <= latest_coverage.end_date
        ):
            warnings.append(
                FreshnessWarningCode.LATEST_TRANSACTION_OUTSIDE_CONTIGUOUS_COVERAGE
            )
    evidence_ages = tuple(
        value for value in (transaction_age, balance_age) if value is not None
    )
    return FinancialDataFreshness(
        account_id=plan.account_id,
        assessed_on=assessed_on,
        mode=(
            FinancialDataMode.ARCHIVE
            if warnings
            else FinancialDataMode.ACTIVE_FORECASTING
        ),
        latest_transaction_date=latest_transaction,
        latest_verified_balance=VerifiedBalanceEvidence(
            balance=balance.balance,
            currency=Currency(balance.currency),
            as_of_date=balance.as_of_date,
            recorded_at=balance.recorded_at,
            source=BalanceSnapshotSource(balance.source),
        ),
        transaction_age_days=transaction_age,
        balance_age_days=balance_age,
        data_freshness_days=min(evidence_ages, default=None),
        latest_contiguous_coverage=latest_coverage,
        contiguous_coverage_days=coverage_days,
        coverage_age_days=coverage_age,
        warnings=tuple(warnings),
    )


def _recurring_occurrences(
    session: Session, *, plan: ForecastPathPlan
) -> tuple[RecurringForecastOccurrence, ...]:
    end_date = plan.forecast_start + timedelta(days=plan.horizon_days - 1)
    statement = (
        select(RecurringPaymentCandidateRecord, RecurringSeriesRecord)
        .join(
            RecurringSeriesRecord,
            RecurringSeriesRecord.id
            == RecurringPaymentCandidateRecord.recurring_series_id,
        )
        .where(
            RecurringPaymentCandidateRecord.account_id == plan.account_id,
            RecurringPaymentCandidateRecord.status == "confirmed",
            RecurringPaymentCandidateRecord.reviewed_at.is_not(None),
            RecurringPaymentCandidateRecord.reviewed_at <= plan.knowledge_cutoff_at,
            RecurringPaymentCandidateRecord.detected_at <= plan.knowledge_cutoff_at,
            RecurringPaymentCandidateRecord.knowledge_cutoff_at
            <= plan.knowledge_cutoff_at,
            RecurringSeriesRecord.account_id == plan.account_id,
            RecurringSeriesRecord.is_active.is_(True),
            RecurringSeriesRecord.created_at <= plan.knowledge_cutoff_at,
        )
        .order_by(
            RecurringPaymentCandidateRecord.next_expected_date,
            RecurringPaymentCandidateRecord.id,
        )
    )
    results: list[RecurringForecastOccurrence] = []
    allowed_roles = {
        FinancialRole.INCOME,
        FinancialRole.EXPENSE,
        FinancialRole.REFUND,
        FinancialRole.REIMBURSEMENT,
        FinancialRole.CASH_WITHDRAWAL,
    }
    for candidate, series in session.execute(statement).tuples():
        role = FinancialRole(candidate.financial_role_id)
        if role not in allowed_roles:
            continue
        known_at = max(
            candidate.detected_at,
            candidate.knowledge_cutoff_at,
            cast(datetime, candidate.reviewed_at),
            series.created_at,
        )
        occurrence_date = candidate.next_expected_date
        while occurrence_date < plan.forecast_start:
            occurrence_date = _advance_recurrence(occurrence_date, candidate.frequency)
        while occurrence_date <= end_date:
            results.append(
                RecurringForecastOccurrence(
                    candidate_id=candidate.id,
                    occurrence_date=occurrence_date,
                    signed_amount=candidate.expected_amount,
                    financial_role=role,
                    known_at=known_at,
                )
            )
            occurrence_date = _advance_recurrence(occurrence_date, candidate.frequency)
    return tuple(
        sorted(results, key=lambda item: (item.occurrence_date, item.candidate_id))
    )


def _payday_distances(value: date, payday_days: tuple[int, ...]) -> tuple[int, int]:
    candidates: list[date] = []
    for month_offset in (-1, 0, 1):
        month_index = value.month - 1 + month_offset
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        candidates.extend(date(year, month, day) for day in payday_days)
    previous = max(item for item in candidates if item <= value)
    following = min(item for item in candidates if item >= value)
    return (value - previous).days, (following - value).days


def _baseline_value(
    trained: TrainedPrimaryForecaster,
    row: ForecastInferenceRow,
    history: tuple[tuple[date, Decimal], ...],
) -> Decimal:
    selected = trained.comparison.selected_model
    values = tuple(item[1] for item in history)
    historical_mean = sum(values, start=_ZERO) / len(values) if values else _ZERO
    if selected is ForecastBaselineName.RECENT_ROLLING_MEAN:
        return row.rolling_mean_4
    if selected is ForecastBaselineName.HISTORICAL_MEAN:
        return historical_mean
    if selected is ForecastBaselineName.SEASONAL_NAIVE:
        seasonal_week = row.week_start - timedelta(weeks=52)
        return next(
            (amount for week, amount in history if week == seasonal_week),
            historical_mean,
        )
    return _ZERO


def _recursive_prediction(
    trained: TrainedPrimaryForecaster,
    row: ForecastInferenceRow,
    history: tuple[tuple[date, Decimal], ...],
) -> Decimal:
    if trained.estimator is None:
        return _money(max(_ZERO, _baseline_value(trained, row, history)))
    raw = cast(
        Any,
        trained.estimator.predict([list(feature_vector(row))]),
    )[0]
    return _money(max(_ZERO, Decimal(str(float(raw)))))


def _weekly_point_predictions(
    *,
    dataset: ForecastDataset,
    trained: TrainedPrimaryForecaster,
    plan: ForecastPathPlan,
    occurrences: tuple[RecurringForecastOccurrence, ...],
) -> tuple[tuple[date, Decimal], ...]:
    first_row = build_next_forecast_inference_row(dataset)
    if first_row.week_start != plan.forecast_start:
        raise ForecastPathError(
            ForecastPathErrorCode.FORECAST_EVIDENCE_MISALIGNED,
            "forecast start must be the next unobserved model week",
        )
    first = predict_discretionary_spending(trained, first_row)
    week_count = math.ceil(plan.horizon_days / 7)
    points: list[tuple[date, Decimal]] = [
        (first.week_start, first.discretionary_spending)
    ]
    history: list[tuple[date, Decimal]] = [
        (week, amount) for week, amount, _known_at in trained.target_history
    ]
    history.append(points[0])
    expense_roles = {FinancialRole.EXPENSE, FinancialRole.CASH_WITHDRAWAL}
    for index in range(1, week_count):
        week_start = plan.forecast_start + timedelta(weeks=index)
        values = tuple(item[1] for item in history[-8:])
        recurring = sum(
            (
                abs(item.signed_amount)
                for item in occurrences
                if item.financial_role in expense_roles
                and week_start <= item.occurrence_date <= week_start + timedelta(days=6)
            ),
            start=_ZERO,
        )
        since, until = _payday_distances(week_start, dataset.plan.payday_days)
        row = ForecastInferenceRow(
            week_start=week_start,
            forecast_origin_at=datetime.combine(week_start, time.min, tzinfo=UTC),
            lag_1=values[-1],
            lag_2=values[-2],
            lag_4=values[-4],
            rolling_mean_4=sum(values[-4:], start=_ZERO) / 4,
            rolling_mean_8=sum(values, start=_ZERO) / 8,
            days_since_payday=since,
            days_until_payday=until,
            month=week_start.month,
            week_of_year=week_start.isocalendar().week,
            known_recurring_outflow=recurring,
            recurring_outflow_known_at=plan.knowledge_cutoff_at,
        )
        value = _recursive_prediction(trained, row, tuple(history))
        points.append((week_start, value))
        history.append((week_start, value))
    return tuple(points)


def _residuals(
    trained: TrainedPrimaryForecaster,
    dataset: ForecastDataset,
    plan: ForecastPathPlan,
) -> tuple[tuple[Decimal, ...], bool]:
    validation = trained.comparison.expanding_validation
    values = (
        tuple(
            actual - predicted
            for actual, predicted in zip(
                validation.actuals, validation.predictions, strict=True
            )
        )
        if validation is not None
        else tuple(row.target - row.rolling_mean_4 for row in dataset.feature_rows)
    )
    limited = len(values) < plan.policy.minimum_residual_samples
    minimum = plan.policy.minimum_weekly_uncertainty
    if not values:
        values = (-minimum, minimum)
    elif max(abs(item) for item in values) < minimum:
        values = (*values, -minimum, minimum)
    return values, limited


def _interval_performance(
    trained: TrainedPrimaryForecaster,
    residuals: tuple[Decimal, ...],
    plan: ForecastPathPlan,
    confidence_multiplier: Decimal,
) -> ForecastIntervalPerformance | None:
    final = trained.comparison.final_test
    if final is None:
        return None
    tail = (_ONE - plan.policy.interval_probability) / 2
    lower_error = _quantile(residuals, tail) * confidence_multiplier
    upper_error = _quantile(residuals, _ONE - tail) * confidence_multiplier
    covered = 0
    widths: list[Decimal] = []
    for actual, prediction in zip(final.actuals, final.predictions, strict=True):
        lower = max(_ZERO, prediction + lower_error)
        upper = max(lower, prediction + upper_error)
        covered += int(lower <= actual <= upper)
        widths.append(upper - lower)
    return ForecastIntervalPerformance(
        nominal_coverage=plan.policy.interval_probability,
        empirical_coverage=Decimal(covered) / len(final.actuals),
        mean_interval_width=_money(sum(widths, start=_ZERO) / len(widths)),
        sample_count=len(final.actuals),
    )


def _weekday_weights(dataset: ForecastDataset) -> tuple[Decimal, ...]:
    totals = [Decimal("0") for _ in range(7)]
    for item in dataset.daily_calendar:
        if (
            item.status is ForecastDayStatus.COVERED
            and item.discretionary_spending is not None
        ):
            totals[item.observation_date.weekday()] += item.discretionary_spending
    total = sum(totals, start=_ZERO)
    if total == 0:
        return tuple(Decimal("1") / 7 for _ in range(7))
    return tuple(value / total for value in totals)


def _allocate_week(
    amount: Decimal, weights: tuple[Decimal, ...]
) -> tuple[Decimal, ...]:
    remaining = amount
    allocated: list[Decimal] = []
    for weight in weights[:6]:
        value = min(remaining, max(_ZERO, _money(amount * weight)))
        allocated.append(value)
        remaining -= value
    return (*allocated, _money(remaining))


def _validate_inputs(
    *,
    dataset: ForecastDataset,
    trained: TrainedPrimaryForecaster,
    plan: ForecastPathPlan,
    scenario: ForecastScenario,
) -> None:
    if (
        dataset.plan.user_profile_id != plan.user_profile_id
        or dataset.plan.account_ids != (plan.account_id,)
    ):
        raise ForecastPathError(
            ForecastPathErrorCode.ACCOUNT_SCOPE_MISMATCH,
            "forecast dataset must contain exactly the requested owned account",
        )
    if (
        dataset.plan.knowledge_cutoff_at != plan.knowledge_cutoff_at
        or trained.comparison.knowledge_cutoff_at != plan.knowledge_cutoff_at
        or trained.latest_observed_week is None
        or trained.latest_observed_week + timedelta(weeks=1) != plan.forecast_start
    ):
        raise ForecastPathError(
            ForecastPathErrorCode.FORECAST_EVIDENCE_MISALIGNED,
            "model, dataset, cutoff, and forecast origin must align",
        )
    end_date = plan.forecast_start + timedelta(days=plan.horizon_days - 1)
    if any(
        not plan.forecast_start <= item.adjustment_date <= end_date
        for item in scenario.adjustments
    ):
        raise ForecastPathError(
            ForecastPathErrorCode.SCENARIO_OUTSIDE_HORIZON,
            "scenario adjustments must fall inside the forecast horizon",
        )


def build_balance_forecast_path(
    factory: sessionmaker[Session],
    *,
    dataset: ForecastDataset,
    trained: TrainedPrimaryForecaster,
    plan: ForecastPathPlan,
    scenario: ForecastScenario | None = None,
) -> BalanceForecastPath:
    """Build a deterministic residual-bootstrap daily balance path without writes."""
    selected_scenario = scenario or ForecastScenario()
    _validate_inputs(
        dataset=dataset,
        trained=trained,
        plan=plan,
        scenario=selected_scenario,
    )
    with session_scope(factory) as session:
        account = session.scalar(
            select(AccountRecord).where(
                AccountRecord.id == plan.account_id,
                AccountRecord.user_profile_id == plan.user_profile_id,
            )
        )
        if account is None:
            raise ForecastPathError(
                ForecastPathErrorCode.ACCOUNT_NOT_FOUND,
                "forecast account is unavailable to this profile",
            )
        balance = _latest_balance(
            session,
            account_id=plan.account_id,
            as_of_date=plan.forecast_start - timedelta(days=1),
            cutoff=plan.knowledge_cutoff_at,
        )
        if balance is None:
            raise ForecastPathError(
                ForecastPathErrorCode.BALANCE_NOT_FOUND,
                "a verified balance known by the forecast cutoff is required",
            )
        occurrences = _recurring_occurrences(session, plan=plan)

    freshness = _forecast_freshness(
        account=account,
        balance=balance,
        dataset=dataset,
        plan=plan,
    )
    residuals, limited_residuals = _residuals(trained, dataset, plan)
    warnings: list[ForecastPathWarningCode] = []
    confidence_multiplier = _ONE
    if not trained.comparison.selected:
        warnings.append(ForecastPathWarningCode.LOW_CONFIDENCE_MODEL)
        confidence_multiplier *= plan.policy.low_confidence_multiplier
    if limited_residuals:
        warnings.append(ForecastPathWarningCode.LIMITED_RESIDUAL_HISTORY)
    if freshness.warnings:
        warnings.append(ForecastPathWarningCode.STALE_DATA)
        confidence_multiplier *= plan.policy.stale_data_multiplier

    base_points = _weekly_point_predictions(
        dataset=dataset,
        trained=trained,
        plan=plan,
        occurrences=occurrences,
    )
    multiplier = selected_scenario.discretionary_spending_multiplier
    point_values = tuple(
        (week, _money(value * multiplier)) for week, value in base_points
    )
    weights = _weekday_weights(dataset)
    expected_daily_outflows: dict[date, Decimal] = {}
    for week_start, amount in point_values:
        for offset, daily_amount in enumerate(_allocate_week(amount, weights)):
            expected_daily_outflows[week_start + timedelta(days=offset)] = daily_amount

    rng = random.Random(plan.policy.random_seed)
    simulated_weekly: list[list[Decimal]] = [[] for _ in point_values]
    simulated_daily_balances: list[list[Decimal]] = [
        [] for _ in range(plan.horizon_days)
    ]
    recurring_by_date: defaultdict[date, Decimal] = defaultdict(lambda: _ZERO)
    for occurrence in occurrences:
        recurring_by_date[occurrence.occurrence_date] += occurrence.signed_amount
    adjustments_by_date: defaultdict[date, Decimal] = defaultdict(lambda: _ZERO)
    for adjustment in selected_scenario.adjustments:
        adjustments_by_date[adjustment.adjustment_date] += adjustment.amount

    for _simulation in range(plan.policy.simulation_count):
        simulated_outflows: dict[date, Decimal] = {}
        for index, (week_start, point) in enumerate(point_values):
            sampled = max(
                _ZERO,
                point + rng.choice(residuals) * confidence_multiplier * multiplier,
            )
            sampled = _money(sampled)
            simulated_weekly[index].append(sampled)
            for offset, daily_amount in enumerate(_allocate_week(sampled, weights)):
                simulated_outflows[week_start + timedelta(days=offset)] = daily_amount
        running = balance.balance
        for offset in range(plan.horizon_days):
            forecast_date = plan.forecast_start + timedelta(days=offset)
            running = _money(
                running
                + recurring_by_date[forecast_date]
                + adjustments_by_date[forecast_date]
                - simulated_outflows[forecast_date]
            )
            simulated_daily_balances[offset].append(running)

    tail = (_ONE - plan.policy.interval_probability) / 2
    weekly_results: list[WeeklySpendingPath] = []
    for (week_start, expected), simulations in zip(
        point_values, simulated_weekly, strict=True
    ):
        values = tuple(simulations)
        lower = min(expected, _quantile(values, tail))
        upper = max(expected, _quantile(values, _ONE - tail))
        weekly_results.append(
            WeeklySpendingPath(
                week_start=week_start,
                week_end=week_start + timedelta(days=6),
                expected_discretionary_spending=expected,
                lower_discretionary_spending=_money(lower),
                upper_discretionary_spending=_money(upper),
            )
        )

    expected_running = balance.balance
    daily_results: list[DailyBalancePathPoint] = []
    for offset, simulations in enumerate(simulated_daily_balances):
        forecast_date = plan.forecast_start + timedelta(days=offset)
        discretionary = expected_daily_outflows[forecast_date]
        recurring = recurring_by_date[forecast_date]
        scenario_adjustment_value = adjustments_by_date[forecast_date]
        expected_running = _money(
            expected_running + recurring + scenario_adjustment_value - discretionary
        )
        values = tuple(simulations)
        lower = min(expected_running, _quantile(values, tail))
        upper = max(expected_running, _quantile(values, _ONE - tail))
        daily_results.append(
            DailyBalancePathPoint(
                forecast_date=forecast_date,
                expected_discretionary_outflow=discretionary,
                recurring_net_flow=_money(recurring),
                scenario_adjustment=_money(scenario_adjustment_value),
                expected_balance=expected_running,
                lower_balance=_money(lower),
                upper_balance=_money(upper),
            )
        )

    daily_path = tuple(daily_results)
    return BalanceForecastPath(
        plan=plan,
        scenario=selected_scenario,
        opening_balance=ForecastOpeningBalance(
            balance=balance.balance,
            currency=Currency(balance.currency),
            as_of_date=balance.as_of_date,
            recorded_at=balance.recorded_at,
            source=BalanceSnapshotSource(balance.source),
        ),
        selected_model=trained.comparison.selected_model,
        interval_method=ForecastIntervalMethod.RESIDUAL_BOOTSTRAP,
        widening_multiplier=confidence_multiplier,
        warnings=tuple(warnings),
        freshness_warnings=freshness.warnings,
        recurring_occurrences=occurrences,
        weekly_spending=tuple(weekly_results),
        daily_balances=daily_path,
        interval_performance=_interval_performance(
            trained, residuals, plan, confidence_multiplier
        ),
        expected_final_balance=daily_path[-1].expected_balance,
        lower_final_balance=daily_path[-1].lower_balance,
        upper_final_balance=daily_path[-1].upper_balance,
    )


__all__ = [
    "ForecastPathError",
    "ForecastPathErrorCode",
    "build_balance_forecast_path",
]
