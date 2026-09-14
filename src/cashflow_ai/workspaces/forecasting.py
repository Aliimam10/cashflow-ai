"""Conservative balance forecasts over one finalized statement workspace."""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import cast
from zoneinfo import ZoneInfo

from cashflow_ai.schemas.money import MONEY_QUANTUM
from cashflow_ai.schemas.statements import CoverageStatus, DateRange
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspace_forecasts import (
    WorkspaceForecastAvailable,
    WorkspaceForecastModelMetadata,
    WorkspaceForecastPoint,
    WorkspaceForecastReasonCode,
    WorkspaceForecastRequest,
    WorkspaceForecastResult,
    WorkspaceForecastWarningCode,
    WorkspaceForecastWithheld,
)
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceBalanceConfirmation,
    WorkspaceCoverageConfirmation,
    WorkspaceRowReviewState,
    WorkspaceStatus,
    WorkspaceTransactionRow,
)

_LONDON = ZoneInfo("Europe/London")
_ZERO = Decimal("0.00")
_ONE = Decimal("1")
_HISTORY_DAYS = 60
_MAX_EVIDENCE_AGE_DAYS = 45
_INTERVAL_PROBABILITY = Decimal("0.80")
_SIMULATION_COUNT = 500
_RANDOM_SEED = 42
_MINIMUM_DAILY_UNCERTAINTY = Decimal("1.00")
_CASH_AFFECTING_ROLES = frozenset(
    role
    for role in FinancialRole
    if role not in {FinancialRole.EXCLUDED, FinancialRole.UNKNOWN}
)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _calendar_today() -> date:
    """Return the local product date used for freshness decisions."""
    return datetime.now(tz=_LONDON).date()


def _intersects(left: DateRange, start: date, end: date) -> bool:
    return left.start_date <= end and left.end_date >= start


def _row_is_usable(row: WorkspaceTransactionRow) -> bool:
    return (
        row.review_state is WorkspaceRowReviewState.CONFIRMED
        and row.source_id is None
        and row.source_type is None
        and row.transaction_date is not None
        and row.description is not None
        and bool(row.description.strip())
        and row.amount is not None
        and row.amount != _ZERO
    )


def _future_dated(workspace: StatementWorkspace, today: date) -> bool:
    finalized_at = workspace.finalized_at
    coverage = workspace.coverage
    balance = workspace.balance
    if finalized_at is not None and finalized_at.astimezone(_LONDON).date() > today:
        return True
    if coverage is not None and (
        coverage.start_date > today or coverage.end_date > today
    ):
        return True
    if balance is not None and balance.as_of_date > today:
        return True
    return any(
        (row.transaction_date is not None and row.transaction_date > today)
        or (row.posting_date is not None and row.posting_date > today)
        for row in workspace.rows
    )


def _readiness_reasons(
    workspace: StatementWorkspace,
    request: WorkspaceForecastRequest,
    today: date,
) -> tuple[WorkspaceForecastReasonCode, ...]:
    reasons: set[WorkspaceForecastReasonCode] = set()
    if workspace.revision != request.expected_workspace_revision:
        reasons.add(WorkspaceForecastReasonCode.REVISION_MISMATCH)
    if (
        workspace.status is not WorkspaceStatus.FINALIZED
        or workspace.finalized_at is None
        or workspace.coverage is None
    ):
        reasons.add(WorkspaceForecastReasonCode.WORKSPACE_NOT_FINALIZED)
    if any(not _row_is_usable(row) for row in workspace.rows):
        reasons.add(WorkspaceForecastReasonCode.UNRESOLVED_ROWS)
    if any(row.financial_role is FinancialRole.UNKNOWN for row in workspace.rows):
        reasons.add(WorkspaceForecastReasonCode.UNKNOWN_FINANCIAL_ROLES)
    if workspace.balance is None:
        reasons.add(WorkspaceForecastReasonCode.BALANCE_REQUIRED)
    if _future_dated(workspace, today):
        reasons.add(WorkspaceForecastReasonCode.FUTURE_DATED_EVIDENCE)

    coverage = workspace.coverage
    if coverage is not None:
        if coverage.status is CoverageStatus.PARTIAL:
            reasons.add(WorkspaceForecastReasonCode.PARTIAL_COVERAGE)
        elif coverage.status is CoverageStatus.UNKNOWN:
            reasons.add(WorkspaceForecastReasonCode.UNKNOWN_COVERAGE)
        if (
            today >= coverage.end_date
            and (today - coverage.end_date).days > _MAX_EVIDENCE_AGE_DAYS
        ):
            reasons.add(WorkspaceForecastReasonCode.COVERAGE_STALE)
        training_start = coverage.end_date - timedelta(days=_HISTORY_DAYS - 1)
        if coverage.start_date > training_start:
            reasons.add(WorkspaceForecastReasonCode.INSUFFICIENT_HISTORY)
        if coverage.status is CoverageStatus.GAPPED and any(
            _intersects(gap, training_start, coverage.end_date)
            for gap in coverage.missing_periods
        ):
            reasons.add(WorkspaceForecastReasonCode.RECENT_COVERAGE_GAP)

    balance = workspace.balance
    if balance is not None:
        if (
            today >= balance.as_of_date
            and (today - balance.as_of_date).days > _MAX_EVIDENCE_AGE_DAYS
        ):
            reasons.add(WorkspaceForecastReasonCode.BALANCE_STALE)
        dated_rows = tuple(
            row.transaction_date
            for row in workspace.rows
            if row.transaction_date is not None
        )
        if dated_rows and balance.as_of_date < max(dated_rows):
            reasons.add(WorkspaceForecastReasonCode.BALANCE_NOT_LATEST)

    return tuple(reason for reason in WorkspaceForecastReasonCode if reason in reasons)


