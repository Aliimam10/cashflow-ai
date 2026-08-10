"""Pure statement-coverage analysis for confirmed imports."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from cashflow_ai.schemas.csv_imports import CsvCoverageAnalysis
from cashflow_ai.schemas.statements import DateRange, StatementCoverage

_ONE_DAY = timedelta(days=1)


def _known_ranges(coverage: StatementCoverage) -> tuple[DateRange, ...]:
    """Subtract explicitly missing periods from one statement extent."""
    ranges: list[DateRange] = []
    cursor = coverage.statement_start_date
    for missing in coverage.missing_periods:
        if cursor < missing.start_date:
            ranges.append(
                DateRange(start_date=cursor, end_date=missing.start_date - _ONE_DAY)
            )
        cursor = missing.end_date + _ONE_DAY
    if cursor <= coverage.statement_end_date:
        ranges.append(
            DateRange(start_date=cursor, end_date=coverage.statement_end_date)
        )
    return tuple(ranges)


def _merge_ranges(ranges: Iterable[DateRange]) -> tuple[DateRange, ...]:
    ordered = sorted(ranges, key=lambda item: (item.start_date, item.end_date))
    merged: list[DateRange] = []
    for current in ordered:
        if not merged or current.start_date > merged[-1].end_date + _ONE_DAY:
            merged.append(current)
            continue
        previous = merged[-1]
        merged[-1] = DateRange(
            start_date=previous.start_date,
            end_date=max(previous.end_date, current.end_date),
        )
    return tuple(merged)


def _missing_within_statements(
    statements: Iterable[StatementCoverage],
) -> tuple[DateRange, ...]:
    items = tuple(statements)
    if not items:
        return ()
    known = _merge_ranges(
        known_range for statement in items for known_range in _known_ranges(statement)
    )
    outer_start = min(item.statement_start_date for item in items)
    outer_end = max(item.statement_end_date for item in items)
    if not known:
        return (DateRange(start_date=outer_start, end_date=outer_end),)

    gaps: list[DateRange] = []
    cursor = outer_start
    for current in known:
        if cursor < current.start_date:
            gaps.append(
                DateRange(start_date=cursor, end_date=current.start_date - _ONE_DAY)
            )
        cursor = max(cursor, current.end_date + _ONE_DAY)
    if cursor <= outer_end:
        gaps.append(DateRange(start_date=cursor, end_date=outer_end))
    return tuple(gaps)


def _overlap_ranges(
    incoming: StatementCoverage,
    existing: Iterable[StatementCoverage],
) -> tuple[DateRange, ...]:
    overlaps: list[DateRange] = []
    for statement in existing:
        start = max(incoming.statement_start_date, statement.statement_start_date)
        end = min(incoming.statement_end_date, statement.statement_end_date)
        if start <= end:
            overlaps.append(DateRange(start_date=start, end_date=end))
    return _merge_ranges(overlaps)


def analyse_statement_coverage(
    incoming: StatementCoverage,
    existing: Iterable[StatementCoverage],
) -> CsvCoverageAnalysis:
    """Report historical gaps, newly exposed gaps, overlap, and disconnection."""
    prior = tuple(existing)
    previous_missing = _missing_within_statements(prior)
    combined_missing = _missing_within_statements((*prior, incoming))
    new_missing = tuple(
        gap
        for gap in combined_missing
        if not any(
            old.start_date <= gap.start_date and old.end_date >= gap.end_date
            for old in previous_missing
        )
    )
    overlaps = _overlap_ranges(incoming, prior)
    disconnected = bool(prior) and not any(
        incoming.statement_start_date <= statement.statement_end_date + _ONE_DAY
        and statement.statement_start_date <= incoming.statement_end_date + _ONE_DAY
        for statement in prior
    )
    return CsvCoverageAnalysis(
        previous_statement_count=len(prior),
        previous_missing_periods=previous_missing,
        new_missing_periods=new_missing,
        overlap_periods=overlaps,
        disconnected_range=disconnected,
    )
