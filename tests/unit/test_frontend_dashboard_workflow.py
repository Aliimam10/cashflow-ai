"""Tests for the pure personal cash-dashboard hero projection."""

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from cashflow_ai.frontend.dashboard_workflow import cash_balance_hero, pulse_line_html
from cashflow_ai.schemas.analytics import (
    AccountBalanceHistory,
    AccountCoverageIndicator,
    AnalyticsCoverageStatus,
    AnalyticsScope,
    AnalyticsView,
    BalanceHistoryPoint,
    BalanceHistorySegment,
    CashFlowAnalytics,
    DataCoverageIndicator,
    SavingsRateResult,
    SavingsRateUnavailableReason,
)
from cashflow_ai.schemas.statements import BalanceSnapshotSource, DateRange
from cashflow_ai.schemas.transactions import Currency

PERIOD = DateRange(start_date=date(2025, 9, 1), end_date=date(2026, 8, 31))


def _point(
    account_id: str,
    snapshot_id: str,
    observed_on: date,
    balance: str,
) -> BalanceHistoryPoint:
    return BalanceHistoryPoint(
        snapshot_id=snapshot_id,
        account_id=account_id,
        as_of_date=observed_on,
        balance=Decimal(balance),
        currency=Currency.GBP,
        source=BalanceSnapshotSource.RUNNING_BALANCE,
    )


def _history(
    account_id: str,
    *segments: tuple[BalanceHistoryPoint, ...],
) -> AccountBalanceHistory:
    return AccountBalanceHistory(
        account_id=account_id,
        segments=tuple(
            BalanceHistorySegment(coverage_period=None, points=points)
            for points in segments
        ),
    )


def _analytics(
    *histories: AccountBalanceHistory,
    account_ids: tuple[str, ...] = ("account-a",),
) -> CashFlowAnalytics:
    coverage = DataCoverageIndicator(
        requested_period=PERIOD,
        status=AnalyticsCoverageStatus.MISSING,
        fully_covered_periods=(),
        partially_covered_periods=(),
        missing_periods=(PERIOD,),
        requested_days=365,
        fully_covered_days=0,
        partially_covered_days=0,
        missing_days=365,
        accounts=tuple(
            AccountCoverageIndicator(
                account_id=account_id,
                status=AnalyticsCoverageStatus.MISSING,
                covered_periods=(),
                missing_periods=(PERIOD,),
                covered_days=0,
                missing_days=365,
            )
            for account_id in account_ids
        ),
    )
    return CashFlowAnalytics(
        scope=AnalyticsScope(
            user_profile_id="synthetic-profile",
            account_ids=account_ids,
            period=PERIOD,
            view=(
                AnalyticsView.ACCOUNT
                if len(account_ids) == 1
                else AnalyticsView.CONSOLIDATED
            ),
        ),
        currency=Currency.GBP,
        coverage=coverage,
        totals=None,
        savings_rate=SavingsRateResult(
            unavailable_reason=SavingsRateUnavailableReason.INCOMPLETE_COVERAGE
        ),
        category_spending=None,
        spending_cadence=None,
        largest_transactions=(),
        balance_history=histories,
        monthly_cash_flow=(),
        monthly_comparisons=(),
        observed_transaction_count=0,
    )


def test_empty_and_one_point_histories_never_invent_cash_change() -> None:
    empty = _analytics(AccountBalanceHistory(account_id="account-a", segments=()))
    empty_hero = cash_balance_hero(empty)

    assert empty_hero.cash_balance is None
    assert empty_hero.cash_balance_label == "Not available"
    assert empty_hero.change_label == "Not enough balance history"
    assert empty_hero.change_tone == "unavailable"
    assert empty_hero.earliest_latest_observation_date is None
    assert empty_hero.latest_observation_date is None
    assert empty_hero.earliest_comparison_date is None
    assert empty_hero.account_count == empty_hero.point_count == 0
    assert empty_hero.has_gaps is False
    assert "cf-pulse--empty" in pulse_line_html(empty)
    assert "<svg" not in pulse_line_html(empty)

    single = _analytics(
        _history(
            "account-a",
            (_point("account-a", "snapshot-a", date(2026, 8, 31), "1250.00"),),
        )
    )
    single_hero = cash_balance_hero(single)
    single_svg = pulse_line_html(single)

    assert single_hero.cash_balance == Decimal("1250.00")
    assert single_hero.cash_balance_label == "£1,250.00"
    assert single_hero.change is None
    assert single_hero.change_percent is None
    assert single_hero.comparable_account_count == 0
    assert single_hero.earliest_latest_observation_date == date(2026, 8, 31)
    assert single_hero.latest_observation_date == date(2026, 8, 31)
    assert '<circle class="cf-pulse-point" cx="360" cy="80" r="3" />' in single_svg
    assert "Statement gaps" not in single_svg