def _daily_flows(
    rows: tuple[WorkspaceTransactionRow, ...],
    start: date,
    end: date,
) -> dict[date, Decimal]:
    flows = {
        start + timedelta(days=offset): _ZERO
        for offset in range((end - start).days + 1)
    }
    for row in rows:
        transaction_date = cast(date, row.transaction_date)
        amount = cast(Decimal, row.amount)
        if (
            start <= transaction_date <= end
            and row.financial_role in _CASH_AFFECTING_ROLES
        ):
            flows[transaction_date] = _money(flows[transaction_date] + amount)
    return flows


def _weekday_means(flows: dict[date, Decimal]) -> dict[int, Decimal]:
    values: defaultdict[int, list[Decimal]] = defaultdict(list)
    for observation_date, amount in flows.items():
        values[observation_date.weekday()].append(amount)
    return {
        weekday: _money(sum(amounts, start=_ZERO) / len(amounts))
        for weekday, amounts in values.items()
    }


def _residuals(
    flows: dict[date, Decimal], weekday_means: dict[int, Decimal]
) -> tuple[Decimal, ...]:
    return tuple(
        _money(amount - weekday_means[observation_date.weekday()])
        for observation_date, amount in flows.items()
    )


def _quantile(values: tuple[Decimal, ...], probability: Decimal) -> Decimal:
    ordered = tuple(sorted(values))
    if len(ordered) == 1:
        return ordered[0]
    position = probability * Decimal(len(ordered) - 1)
    lower_index = int(position.to_integral_value(rounding=ROUND_FLOOR))
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - Decimal(lower_index)
    return (
        ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction
    )


def _in_gap(
    observation_date: date,
    coverage: WorkspaceCoverageConfirmation,
) -> bool:
    return any(
        gap.start_date <= observation_date <= gap.end_date
        for gap in coverage.missing_periods
    )


def _date_is_known(
    observation_date: date,
    coverage: WorkspaceCoverageConfirmation,
) -> bool:
    return coverage.start_date <= observation_date <= coverage.end_date and not _in_gap(
        observation_date, coverage
    )


def _advance_balances(
    *,
    observation_date: date,
    expected: Decimal,
    simulated: list[Decimal],
    weekday_means: dict[int, Decimal],
    residuals: tuple[Decimal, ...],
    rng: random.Random,
    known_zero: bool,
) -> tuple[Decimal, list[Decimal]]:
    if known_zero:
        return expected, simulated
    mean = weekday_means[observation_date.weekday()]
    expected = _money(expected + mean)
    return expected, [
        _money(value + mean + residuals[rng.randrange(len(residuals))])
        for value in simulated
    ]


def _interval(
    simulated: list[Decimal],
    expected: Decimal,
    uncertain_days: int,
) -> tuple[Decimal, Decimal]:
    tail = (_ONE - _INTERVAL_PROBABILITY) / Decimal(2)
    values = tuple(simulated)
    floor = _money(_MINIMUM_DAILY_UNCERTAINTY * Decimal(max(uncertain_days, 1)).sqrt())
    lower = min(expected, _quantile(values, tail), _money(expected - floor))
    upper = max(
        expected,
        _quantile(values, _ONE - tail),
        _money(expected + floor),
    )
    return _money(lower), _money(upper)


