"""Pure, privacy-safe projections for the personal cash dashboard hero."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape
from typing import Literal

from cashflow_ai.schemas.analytics import BalanceHistoryPoint, CashFlowAnalytics
from cashflow_ai.schemas.transactions import Currency

_SVG_WIDTH = Decimal(720)
_SVG_HEIGHT = Decimal(160)
_SVG_PADDING = Decimal(8)
_COORDINATE_QUANTUM = Decimal("0.01")
_CURRENCY_SYMBOLS = {Currency.GBP: "£"}

type ChangeTone = Literal["positive", "negative", "neutral", "unavailable"]


@dataclass(frozen=True, slots=True)
class CashBalanceHero:
    """Cash-balance headline derived only from verified balance observations."""

    cash_balance: Decimal | None
    cash_balance_label: str
    change: Decimal | None
    change_label: str
    change_percent: Decimal | None
    change_percent_label: str | None
    change_tone: ChangeTone
    earliest_latest_observation_date: date | None
    latest_observation_date: date | None
    earliest_comparison_date: date | None
    account_count: int
    comparable_account_count: int
    point_count: int
    has_gaps: bool


@dataclass(frozen=True, slots=True)
class _AccountSeries:
    account_id: str
    segments: tuple[tuple[BalanceHistoryPoint, ...], ...]

    @property
    def points(self) -> tuple[BalanceHistoryPoint, ...]:
        return tuple(point for segment in self.segments for point in segment)


def _balance_series(analytics: CashFlowAnalytics) -> tuple[_AccountSeries, ...]:
    grouped: dict[str, list[tuple[BalanceHistoryPoint, ...]]] = defaultdict(list)
    for history in analytics.balance_history:
        grouped[history.account_id].extend(
            tuple(
                sorted(
                    segment.points,
                    key=lambda point: (point.as_of_date, point.snapshot_id),
                )
            )
            for segment in history.segments
        )
    return tuple(
        _AccountSeries(
            account_id=account_id,
            segments=tuple(
                sorted(
                    segments,
                    key=lambda segment: (
                        segment[0].as_of_date,
                        segment[0].snapshot_id,
                    ),
                )
            ),
        )
        for account_id, segments in sorted(grouped.items())
        if segments
    )


def _money_label(value: Decimal, currency: Currency, *, signed: bool = False) -> str:
    symbol = _CURRENCY_SYMBOLS[currency]
    magnitude = f"{value.copy_abs():,.2f}"
    if value < 0:
        return f"-{symbol}{magnitude}"
    if signed and value > 0:
        return f"+{symbol}{magnitude}"
    return f"{symbol}{magnitude}"


def _percent_label(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.1"))
    prefix = "+" if rounded > 0 else ""
    return f"{prefix}{rounded:.1f}%"


def cash_balance_hero(analytics: CashFlowAnalytics) -> CashBalanceHero:
    """Summarise latest cash and like-for-like per-account historical change.

    Each account contributes its latest verified observation. Change is the sum of
    each comparable account's own first-to-last difference; no value is carried or
    interpolated across a statement gap.
    """
    series = _balance_series(analytics)
    if not series:
        return CashBalanceHero(
            cash_balance=None,
            cash_balance_label="Not available",
            change=None,
            change_label="Not enough balance history",
            change_percent=None,
            change_percent_label=None,
            change_tone="unavailable",
            earliest_latest_observation_date=None,
            latest_observation_date=None,
            earliest_comparison_date=None,
            account_count=0,
            comparable_account_count=0,
            point_count=0,
            has_gaps=False,
        )

    endpoint_pairs = tuple(
        (account.points[0], account.points[-1]) for account in series
    )
    comparable = tuple(
        (first, latest)
        for first, latest in endpoint_pairs
        if first.as_of_date < latest.as_of_date
    )
    cash_balance = sum(
        (latest.balance for _first, latest in endpoint_pairs),
        start=Decimal(0),
    )
    change = (
        sum(
            (latest.balance - first.balance for first, latest in comparable),
            start=Decimal(0),
        )
        if comparable
        else None
    )
    comparison_base = sum(
        (first.balance for first, _latest in comparable),
        start=Decimal(0),
    )
    change_percent = (
        change / comparison_base * Decimal(100)
        if change is not None and comparison_base != 0
        else None
    )
    change_tone: ChangeTone
    if change is None:
        change_tone = "unavailable"
    elif change > 0:
        change_tone = "positive"
    elif change < 0:
        change_tone = "negative"
    else:
        change_tone = "neutral"

    return CashBalanceHero(
        cash_balance=cash_balance,
        cash_balance_label=_money_label(cash_balance, analytics.currency),
        change=change,
        change_label=(
            "Not enough balance history"
            if change is None
            else _money_label(change, analytics.currency, signed=True)
        ),
        change_percent=change_percent,
        change_percent_label=(
            None if change_percent is None else _percent_label(change_percent)
        ),
        change_tone=change_tone,
        earliest_latest_observation_date=min(
            latest.as_of_date for _first, latest in endpoint_pairs
        ),
        latest_observation_date=max(
            latest.as_of_date for _first, latest in endpoint_pairs
        ),
        earliest_comparison_date=(
            min(first.as_of_date for first, _latest in comparable)
            if comparable
            else None
        ),
        account_count=len(series),
        comparable_account_count=len(comparable),
        point_count=sum(len(account.points) for account in series),
        has_gaps=any(len(account.segments) > 1 for account in series),
    )


def _coordinate(value: Decimal) -> str:
    rendered = format(value.quantize(_COORDINATE_QUANTUM), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def pulse_line_html(analytics: CashFlowAnalytics) -> str:
    """Build a deterministic inline SVG while leaving statement gaps disconnected."""
    series = _balance_series(analytics)
    points = tuple(point for account in series for point in account.points)
    if not points:
        return (
            '<div class="cf-pulse cf-pulse--empty" role="img" '
            'aria-label="No verified cash balance history yet.">'
            '<span class="cf-pulse-empty">Balance history will appear here.</span>'
            "</div>"
        )

    first_date = min(point.as_of_date for point in points)
    last_date = max(point.as_of_date for point in points)
    minimum = min(point.balance for point in points)
    maximum = max(point.balance for point in points)
    date_span = Decimal((last_date - first_date).days)
    balance_span = maximum - minimum
    inner_width = _SVG_WIDTH - _SVG_PADDING * 2
    inner_height = _SVG_HEIGHT - _SVG_PADDING * 2

    def project(point: BalanceHistoryPoint) -> tuple[str, str]:
        x = (
            _SVG_WIDTH / 2
            if date_span == 0
            else _SVG_PADDING
            + Decimal((point.as_of_date - first_date).days) / date_span * inner_width
        )
        y = (
            _SVG_HEIGHT / 2
            if balance_span == 0
            else _SVG_PADDING + (maximum - point.balance) / balance_span * inner_height
        )
        return _coordinate(x), _coordinate(y)

    marks: list[str] = []
    for account_index, account in enumerate(series):
        for segment_index, segment in enumerate(account.segments):
            coordinates = tuple(project(point) for point in segment)
            if len(coordinates) == 1:
                x, y = coordinates[0]
                marks.append(
                    f'<circle class="cf-pulse-point" cx="{x}" cy="{y}" r="3" />'
                )
                continue
            path = " ".join(
                f"{'M' if index == 0 else 'L'} {x} {y}"
                for index, (x, y) in enumerate(coordinates)
            )
            marks.append(
                '<path class="cf-pulse-line '
                f"cf-pulse-line--account-{account_index} "
                f'cf-pulse-line--segment-{segment_index}" d="{path}" />'
            )

    has_gaps = any(len(account.segments) > 1 for account in series)
    accessible_label = (
        "Verified cash balance history. Statement gaps are shown as disconnected lines."
        if has_gaps
        else "Verified cash balance history."
    )
    return (
        f'<div class="cf-pulse" role="img" aria-label="{escape(accessible_label)}">'
        '<svg class="cf-pulse-svg" viewBox="0 0 720 160" '
        'preserveAspectRatio="none" aria-hidden="true" focusable="false">'
        + "".join(marks)
        + "</svg></div>"
    )


__all__ = ["CashBalanceHero", "cash_balance_hero", "pulse_line_html"]
