from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.model_registry import (
    ModelRegistryError,
    ModelRegistryErrorCode,
    activate_model,
    get_active_model,
    list_registered_models,
    register_model,
)
from cashflow_ai.model_registry.demo import main as demo_main
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import ModelMetadataRecord
from cashflow_ai.persistence.repositories import ModelMetadataRepository
from cashflow_ai.schemas.model_registry import (
    ModelActivation,
    ModelFeatureSchema,
    ModelMetric,
    ModelMetricUnit,
    ModelParameter,
    ModelRegistration,
    ModelTask,
    RegisteredModel,
)


@pytest.fixture
def engine() -> Engine:
    value = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(value)
    return value


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _registration(**overrides: Any) -> ModelRegistration:
    values: dict[str, Any] = {
        "model_name": "weekly_discretionary_cash_flow",
        "model_type": "hist_gradient_boosting",
        "model_version": "synthetic-1",
        "task": ModelTask.CASH_FLOW_FORECASTING,
        "training_start_date": date(2025, 1, 6),
        "training_end_date": date(2025, 6, 30),
        "feature_schema": ModelFeatureSchema(
            version="weekly_cash_flow_v1",
            feature_names=("lag_1", "rolling_mean_4"),
        ),
        "metrics": (
            ModelMetric(
                name="mae",
                evaluation_slice="final_test",
                value=Decimal("12.50"),
                unit=ModelMetricUnit.GBP,
            ),
        ),
        "parameters": (ModelParameter(name="random_seed", value="7"),),
        "created_at": datetime(2025, 7, 1, tzinfo=UTC),
        "activation_eligible": True,
    }
    values.update(overrides)
    return ModelRegistration(**values)


def _registered(**overrides: Any) -> RegisteredModel:
    registration = _registration()
    values = {
        "model_id": "model-1",
        **registration.model_dump(),
        "metadata_format_version": "1.0",
        "is_active": True,
        "activated_at": datetime(2025, 7, 2, tzinfo=UTC),
    }
    values.update(overrides)
    return RegisteredModel(**values)


def test_registry_registers_lists_activates_and_replaces_versions(
    factory: sessionmaker[Session],
) -> None:
    first = register_model(factory, registration=_registration())
    second = register_model(
        factory,
        registration=_registration(
            model_version="synthetic-2",
            created_at=datetime(2025, 7, 2, tzinfo=UTC),
        ),
    )

    assert first.model_id != second.model_id
    assert first.metrics[0].value == Decimal("12.50000000")
    assert first.parameters[0].value == "7"
    assert first.feature_schema.feature_names == ("lag_1", "rolling_mean_4")
    assert first.artifact_path is None
    assert first.taxonomy_version is None
    assert first.metadata_format_version == "1.0"
    assert get_active_model(factory, task=ModelTask.CASH_FLOW_FORECASTING) is None
    assert list_registered_models(factory) == (first, second)
    assert (
        list_registered_models(factory, task=ModelTask.TRANSACTION_CATEGORISATION) == ()
    )

    first_activation = activate_model(
        factory,
        task=ModelTask.CASH_FLOW_FORECASTING,
        model_id=first.model_id,
    )
    assert first_activation.previous_active_model_id is None
    assert first_activation.active_model.is_active
    assert first_activation.active_model.activated_at is not None
    assert (
        get_active_model(factory, task=ModelTask.CASH_FLOW_FORECASTING)
        == first_activation.active_model
    )

    unchanged = activate_model(
        factory,
        task=ModelTask.CASH_FLOW_FORECASTING,
        model_id=first.model_id,
    )
    assert unchanged == first_activation

    replacement = activate_model(
        factory,
        task=ModelTask.CASH_FLOW_FORECASTING,
        model_id=second.model_id,
    )
    assert replacement.previous_active_model_id == first.model_id
    assert replacement.active_model.model_id == second.model_id
    stored = list_registered_models(factory, task=ModelTask.CASH_FLOW_FORECASTING)
    assert [item.is_active for item in stored] == [False, True]
    assert stored[0].activated_at == first_activation.active_model.activated_at


