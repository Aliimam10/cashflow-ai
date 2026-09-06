"""Pure display projections for the transaction review and analytics page."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from cashflow_ai.schemas.analytics import CashFlowAnalytics, DataCoverageIndicator
from cashflow_ai.schemas.api import TransactionResponse


def money_text(value: Decimal, currency: str) -> str:
    """Format an API decimal without converting financial values to float."""
    return f"{currency} {value:,.2f}"


def transaction_rows(
    transactions: tuple[TransactionResponse, ...],
    *,
    account_names: dict[str, str],
    category_names: dict[str, str],
) -> list[dict[str, object]]:
    """Build a readable table while retaining the stable transaction identifier."""
    return [
        {
            "Date": item.transaction_date.isoformat(),
            "Account": account_names.get(item.account_id, "Unknown account"),
            "Description": item.description,
            "Amount": money_text(item.amount, item.currency.value),
            "Category": category_names.get(item.category_id or "", "Uncategorised"),
            "Financial role": item.financial_role.value.replace("_", " "),
            "Transaction ID": item.transaction_id,
        }
        for item in transactions
    ]


def coverage_rows(coverage: DataCoverageIndicator) -> list[dict[str, object]]:
    """Project explicit known and unknown periods for a timeline chart."""
    periods = (
        ("Fully covered", coverage.fully_covered_periods),
        ("Partially covered", coverage.partially_covered_periods),
        ("Missing", coverage.missing_periods),
    )
    return [
        {
            "status": status,
            "start": period.start_date.isoformat(),
            "end": period.end_date.isoformat(),
        }
        for status, ranges in periods
        for period in ranges
    ]


def coverage_chart(coverage: DataCoverageIndicator) -> dict[str, Any]:
    """Return a gap-visible Vega-Lite timeline specification."""
    return {
        "data": {"values": coverage_rows(coverage)},
        "mark": {"type": "bar", "height": 28},
        "encoding": {
            "x": {"field": "start", "type": "temporal", "title": "Statement date"},
            "x2": {"field": "end"},
            "color": {
                "field": "status",
                "type": "nominal",
                "scale": {
                    "domain": ["Fully covered", "Partially covered", "Missing"],
                    "range": ["#52D98F", "#F5A623", "#FF6B78"],
                },
            },
            "tooltip": ["status", "start", "end"],
        },
    }


def balance_chart(analytics: CashFlowAnalytics) -> dict[str, Any]:
    """Return balance lines split at coverage gaps, with no interpolation."""
    values = [
        {
            "account_id": history.account_id,
            "segment": f"{history.account_id}:{segment_index}",
            "date": point.as_of_date.isoformat(),
            "balance": float(point.balance),
        }
        for history in analytics.balance_history
        for segment_index, segment in enumerate(history.segments)
        for point in segment.points
    ]
    return {
        "data": {"values": values},
        "mark": {"type": "line", "point": True},
        "encoding": {
            "x": {"field": "date", "type": "temporal", "title": "Date"},
            "y": {
                "field": "balance",
                "type": "quantitative",
                "title": f"Balance ({analytics.currency.value})",
                "scale": {"zero": False},
            },
            "color": {"field": "account_id", "type": "nominal"},
            "detail": {"field": "segment"},
            "tooltip": ["account_id", "date", "balance"],
        },
    }


def category_chart(analytics: CashFlowAnalytics) -> dict[str, Any]:
    """Return the requested observed-only category-spending donut."""
    values = [
        {
            "category": item.category_name or "Uncategorised",
            "amount": float(item.amount),
            "transactions": item.transaction_count,
        }
        for item in analytics.category_spending or ()
    ]
    return {
        "data": {"values": values},
        "mark": {
            "type": "arc",
            "innerRadius": 58,
            "outerRadius": 105,
            "cornerRadius": 4,
            "padAngle": 0.02,
        },
        "encoding": {
            "theta": {
                "field": "amount",
                "type": "quantitative",
            },
            "color": {
                "field": "category",
                "type": "nominal",
                "sort": "-theta",
                "scale": {
                    "range": [
                        "#5B8DEF",
                        "#52D98F",
                        "#F5A623",
                        "#A78BFA",
                        "#38BDF8",
                        "#FF6B78",
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


def monthly_cash_flow_chart(analytics: CashFlowAnalytics) -> dict[str, Any]:
    """Return monthly income/expense bars with a net-cash-flow pulse line."""
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
    return {
        "data": {"values": values},
        "layer": [
            {
                "transform": [
                    {"filter": "datum.flow !== 'Net cash flow'"},
                ],
                "mark": {
                    "type": "bar",
                    "cornerRadiusTopLeft": 3,
                    "cornerRadiusTopRight": 3,
                },
                "encoding": {
                    "x": {
                        "field": "month",
                        "type": "temporal",
                        "timeUnit": "yearmonth",
                        "title": "Month",
                    },
                    "y": {
                        "field": "amount",
                        "type": "quantitative",
                        "title": f"Observed amount ({analytics.currency.value})",
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
                    "xOffset": {"field": "flow"},
                    "tooltip": ["month", "flow", "amount", "coverage"],
                },
            },
            {
                "transform": [
                    {"filter": "datum.flow === 'Net cash flow'"},
                ],
                "mark": {
                    "type": "line",
                    "point": {"filled": True, "size": 48},
                    "strokeWidth": 2.5,
                    "color": "#5B8DEF",
                },
                "encoding": {
                    "x": {
                        "field": "month",
                        "type": "temporal",
                        "timeUnit": "yearmonth",
                    },
                    "y": {"field": "amount", "type": "quantitative"},
                    "tooltip": ["month", "flow", "amount", "coverage"],
                },
            },
        ],
        "resolve": {"scale": {"y": "shared"}},
    }


def cadence_chart(analytics: CashFlowAnalytics) -> dict[str, Any]:
    """Return recurring, discretionary, and unclassified observed expenses."""
    cadence = analytics.spending_cadence
    values = (
        []
        if cadence is None
        else [
            {"cadence": "Recurring", "amount": float(cadence.recurring)},
            {"cadence": "Discretionary", "amount": float(cadence.discretionary)},
            {"cadence": "Unclassified", "amount": float(cadence.unclassified)},
        ]
    )
    return {
        "data": {"values": values},
        "mark": {"type": "arc", "innerRadius": 45},
        "encoding": {
            "theta": {"field": "amount", "type": "quantitative"},
            "color": {
                "field": "cadence",
                "type": "nominal",
                "scale": {
                    "domain": ["Recurring", "Discretionary", "Unclassified"],
                    "range": ["#5B8DEF", "#F5A623", "#697587"],
                },
            },
            "tooltip": ["cadence", "amount"],
        },
    }


__all__ = [
    "balance_chart",
    "cadence_chart",
    "category_chart",
    "coverage_chart",
    "coverage_rows",
    "money_text",
    "monthly_cash_flow_chart",
    "transaction_rows",
]
