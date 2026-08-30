"""Public coverage-aware forecast-data and baseline boundary."""

from cashflow_ai.forecasting.model import (
    TrainedPrimaryForecaster,
    predict_discretionary_spending,
    train_primary_forecaster,
)
from cashflow_ai.forecasting.paths import (
    ForecastPathError,
    ForecastPathErrorCode,
    build_balance_forecast_path,
)
from cashflow_ai.forecasting.service import (
    ForecastingDataError,
    ForecastingDataErrorCode,
    build_forecast_dataset,
    build_forecast_feature_rows,
    build_next_forecast_inference_row,
    evaluate_forecast_baselines,
    validate_forecast_dataset,
)

__all__ = [
    "ForecastPathError",
    "ForecastPathErrorCode",
    "ForecastingDataError",
    "ForecastingDataErrorCode",
    "TrainedPrimaryForecaster",
    "build_balance_forecast_path",
    "build_forecast_dataset",
    "build_forecast_feature_rows",
    "build_next_forecast_inference_row",
    "evaluate_forecast_baselines",
    "predict_discretionary_spending",
    "train_primary_forecaster",
    "validate_forecast_dataset",
]