def _available_forecast(
    workspace: StatementWorkspace,
    request: WorkspaceForecastRequest,
    today: date,
) -> WorkspaceForecastAvailable:
    coverage = cast(WorkspaceCoverageConfirmation, workspace.coverage)
    balance = cast(WorkspaceBalanceConfirmation, workspace.balance)
    finalized_at = cast(datetime, workspace.finalized_at)
    training_end = coverage.end_date
    training_start = training_end - timedelta(days=_HISTORY_DAYS - 1)
    flows = _daily_flows(workspace.rows, training_start, training_end)
    weekday_means = _weekday_means(flows)
    residuals = _residuals(flows, weekday_means)

    expected = _money(balance.balance)
    simulated = [expected] * _SIMULATION_COUNT
    rng = random.Random(_RANDOM_SEED)
    uncertain_days = 0
    bridge_date = balance.as_of_date + timedelta(days=1)
    while bridge_date <= today:
        known_zero = _date_is_known(bridge_date, coverage)
        expected, simulated = _advance_balances(
            observation_date=bridge_date,
            expected=expected,
            simulated=simulated,
            weekday_means=weekday_means,
            residuals=residuals,
            rng=rng,
            known_zero=known_zero,
        )
        uncertain_days += int(not known_zero)
        bridge_date += timedelta(days=1)
    expected_today = expected

    points: list[WorkspaceForecastPoint] = []
    for offset in range(1, request.horizon_days + 1):
        forecast_date = today + timedelta(days=offset)
        expected, simulated = _advance_balances(
            observation_date=forecast_date,
            expected=expected,
            simulated=simulated,
            weekday_means=weekday_means,
            residuals=residuals,
            rng=rng,
            known_zero=False,
        )
        uncertain_days += 1
        lower, upper = _interval(simulated, expected, uncertain_days)
        points.append(
            WorkspaceForecastPoint(
                forecast_date=forecast_date,
                expected_balance=expected,
                lower_balance=lower,
                upper_balance=upper,
            )
        )

    warnings = [
        WorkspaceForecastWarningCode.ONE_SHOT_WORKSPACE_BASELINE,
        WorkspaceForecastWarningCode.EMPIRICAL_INTERVAL_ESTIMATE,
        WorkspaceForecastWarningCode.UNCLASSIFIED_TOTAL_CASH_FLOW,
    ]
    if balance.as_of_date < today:
        warnings.append(WorkspaceForecastWarningCode.BALANCE_BRIDGED_TO_TODAY)
    return WorkspaceForecastAvailable(
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        currency=workspace.currency,
        as_of_date=today,
        horizon_days=request.horizon_days,
        confirmed_balance=balance.balance,
        confirmed_balance_as_of=balance.as_of_date,
        expected_balance_as_of_today=expected_today,
        model=WorkspaceForecastModelMetadata(
            selection_reason=(
                "All workspace history became known at finalization, so this run "
                "uses a transparent recent-history baseline without claiming an "
                "advanced-model comparison or historical backtest."
            ),
            training_window_start=training_start,
            training_window_end=training_end,
            knowledge_cutoff_at=finalized_at,
            interval_probability=_INTERVAL_PROBABILITY,
            simulation_count=_SIMULATION_COUNT,
            random_seed=_RANDOM_SEED,
            minimum_daily_uncertainty=_MINIMUM_DAILY_UNCERTAINTY,
        ),
        warnings=tuple(warnings),
        daily_balances=tuple(points),
    )


def forecast_statement_workspace(
    workspace: StatementWorkspace,
    request: WorkspaceForecastRequest,
    *,
    today: date | None = None,
) -> WorkspaceForecastResult:
    """Return a deterministic path, or stable reasons why it is unsafe."""
    effective_today = today if today is not None else _calendar_today()
    reasons = _readiness_reasons(workspace, request, effective_today)
    if reasons:
        return WorkspaceForecastWithheld(
            workspace_id=workspace.workspace_id,
            workspace_revision=workspace.revision,
            currency=workspace.currency,
            as_of_date=effective_today,
            horizon_days=request.horizon_days,
            reasons=reasons,
        )
    return _available_forecast(workspace, request, effective_today)


__all__ = ["forecast_statement_workspace"]