def test_multi_account_balance_and_change_are_like_for_like_across_gaps() -> None:
    account_a = _history(
        "account-a",
        (
            _point("account-a", "a-2", date(2025, 10, 1), "150.00"),
            _point("account-a", "a-1", date(2025, 9, 1), "100.00"),
        ),
        (_point("account-a", "a-3", date(2026, 8, 31), "200.00"),),
    )
    account_b = _history(
        "account-b",
        (
            _point("account-b", "b-1", date(2025, 9, 15), "50.00"),
            _point("account-b", "b-2", date(2026, 8, 15), "80.00"),
        ),
    )
    analytics = _analytics(
        account_b,
        account_a,
        account_ids=("account-a", "account-b"),
    )

    hero = cash_balance_hero(analytics)
    svg = pulse_line_html(analytics)

    assert hero.cash_balance == Decimal("280.00")
    assert hero.change == Decimal("130.00")
    assert hero.change_percent_label == "+86.7%"
    assert hero.change_tone == "positive"
    assert hero.earliest_latest_observation_date == date(2026, 8, 15)
    assert hero.latest_observation_date == date(2026, 8, 31)
    assert hero.earliest_comparison_date == date(2025, 9, 1)
    assert hero.account_count == hero.comparable_account_count == 2
    assert hero.point_count == 5
    assert hero.has_gaps is True
    assert svg.count("<path") == 2
    assert svg.count("<circle") == 1
    assert "Statement gaps are shown as disconnected lines" in svg
    assert "account-a" not in svg
    assert "javascript" not in svg.casefold()
    assert "http" not in svg.casefold()


def test_negative_flat_zero_base_and_same_day_paths_are_honest() -> None:
    negative = _analytics(
        _history(
            "account-a",
            (
                _point("account-a", "a-1", date(2026, 1, 1), "100.00"),
                _point("account-a", "a-2", date(2026, 2, 1), "90.00"),
            ),
        )
    )
    negative_hero = cash_balance_hero(negative)
    assert negative_hero.change_label == "-£10.00"
    assert negative_hero.change_percent_label == "-10.0%"
    assert negative_hero.change_tone == "negative"

    flat = _analytics(
        _history(
            "account-a",
            (
                _point("account-a", "a-1", date(2026, 1, 1), "100.00"),
                _point("account-a", "a-2", date(2026, 2, 1), "100.00"),
            ),
        )
    )
    flat_hero = cash_balance_hero(flat)
    assert flat_hero.change_label == "£0.00"
    assert flat_hero.change_percent_label == "0.0%"
    assert flat_hero.change_tone == "neutral"
    assert 'd="M 8 80 L 712 80"' in pulse_line_html(flat)

    zero_base = _analytics(
        _history(
            "account-a",
            (
                _point("account-a", "a-1", date(2026, 1, 1), "0.00"),
                _point("account-a", "a-2", date(2026, 2, 1), "25.00"),
            ),
        )
    )
    zero_hero = cash_balance_hero(zero_base)
    assert zero_hero.change_percent is None
    assert zero_hero.change_percent_label is None

    same_day = _analytics(
        _history(
            "account-a",
            (
                _point("account-a", "a-1", date(2026, 8, 1), "10.00"),
                _point("account-a", "a-2", date(2026, 8, 1), "20.00"),
            ),
        )
    )
    assert 'd="M 360 152 L 360 8"' in pulse_line_html(same_day)
    assert cash_balance_hero(same_day).change is None

    negative_cash = _analytics(
        _history(
            "account-a",
            (_point("account-a", "a-1", date(2026, 1, 1), "-25.00"),),
        )
    )
    assert cash_balance_hero(negative_cash).cash_balance_label == "-£25.00"


def test_hero_projection_is_frozen() -> None:
    hero = cash_balance_hero(
        _analytics(
            _history(
                "account-a",
                (_point("account-a", "a-1", date(2026, 1, 1), "20.00"),),
            )
        )
    )

    with pytest.raises(FrozenInstanceError):
        hero.cash_balance = Decimal("999.00")  # type: ignore[misc]
