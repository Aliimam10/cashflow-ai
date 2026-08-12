"""Public coverage-aware cash-flow analytics boundary."""

from cashflow_ai.analytics.service import (
    AnalyticsServiceError,
    AnalyticsServiceErrorCode,
    compute_cash_flow_analytics,
)

__all__ = [
    "AnalyticsServiceError",
    "AnalyticsServiceErrorCode",
    "compute_cash_flow_analytics",
]