def test_registry_rejects_duplicate_wrong_task_missing_and_ineligible_versions(
    factory: sessionmaker[Session],
) -> None:
    registered = register_model(factory, registration=_registration())
    with pytest.raises(ModelRegistryError) as duplicate:
        register_model(factory, registration=_registration())
    assert duplicate.value.code is ModelRegistryErrorCode.DUPLICATE_VERSION

    with pytest.raises(ModelRegistryError) as missing:
        activate_model(
            factory,
            task=ModelTask.CASH_FLOW_FORECASTING,
            model_id="missing",
        )
    assert missing.value.code is ModelRegistryErrorCode.MODEL_NOT_FOUND

    with pytest.raises(ModelRegistryError) as wrong_task:
        activate_model(
            factory,
            task=ModelTask.TRANSACTION_ANOMALY_DETECTION,
            model_id=registered.model_id,
        )
    assert wrong_task.value.code is ModelRegistryErrorCode.TASK_MISMATCH

    ineligible = register_model(
        factory,
        registration=_registration(
            model_version="fallback-1",
            model_type="low_data_fallback",
            activation_eligible=False,
        ),
    )
    with pytest.raises(ModelRegistryError) as not_eligible:
        activate_model(
            factory,
            task=ModelTask.CASH_FLOW_FORECASTING,
            model_id=ineligible.model_id,
        )
    assert not_eligible.value.code is ModelRegistryErrorCode.NOT_ACTIVATION_ELIGIBLE


def test_legacy_registry_rows_remain_inspectable_but_inactive(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        session.add(
            ModelMetadataRecord(
                id="legacy-model",
                model_name="legacy_model",
                model_type="legacy_model",
                model_version="v1",
                task=ModelTask.CASH_FLOW_FORECASTING.value,
                artifact_path=None,
                training_cutoff=date(2024, 1, 1),
                training_start_date=date(2024, 1, 1),
                training_end_date=date(2024, 1, 1),
                feature_schema_version="legacy_unknown",
                feature_names_json=[],
                taxonomy_version=None,
                metrics_json={"old": 1},
                parameters_json={"old": True},
                metadata_format_version="legacy-0",
                activation_eligible=False,
                is_active=False,
                activated_at=None,
                created_at=datetime(2024, 1, 2, tzinfo=UTC),
            )
        )

    (stored,) = list_registered_models(factory)
    assert stored.model_id == "legacy-model"
    assert stored.metrics == ()
    assert stored.parameters == ()
    assert stored.feature_schema.feature_names == ()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("metrics_json", []),
        ("parameters_json", {}),
        ("feature_names_json", [1]),
        ("metadata_format_version", "future-9"),
    ],
)
def test_registry_rejects_corrupt_or_unsupported_stored_metadata(
    factory: sessionmaker[Session], attribute: str, value: object
) -> None:
    registered = register_model(factory, registration=_registration())
    with session_scope(factory) as session:
        record = ModelMetadataRepository(session).get(registered.model_id)
        assert record is not None
        setattr(record, attribute, value)

    with pytest.raises(ModelRegistryError) as error:
        list_registered_models(factory)
    assert error.value.code is ModelRegistryErrorCode.INVALID_STORED_METADATA


