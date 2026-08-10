"""Tests for gap, overlap, and disconnected statement analysis."""

from datetime import date

from cashflow_ai.imports import analyse_statement_coverage
from cashflow_ai.schemas import CoverageStatus, DateRange, StatementCoverage


def coverage(
    start: date,
    end: date,
    *,
    missing: tuple[DateRange, ...] = (),
) -> StatementCoverage:
    return StatementCoverage(
        statement_start_date=start,
        statement_end_date=end,
        status=CoverageStatus.GAPPED if missing else CoverageStatus.COMPLETE,
        missing_periods=missing,
    )


def test_disconnected_statement_reports_old_and_new_missing_intervals() -> None:
    january = coverage(date(2026, 1, 1), date(2026, 1, 31))
    april = coverage(date(2026, 4, 1), date(2026, 4, 30))
    june = coverage(date(2026, 6, 1), date(2026, 6, 30))

    result = analyse_statement_coverage(june, (january, april))

    assert result.previous_statement_count == 2
    assert result.previous_missing_periods == (
        DateRange(start_date=date(2026, 2, 1), end_date=date(2026, 3, 31)),
    )
    assert result.new_missing_periods == (
        DateRange(start_date=date(2026, 5, 1), end_date=date(2026, 5, 31)),
    )
    assert result.overlap_periods == ()
    assert result.disconnected_range is True


def test_overlapping_statement_merges_overlap_and_does_not_invent_a_gap() -> None:
    first = coverage(date(2026, 1, 1), date(2026, 1, 31))
    second = coverage(date(2026, 1, 20), date(2026, 2, 10))
    incoming = coverage(date(2026, 1, 25), date(2026, 2, 20))

    result = analyse_statement_coverage(incoming, (first, second))

    assert result.previous_missing_periods == ()
    assert result.new_missing_periods == ()
    assert result.overlap_periods == (
        DateRange(start_date=date(2026, 1, 25), end_date=date(2026, 2, 10)),
    )
    assert result.disconnected_range is False


def test_adjacent_statement_is_connected_and_shrinks_an_old_gap() -> None:
    january = coverage(date(2026, 1, 1), date(2026, 1, 31))
    april = coverage(date(2026, 4, 1), date(2026, 4, 30))
    march = coverage(date(2026, 3, 1), date(2026, 3, 31))

    result = analyse_statement_coverage(march, (january, april))

    assert result.previous_missing_periods == (
        DateRange(start_date=date(2026, 2, 1), end_date=date(2026, 3, 31)),
    )
    assert result.new_missing_periods == ()
    assert result.disconnected_range is False


def test_explicit_missing_periods_at_statement_edges_are_retained() -> None:
    whole_month = DateRange(
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 31),
    )
    incoming = coverage(
        whole_month.start_date,
        whole_month.end_date,
        missing=(whole_month,),
    )

    result = analyse_statement_coverage(incoming, ())

    assert result.previous_missing_periods == ()
    assert result.new_missing_periods == (whole_month,)
    assert result.disconnected_range is False


def test_edge_gaps_survive_when_known_ranges_exist() -> None:
    incoming = coverage(
        date(2026, 7, 1),
        date(2026, 7, 31),
        missing=(
            DateRange(start_date=date(2026, 7, 1), end_date=date(2026, 7, 2)),
            DateRange(start_date=date(2026, 7, 30), end_date=date(2026, 7, 31)),
        ),
    )

    result = analyse_statement_coverage(incoming, ())

    assert result.new_missing_periods == incoming.missing_periods
