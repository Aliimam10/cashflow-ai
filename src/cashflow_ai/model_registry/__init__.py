"""Lightweight local model metadata and activation lifecycle."""

from cashflow_ai.model_registry.adapters import (
    registration_from_anomaly_detection,
    registration_from_categorisation,
    registration_from_forecast,
)
from cashflow_ai.model_registry.service import (
    ModelRegistryError,
    ModelRegistryErrorCode,
    activate_model,
    get_active_model,
    list_registered_models,
    register_model,
)

__all__ = [
    "ModelRegistryError",
    "ModelRegistryErrorCode",
    "activate_model",
    "get_active_model",
    "list_registered_models",
    "register_model",
    "registration_from_anomaly_detection",
    "registration_from_categorisation",
    "registration_from_forecast",
]
