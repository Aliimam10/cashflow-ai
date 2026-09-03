"""Public review-only transaction anomaly detection boundary."""

from cashflow_ai.anomalies.service import (
    AnomalyDetectionError,
    AnomalyDetectionErrorCode,
    detect_unusual_transactions,
    record_anomaly_feedback,
)

__all__ = [
    "AnomalyDetectionError",
    "AnomalyDetectionErrorCode",
    "detect_unusual_transactions",
    "record_anomaly_feedback",
]