def test_registry_translates_database_write_conflicts(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_add(
        repository: ModelMetadataRepository,
        metadata: ModelMetadataRecord,
    ) -> ModelMetadataRecord:
        del repository, metadata
        raise IntegrityError("insert", {}, RuntimeError("synthetic conflict"))

    monkeypatch.setattr(ModelMetadataRepository, "add", fail_add)
    with pytest.raises(ModelRegistryError) as registration_error:
        register_model(factory, registration=_registration())
    assert registration_error.value.code is ModelRegistryErrorCode.WRITE_CONFLICT


def test_registry_translates_activation_write_conflicts(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = register_model(factory, registration=_registration())

    def fail_deactivate(
        repository: ModelMetadataRepository, *, task: str
    ) -> ModelMetadataRecord | None:
        del repository, task
        raise IntegrityError("update", {}, RuntimeError("synthetic conflict"))

    monkeypatch.setattr(
        ModelMetadataRepository,
        "deactivate_for_task",
        fail_deactivate,
    )
    with pytest.raises(ModelRegistryError) as activation_error:
        activate_model(
            factory,
            task=ModelTask.CASH_FLOW_FORECASTING,
            model_id=registered.model_id,
        )
    assert activation_error.value.code is ModelRegistryErrorCode.WRITE_CONFLICT


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"training_end_date": date(2024, 12, 31)},
            "training end date cannot precede",
        ),
        (
            {
                "feature_schema": ModelFeatureSchema(
                    version="empty_v1", feature_names=()
                )
            },
            "at least one feature name",
        ),
        (
            {"created_at": datetime(2025, 6, 1, tzinfo=UTC)},
            "creation time cannot precede",
        ),
        (
            {
                "metrics": (
                    ModelMetric(
                        name="mae",
                        evaluation_slice="final_test",
                        value=Decimal("1"),
                        unit=ModelMetricUnit.GBP,
                    ),
                )
                * 2
            },
            "metric identities must be unique",
        ),
        (
            {
                "parameters": (
                    ModelParameter(name="random_seed", value="1"),
                    ModelParameter(name="random_seed", value="2"),
                )
            },
            "parameter names must be unique",
        ),
        (
            {
                "task": ModelTask.TRANSACTION_CATEGORISATION,
                "taxonomy_version": None,
                "artifact_path": "models/category/model.joblib",
            },
            "requires taxonomy and artifact",
        ),
        (
            {
                "task": ModelTask.TRANSACTION_CATEGORISATION,
                "taxonomy_version": "1.0",
                "artifact_path": None,
            },
            "requires taxonomy and artifact",
        ),
    ],
)
def test_registration_contract_rejects_incoherent_metadata(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _registration(**overrides)


@pytest.mark.parametrize(
    "artifact_path",
    [
        "",
        "/models/category/model.joblib",
        "private/category/model.joblib",
        "models/category/../model.joblib",
        "models\\category\\model.joblib",
    ],
)
def test_registration_contract_rejects_unsafe_artifact_paths(
    artifact_path: str,
) -> None:
    with pytest.raises(ValidationError, match="relative and inside models"):
        _registration(artifact_path=artifact_path)


def test_feature_and_activation_contracts_reject_ambiguous_state() -> None:
    with pytest.raises(ValidationError, match="feature names must be unique"):
        ModelFeatureSchema(version="features_v1", feature_names=("lag_1", "lag_1"))
    with pytest.raises(ValidationError, match="eligibility or activation time"):
        _registered(activation_eligible=False)
    with pytest.raises(ValidationError, match="eligibility or activation time"):
        _registered(activated_at=None)
    with pytest.raises(ValidationError, match="activation result is inconsistent"):
        ModelActivation(
            task=ModelTask.TRANSACTION_CATEGORISATION,
            active_model=_registered(),
        )
    with pytest.raises(ValidationError, match="activation result is inconsistent"):
        ModelActivation(
            task=ModelTask.CASH_FLOW_FORECASTING,
            active_model=_registered(is_active=False),
        )
    with pytest.raises(ValidationError, match="previous active model must differ"):
        ModelActivation(
            task=ModelTask.CASH_FLOW_FORECASTING,
            active_model=_registered(),
            previous_active_model_id="model-1",
        )


@pytest.mark.parametrize(
    ("selected", "replaced"),
    [("synthetic-1", "false"), ("synthetic-2", "true")],
)
def test_manual_demo_reports_explicit_synthetic_activation(
    selected: str,
    replaced: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "sys.argv", ["cashflow-model-registry-demo", "--activate", selected]
    )
    demo_main()

    output = capsys.readouterr().out
    assert "registered versions: 2" in output
    assert f"active version: {selected}" in output
    assert f"previous active version replaced: {replaced}" in output
    assert "transaction-level financial data stored: false" in output
