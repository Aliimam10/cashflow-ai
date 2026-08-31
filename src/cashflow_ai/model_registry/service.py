"""Application service for the lightweight local model registry."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence.base import utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import ModelMetadataRecord
from cashflow_ai.persistence.repositories import ModelMetadataRepository
from cashflow_ai.schemas.model_registry import (
    ModelActivation,
    ModelFeatureSchema,
    ModelMetric,
    ModelParameter,
    ModelRegistration,
    ModelTask,
    RegisteredModel,
)

_METADATA_FORMAT_VERSION = "1.0"


class ModelRegistryErrorCode(StrEnum):
    """Stable, privacy-safe failures at the registry boundary."""

    DUPLICATE_VERSION = "duplicate_version"
    MODEL_NOT_FOUND = "model_not_found"
    TASK_MISMATCH = "task_mismatch"
    NOT_ACTIVATION_ELIGIBLE = "not_activation_eligible"
    INVALID_STORED_METADATA = "invalid_stored_metadata"
    WRITE_CONFLICT = "write_conflict"


class ModelRegistryError(ValueError):
    """Controlled registry failure that does not reveal financial data."""

    def __init__(self, code: ModelRegistryErrorCode, message: str) -> None:
        """Store a stable error code beside a safe message."""
        super().__init__(message)
        self.code = code


def _current_items(payload: object, key: str) -> tuple[object, ...]:
    if not isinstance(payload, dict):
        raise ValueError("metadata payload is not an object")
    items = payload.get(key)
    if not isinstance(items, list):
        raise ValueError("metadata payload does not contain a list")
    return tuple(items)


def _record_to_contract(record: ModelMetadataRecord) -> RegisteredModel:
    try:
        if record.metadata_format_version == _METADATA_FORMAT_VERSION:
            metrics = tuple(
                ModelMetric.model_validate(item)
                for item in _current_items(record.metrics_json, "metrics")
            )
            parameters = tuple(
                ModelParameter.model_validate(item)
                for item in _current_items(record.parameters_json, "parameters")
            )
        elif record.metadata_format_version == "legacy-0":
            metrics = ()
            parameters = ()
        else:
            raise ValueError("unsupported metadata format")
        feature_names = tuple(record.feature_names_json)
        if any(not isinstance(item, str) for item in feature_names):
            raise ValueError("feature names are not strings")
        return RegisteredModel(
            model_id=record.id,
            model_name=record.model_name,
            model_type=record.model_type,
            model_version=record.model_version,
            task=ModelTask(record.task),
            training_start_date=record.training_start_date,
            training_end_date=record.training_end_date,
            feature_schema=ModelFeatureSchema(
                version=record.feature_schema_version,
                feature_names=feature_names,
            ),
            taxonomy_version=record.taxonomy_version,
            metrics=metrics,
            parameters=parameters,
            artifact_path=record.artifact_path,
            created_at=record.created_at,
            metadata_format_version=record.metadata_format_version,
            activation_eligible=record.activation_eligible,
            is_active=record.is_active,
            activated_at=record.activated_at,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ModelRegistryError(
            ModelRegistryErrorCode.INVALID_STORED_METADATA,
            "stored model metadata is invalid or unsupported",
        ) from exc


def _metrics_payload(registration: ModelRegistration) -> dict[str, Any]:
    return {"metrics": [item.model_dump(mode="json") for item in registration.metrics]}


def _parameters_payload(registration: ModelRegistration) -> dict[str, Any]:
    return {
        "parameters": [item.model_dump(mode="json") for item in registration.parameters]
    }


def register_model(
    factory: sessionmaker[Session],
    *,
    registration: ModelRegistration,
) -> RegisteredModel:
    """Record one immutable evaluated version without activating it."""
    try:
        with session_scope(factory) as session:
            repository = ModelMetadataRepository(session)
            if (
                repository.get_version(
                    model_name=registration.model_name,
                    model_version=registration.model_version,
                )
                is not None
            ):
                raise ModelRegistryError(
                    ModelRegistryErrorCode.DUPLICATE_VERSION,
                    "model name and version are already registered",
                )
            record = repository.add(
                ModelMetadataRecord(
                    model_name=registration.model_name,
                    model_type=registration.model_type,
                    model_version=registration.model_version,
                    task=registration.task.value,
                    artifact_path=registration.artifact_path,
                    training_cutoff=registration.training_end_date,
                    training_start_date=registration.training_start_date,
                    training_end_date=registration.training_end_date,
                    feature_schema_version=registration.feature_schema.version,
                    feature_names_json=list(registration.feature_schema.feature_names),
                    taxonomy_version=registration.taxonomy_version,
                    metrics_json=_metrics_payload(registration),
                    parameters_json=_parameters_payload(registration),
                    metadata_format_version=_METADATA_FORMAT_VERSION,
                    activation_eligible=registration.activation_eligible,
                    is_active=False,
                    activated_at=None,
                    created_at=registration.created_at,
                )
            )
            return _record_to_contract(record)
    except IntegrityError as exc:
        raise ModelRegistryError(
            ModelRegistryErrorCode.WRITE_CONFLICT,
            "model metadata changed during registration; retry safely",
        ) from exc


def list_registered_models(
    factory: sessionmaker[Session],
    *,
    task: ModelTask | None = None,
) -> tuple[RegisteredModel, ...]:
    """List registered versions, optionally limited to one modelling task."""
    with session_scope(factory) as session:
        records = ModelMetadataRepository(session).list(
            task=None if task is None else task.value
        )
        return tuple(_record_to_contract(record) for record in records)


def get_active_model(
    factory: sessionmaker[Session],
    *,
    task: ModelTask,
) -> RegisteredModel | None:
    """Return the explicitly selected local version for a modelling task."""
    with session_scope(factory) as session:
        record = ModelMetadataRepository(session).get_active(task=task.value)
        return None if record is None else _record_to_contract(record)


def activate_model(
    factory: sessionmaker[Session],
    *,
    task: ModelTask,
    model_id: str,
) -> ModelActivation:
    """Atomically select one eligible version and deactivate its predecessor."""
    try:
        with session_scope(factory) as session:
            repository = ModelMetadataRepository(session)
            selected = repository.get(model_id)
            if selected is None:
                raise ModelRegistryError(
                    ModelRegistryErrorCode.MODEL_NOT_FOUND,
                    "registered model does not exist",
                )
            if selected.task != task.value:
                raise ModelRegistryError(
                    ModelRegistryErrorCode.TASK_MISMATCH,
                    "registered model belongs to a different modelling task",
                )
            if not selected.activation_eligible:
                raise ModelRegistryError(
                    ModelRegistryErrorCode.NOT_ACTIVATION_ELIGIBLE,
                    "registered model did not pass its activation gate",
                )
            active = repository.get_active(task=task.value)
            if active is selected:
                return ModelActivation(
                    task=task,
                    active_model=_record_to_contract(selected),
                )
            previous = repository.deactivate_for_task(task=task.value)
            session.flush()
            selected.is_active = True
            selected.activated_at = utc_now()
            session.flush()
            return ModelActivation(
                task=task,
                active_model=_record_to_contract(selected),
                previous_active_model_id=None if previous is None else previous.id,
            )
    except IntegrityError as exc:
        raise ModelRegistryError(
            ModelRegistryErrorCode.WRITE_CONFLICT,
            "model activation conflicted with another local write; retry safely",
        ) from exc


__all__ = [
    "ModelRegistryError",
    "ModelRegistryErrorCode",
    "activate_model",
    "get_active_model",
    "list_registered_models",
    "register_model",
]
