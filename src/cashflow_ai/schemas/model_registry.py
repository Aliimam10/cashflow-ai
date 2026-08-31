"""Data-minimised contracts for the lightweight local model registry."""

from __future__ import annotations

from datetime import UTC, date
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.transactions import Identifier

SafeRegistryName = Annotated[
    str,
    Field(pattern=r"^[a-z0-9][a-z0-9._-]*$", min_length=1, max_length=100),
]
SafeSchemaName = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9_]*$", min_length=1, max_length=100),
]


class _RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ModelTask(StrEnum):
    """Supported local modelling responsibilities with one active version each."""

    TRANSACTION_CATEGORISATION = "transaction_categorisation"
    CASH_FLOW_FORECASTING = "cash_flow_forecasting"
    TRANSACTION_ANOMALY_DETECTION = "transaction_anomaly_detection"


class ModelMetricUnit(StrEnum):
    """Controlled units that keep evaluation values interpretable."""

    SCORE = "score"
    RATIO = "ratio"
    COUNT = "count"
    GBP = "gbp"
    DAYS = "days"
    NONE = "none"


class ModelMetric(_RegistryModel):
    """One aggregate evaluation value without transaction-level predictions."""

    name: SafeSchemaName
    evaluation_slice: SafeSchemaName
    value: Decimal = Field(max_digits=24, decimal_places=8)
    unit: ModelMetricUnit


class ModelParameter(_RegistryModel):
    """One controlled reproducibility setting serialised without arbitrary objects."""

    name: SafeSchemaName
    value: str = Field(min_length=1, max_length=250)


class ModelFeatureSchema(_RegistryModel):
    """Versioned conceptual features used by one registered model."""

    version: SafeRegistryName
    feature_names: tuple[SafeSchemaName, ...] = ()

    @model_validator(mode="after")
    def validate_unique_names(self) -> ModelFeatureSchema:
        """Prevent ambiguous repeated feature identities."""
        if len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("model feature names must be unique")
        return self


class ModelRegistration(_RegistryModel):
    """Immutable metadata proposed for one evaluated local model version."""

    model_name: SafeRegistryName
    model_type: SafeRegistryName
    model_version: SafeRegistryName
    task: ModelTask
    training_start_date: date
    training_end_date: date
    feature_schema: ModelFeatureSchema
    taxonomy_version: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=50,
    )
    metrics: tuple[ModelMetric, ...] = Field(min_length=1)
    parameters: tuple[ModelParameter, ...] = ()
    artifact_path: str | None = Field(default=None, max_length=500)
    created_at: AwareDatetime
    activation_eligible: bool

    @model_validator(mode="after")
    def validate_metadata(self) -> ModelRegistration:
        """Require coherent dates, safe paths, and unique aggregate identities."""
        if self.training_end_date < self.training_start_date:
            raise ValueError("model training end date cannot precede its start date")
        if not self.feature_schema.feature_names:
            raise ValueError("registered models require at least one feature name")
        if self.created_at.astimezone(UTC).date() < self.training_end_date:
            raise ValueError("model creation time cannot precede its training data")
        metric_keys = tuple(
            (metric.evaluation_slice, metric.name) for metric in self.metrics
        )
        if len(set(metric_keys)) != len(metric_keys):
            raise ValueError("model metric identities must be unique")
        parameter_names = tuple(parameter.name for parameter in self.parameters)
        if len(set(parameter_names)) != len(parameter_names):
            raise ValueError("model parameter names must be unique")
        if self.task is ModelTask.TRANSACTION_CATEGORISATION and (
            self.taxonomy_version is None or self.artifact_path is None
        ):
            raise ValueError(
                "categorisation metadata requires taxonomy and artifact provenance"
            )
        if self.artifact_path is not None:
            path = PurePosixPath(self.artifact_path)
            if (
                "\\" in self.artifact_path
                or path.is_absolute()
                or not path.parts
                or path.parts[0] != "models"
                or ".." in path.parts
            ):
                raise ValueError(
                    "model artifact path must be relative and inside models/"
                )
        return self


class RegisteredModel(_RegistryModel):
    """Stored model metadata plus its current local activation state."""

    model_id: Identifier
    model_name: SafeRegistryName
    model_type: SafeRegistryName
    model_version: SafeRegistryName
    task: ModelTask
    training_start_date: date
    training_end_date: date
    feature_schema: ModelFeatureSchema
    taxonomy_version: str | None = None
    metrics: tuple[ModelMetric, ...]
    parameters: tuple[ModelParameter, ...]
    artifact_path: str | None = None
    created_at: AwareDatetime
    metadata_format_version: str = Field(min_length=1, max_length=20)
    activation_eligible: bool
    is_active: bool
    activated_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_activation(self) -> RegisteredModel:
        """An active record must be eligible and carry an activation timestamp."""
        if self.is_active and (
            not self.activation_eligible or self.activated_at is None
        ):
            raise ValueError(
                "active model metadata lacks eligibility or activation time"
            )
        return self


class ModelActivation(_RegistryModel):
    """Result of atomically selecting one model for a task."""

    task: ModelTask
    active_model: RegisteredModel
    previous_active_model_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_active_model(self) -> ModelActivation:
        """Keep activation task and selected record aligned."""
        if self.active_model.task is not self.task or not self.active_model.is_active:
            raise ValueError("model activation result is inconsistent")
        if self.previous_active_model_id == self.active_model.model_id:
            raise ValueError(
                "previous active model must differ from the selected model"
            )
        return self


__all__ = [
    "ModelActivation",
    "ModelFeatureSchema",
    "ModelMetric",
    "ModelMetricUnit",
    "ModelParameter",
    "ModelRegistration",
    "ModelTask",
    "RegisteredModel",
    "SafeRegistryName",
    "SafeSchemaName",
]
