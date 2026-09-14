"""Pure display projections for finalized statement-workspace results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from typing import Any, Literal

from cashflow_ai.schemas.analytics import (
    AnalyticsCoverageStatus,
    BalanceHistoryPoint,
    DataCoverageIndicator,
)
from cashflow_ai.schemas.transactions import Currency
from cashflow_ai.schemas.workspace_analytics import (
    WorkspaceAnalytics,
    WorkspaceTransactionSearchResult,
)
from cashflow_ai.schemas.workspace_forecasts import (
    WorkspaceForecastAvailable,
    WorkspaceForecastReasonCode,
    WorkspaceForecastWarningCode,
)

_SVG_WIDTH = Decimal(720)
_SVG_HEIGHT = Decimal(160)
_SVG_PADDING = Decimal(8)
_COORDINATE_QUANTUM = Decimal("0.01")
_CURRENCY_SYMBOLS = {Currency.GBP: "£"}

type BalanceTone = Literal["positive", "negative", "neutral", "unavailable"]


@dataclass(frozen=True, slots=True)
class WorkspaceBalanceSummary:
    """Safe headline values calculated only from returned balance evidence."""

    balance: Decimal | None
    balance_label: str
    as_of_date: date | None
    change: Decimal | None
    change_label: str
    tone: BalanceTone
    has_gaps: bool


def money_text(
    value: Decimal,
    currency: Currency,
    *,
    signed: bool = False,
) -> str:
    """Format fixed-precision API money without converting it to float."""
    symbol = _CURRENCY_SYMBOLS.get(currency, f"{currency.value} ")
    magnitude = f"{value.copy_abs():,.2f}"
    if value < 0:
        return f"-{symbol}{magnitude}"
    if signed and value > 0:
        return f"+{symbol}{magnitude}"
    return f"{symbol}{magnitude}"


def _ordered_segments(
    analytics: WorkspaceAnalytics,
) -> tuple[tuple[BalanceHistoryPoint, ...], ...]:
    """Return chronological non-empty balance segments without joining gaps."""
    segments = [
        segment.points
        for history in analytics.balance_history
        for segment in history.segments
        if segment.points
    ]
    return tuple(
        sorted(
            segments,
            key=lambda segment: (segment[0].as_of_date, segment[0].snapshot_id),
        )
    )


def workspace_balance_summary(analytics: WorkspaceAnalytics) -> WorkspaceBalanceSummary:
    """Summarize the latest verified balance without bridging statement gaps."""
    segments = _ordered_segments(analytics)
    if not segments:
        return WorkspaceBalanceSummary(
            balance=None,
            balance_label="Not available",
            as_of_date=None,
            change=None,
            change_label="Add or confirm running-balance evidence",
            tone="unavailable",
            has_gaps=False,
        )

    latest_segment = max(
        segments,
        key=lambda segment: (segment[-1].as_of_date, segment[-1].snapshot_id),
    )
    first = latest_segment[0]
    latest = latest_segment[-1]
    change = (
        latest.balance - first.balance if first.as_of_date < latest.as_of_date else None
    )
    tone: BalanceTone
    if change is None:
        tone = "unavailable"
    elif change > 0:
        tone = "positive"
    elif change < 0:
        tone = "negative"
    else:
        tone = "neutral"
    return WorkspaceBalanceSummary(
        balance=latest.balance,
        balance_label=money_text(latest.balance, analytics.currency),
        as_of_date=latest.as_of_date,
        change=change,
        change_label=(
            "Not enough continuous balance history"
            if change is None
            else (
                f"{money_text(change, analytics.currency, signed=True)} in this segment"
            )
        ),
        tone=tone,
        has_gaps=len(segments) > 1,
    )


def _coordinate(value: Decimal) -> str:
    rendered = format(value.quantize(_COORDINATE_QUANTUM), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def workspace_pulse_html(analytics: WorkspaceAnalytics) -> str:
    """Render a deterministic balance pulse whose paths stop at coverage gaps."""
    segments = _ordered_segments(analytics)
    points = tuple(point for segment in segments for point in segment)
    if not points:
        return (
            '<div class="cf-pulse cf-pulse--empty" role="img" '
            'aria-label="No verified balance history is available.">'
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
    for segment_index, segment in enumerate(segments):
        coordinates = tuple(project(point) for point in segment)
        if len(coordinates) == 1:
            x, y = coordinates[0]
            marks.append(f'<circle class="cf-pulse-point" cx="{x}" cy="{y}" r="3" />')
            continue
        path = " ".join(
            f"{'M' if index == 0 else 'L'} {x} {y}"
            for index, (x, y) in enumerate(coordinates)
        )
        marks.append(
            '<path class="cf-pulse-line '
            f'cf-pulse-line--segment-{segment_index}" d="{path}" />'
        )

    gap_guidance = (
        " Statement gaps are shown as disconnected lines." if len(segments) > 1 else ""
    )
    return (
        '<div class="cf-pulse" role="img" '
        f'aria-label="Verified balance history.{gap_guidance}">'
        '<svg class="cf-pulse-svg" viewBox="0 0 720 160" '
        'preserveAspectRatio="none" aria-hidden="true" focusable="false">'
        + "".join(marks)
        + "</svg></div>"
    )


def workspace_balance_hero_html(analytics: WorkspaceAnalytics) -> str:
    """Build the themed balance hero while escaping the user account label."""
    summary = workspace_balance_summary(analytics)
    date_label = (
        "No verified balance date"
        if summary.as_of_date is None
        else f"As of {summary.as_of_date.strftime('%d %b %Y')}"
    )
    gap_label = " · gaps are not joined" if summary.has_gaps else ""
    return (
        '<section class="cf-balance-hero">'
        '<div class="cf-balance-copy">'
        '<span class="cf-balance-kicker">'
        f"{escape(analytics.account_name)} BALANCE</span>"
        f'<div class="cf-balance-value">{escape(summary.balance_label)}</div>'
        f'<div class="cf-balance-meta">{escape(date_label + gap_label)}</div>'
        f'<div class="cf-balance-change is-{summary.tone}">'
        f"{escape(summary.change_label)}</div></div>"
        f'<div class="cf-balance-pulse">{workspace_pulse_html(analytics)}</div>'
        "</section>"
    )


def coverage_chart(coverage: DataCoverageIndicator) -> dict[str, Any]:
    """Create an explicitly labelled coverage timeline."""
    values = [
        {
            "status": status,
            "start": period.start_date.isoformat(),
            # Vega temporal ranges are end-exclusive.  Workspace coverage is
            # inclusive, so advance x2 by one day while keeping the displayed
            # through-date faithful to the user's confirmation.
            "end_exclusive": (period.end_date + timedelta(days=1)).isoformat(),
            "through": period.end_date.isoformat(),
        }
        for status, periods in (
            ("Covered", coverage.fully_covered_periods),
            ("Partially covered", coverage.partially_covered_periods),
            ("Missing", coverage.missing_periods),
        )
        for period in periods
    ]
    return {
        "description": "Confirmed statement coverage; missing dates are not zeroes.",
        "data": {"values": values},
        "mark": {"type": "bar", "height": 24, "cornerRadius": 3},
        "encoding": {
            "x": {"field": "start", "type": "temporal", "title": "Statement date"},
            "x2": {"field": "end_exclusive"},
            "color": {
                "field": "status",
                "type": "nominal",
                "scale": {
                    "domain": ["Covered", "Partially covered", "Missing"],
                    "range": ["#52D98F", "#F5A623", "#FF6B78"],
                },
                "legend": {"title": None, "orient": "bottom"},
            },
            "tooltip": ["status", "start", "through"],
        },
    }


def category_donut_chart(analytics: WorkspaceAnalytics) -> dict[str, Any]:
    """Create an expense-role-only spending donut with visible category labels."""
    values = [
        {
            "category": item.category_name or "Other",
            "amount": float(item.amount),
            "transactions": item.transaction_count,
        }
        for item in analytics.category_spending or ()
    ]
    return {
        "description": "Confirmed expense spending grouped by category.",
        "data": {"values": values},
        "mark": {
            "type": "arc",
            "innerRadius": 68,
            "outerRadius": 116,
            "cornerRadius": 5,
            "padAngle": 0.025,
        },
        "encoding": {
            "theta": {"field": "amount", "type": "quantitative"},
            "color": {
                "field": "category",
                "type": "nominal",
                "sort": "-theta",
                "scale": {
                    "range": [
                        "#5B8DEF",
                        "#FF7648",
                        "#52D98F",
                        "#8B5CF6",
                        "#F5A623",
                        "#38BDF8",
                        "#94A0B2",
                    ]
                },
                "legend": {"title": None, "orient": "bottom"},
            },
            "tooltip": [
                "category",
                {
                    "field": "amount",
                    "type": "quantitative",
                    "title": f"Spending ({analytics.currency.value})",
                    "format": ",.2f",
                },
                "transactions",
            ],
        },
        "view": {"stroke": None},
    }


def monthly_cash_flow_chart(analytics: WorkspaceAnalytics) -> dict[str, Any]:
    """Create monthly income/expense bars plus a net-cash-flow line."""
    values = [
        {
            "month": item.month.isoformat(),
            "flow": flow,
            "amount": float(amount),
            "coverage": item.coverage.status.value,
        }
        for item in analytics.monthly_cash_flow
        if item.totals is not None
        for flow, amount in (
            ("Income", item.totals.total_income),
            ("Expenses", item.totals.total_expenses),
            ("Net cash flow", item.totals.net_cash_flow),
        )
    ]
    month = {"field": "month", "type": "temporal", "timeUnit": "yearmonth"}
    return {
        "description": "Observed monthly income, expenses, and net cash flow.",
        "data": {"values": values},
        "layer": [
            {
                "transform": [{"filter": "datum.flow !== 'Net cash flow'"}],
                "mark": {
                    "type": "bar",
                    "cornerRadiusTopLeft": 3,
                    "cornerRadiusTopRight": 3,
                },
                "encoding": {
                    "x": {**month, "title": "Month"},
                    "xOffset": {"field": "flow"},
                    "y": {
                        "field": "amount",
                        "type": "quantitative",
                        "title": f"Amount ({analytics.currency.value})",
                    },
                    "color": {
                        "field": "flow",
                        "type": "nominal",
                        "scale": {
                            "domain": ["Income", "Expenses"],
                            "range": ["#52D98F", "#F5A623"],
                        },
                        "legend": {"title": None, "orient": "bottom"},
                    },
                    "tooltip": ["month", "flow", "amount", "coverage"],
                },
            },
            {
                "transform": [{"filter": "datum.flow === 'Net cash flow'"}],
                "mark": {
                    "type": "line",
                    "point": {"filled": True, "size": 48},
                    "strokeWidth": 2.5,
                    "color": "#5B8DEF",
                },
                "encoding": {
                    "x": month,
                    "y": {"field": "amount", "type": "quantitative"},
                    "tooltip": ["month", "flow", "amount", "coverage"],
                },
            },
        ],
        "resolve": {"scale": {"y": "shared"}},
    }


def forecast_chart(forecast: WorkspaceForecastAvailable) -> dict[str, Any]:
    """Create the expected workspace balance path and empirical interval band."""
    values = [
        {
            "date": point.forecast_date.isoformat(),
            "expected": float(point.expected_balance),
            "lower": float(point.lower_balance),
            "upper": float(point.upper_balance),
        }
        for point in forecast.daily_balances
    ]
    return {
        "description": "Estimated daily balance with an empirical uncertainty band.",
        "data": {"values": values},
        "layer": [
            {
                "mark": {"type": "area", "opacity": 0.18, "color": "#5B8DEF"},
                "encoding": {
                    "x": {"field": "date", "type": "temporal", "title": "Date"},
                    "y": {
                        "field": "lower",
                        "type": "quantitative",
                        "title": f"Balance ({forecast.currency.value})",
                        "scale": {"zero": False},
                    },
                    "y2": {"field": "upper"},
                    "tooltip": ["date", "lower", "upper"],
                },
            },
            {
                "mark": {
                    "type": "line",
                    "point": False,
                    "strokeWidth": 3,
                    "color": "#5B8DEF",
                },
                "encoding": {
                    "x": {"field": "date", "type": "temporal"},
                    "y": {
                        "field": "expected",
                        "type": "quantitative",
                        "scale": {"zero": False},
                    },
                    "tooltip": ["date", "expected", "lower", "upper"],
                },
            },
        ],
    }


def transaction_rows(
    result: WorkspaceTransactionSearchResult,
) -> list[dict[str, object]]:
    """Project approved transactions into a read-only presentation table."""
    return [
        {
            "Date": item.transaction_date.isoformat(),
            "Description": item.description,
            "Amount": money_text(item.amount, item.currency),
            "Category": item.category_name,
            "Financial role": item.financial_role.value.replace("_", " ").title(),
            "Balance": (
                ""
                if item.balance_after is None
                else money_text(item.balance_after, item.currency)
            ),
        }
        for item in result.items
    ]


def analytics_messages(analytics: WorkspaceAnalytics) -> tuple[str, ...]:
    """Explain partial, missing, and unresolved analytics without data invention."""
    messages: list[str] = []
    if analytics.coverage.status is AnalyticsCoverageStatus.PARTIAL:
        messages.append(
            "Statement coverage is partial. Totals include observed covered dates "
            "only; "
            "missing dates are not counted as zero spending."
        )
    elif analytics.coverage.status is AnalyticsCoverageStatus.MISSING:
        messages.append(
            "The selected period has no confirmed coverage, so financial totals are "
            "withheld."
        )
    if analytics.unresolved_financial_role_count:
        messages.append(
            f"{analytics.unresolved_financial_role_count} transaction(s) have an "
            "unknown "
            "financial role and are excluded from income and expense totals."
        )
    return tuple(messages)


_FORECAST_REASON_MESSAGES = {
    WorkspaceForecastReasonCode.REVISION_MISMATCH: (
        "The workspace changed. Refresh this page before forecasting."
    ),
    WorkspaceForecastReasonCode.WORKSPACE_NOT_FINALIZED: (
        "Finalize the statement table before requesting a forecast."
    ),
    WorkspaceForecastReasonCode.UNRESOLVED_ROWS: (
        "Resolve every extraction or duplicate decision before forecasting."
    ),
    WorkspaceForecastReasonCode.UNKNOWN_FINANCIAL_ROLES: (
        "Assign every transaction a confirmed financial role before forecasting."
    ),
    WorkspaceForecastReasonCode.BALANCE_REQUIRED: (
        "Confirm a latest account balance to anchor the future path."
    ),
    WorkspaceForecastReasonCode.BALANCE_NOT_LATEST: (
        "The confirmed balance is older than later transactions in this workspace."
    ),
    WorkspaceForecastReasonCode.FUTURE_DATED_EVIDENCE: (
        "The workspace contains evidence dated after today. Correct those dates first."
    ),
    WorkspaceForecastReasonCode.BALANCE_STALE: (
        "The confirmed balance is too old for a responsible projection."
    ),
    WorkspaceForecastReasonCode.COVERAGE_STALE: (
        "The latest confirmed statement coverage is too old for a projection."
    ),
    WorkspaceForecastReasonCode.PARTIAL_COVERAGE: (
        "Partial coverage cannot prove recent continuous financial history."
    ),
    WorkspaceForecastReasonCode.UNKNOWN_COVERAGE: (
        "Confirm whether the statement period is complete before forecasting."
    ),
    WorkspaceForecastReasonCode.RECENT_COVERAGE_GAP: (
        "A recent statement gap breaks the continuous history required to forecast."
    ),
    WorkspaceForecastReasonCode.INSUFFICIENT_HISTORY: (
        "At least 60 recent consecutive covered days are required for this forecast."
    ),
}


def forecast_reason_messages(
    reasons: tuple[WorkspaceForecastReasonCode, ...],
) -> tuple[str, ...]:
    """Translate controlled refusal codes into concise corrective guidance."""
    return tuple(_FORECAST_REASON_MESSAGES[reason] for reason in reasons)


_FORECAST_WARNING_MESSAGES = {
    WorkspaceForecastWarningCode.ONE_SHOT_WORKSPACE_BASELINE: (
        "This uses a cautious recent-history baseline, not a validated personal model."
    ),
    WorkspaceForecastWarningCode.EMPIRICAL_INTERVAL_ESTIMATE: (
        "The shaded interval is estimated from past variation and is not a guarantee."
    ),
    WorkspaceForecastWarningCode.UNCLASSIFIED_TOTAL_CASH_FLOW: (
        "This workspace has no confirmed recurring-series labels, so the baseline "
        "models total account movement without calling any portion discretionary."
    ),
    WorkspaceForecastWarningCode.BALANCE_BRIDGED_TO_TODAY: (
        "The last confirmed balance was carried to today; uncovered bridge days use "
        "the same cautious baseline."
    ),
}


def forecast_warning_messages(
    warnings: tuple[WorkspaceForecastWarningCode, ...],
) -> tuple[str, ...]:
    """Translate model limitations without overstating forecast certainty."""
    return tuple(_FORECAST_WARNING_MESSAGES[warning] for warning in warnings)


__all__ = [
    "WorkspaceBalanceSummary",
    "analytics_messages",
    "category_donut_chart",
    "coverage_chart",
    "forecast_chart",
    "forecast_reason_messages",
    "forecast_warning_messages",
    "money_text",
    "monthly_cash_flow_chart",
    "transaction_rows",
    "workspace_balance_hero_html",
    "workspace_balance_summary",
    "workspace_pulse_html",
]
