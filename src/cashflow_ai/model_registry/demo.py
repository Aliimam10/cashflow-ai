"""Human-readable synthetic demonstration of the local model registry."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal

from cashflow_ai.model_registry.service import activate_model, register_model
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.schemas.model_registry import (
    ModelFeatureSchema,
    ModelMetric,
    ModelMetricUnit,
    ModelParameter,
    ModelRegistration,
    ModelTask,
)


def _registration(version: str, *, mae: str) -> ModelRegistration:
    return ModelRegistration(
        model_name="synthetic_weekly_forecast",
        model_type="hist_gradient_boosting",
        model_version=version,
        task=ModelTask.CASH_FLOW_FORECASTING,
        training_start_date=date(2025, 1, 6),
        training_end_date=date(2025, 6, 30),
        feature_schema=ModelFeatureSchema(
            version="weekly_cash_flow_v1",
            feature_names=("lag_1", "rolling_mean_4"),
        ),
        metrics=(
            ModelMetric(
                name="mae",
                evaluation_slice="synthetic_final_test",
                value=Decimal(mae),
                unit=ModelMetricUnit.GBP,
            ),
        ),
        parameters=(ModelParameter(name="random_seed", value="7"),),
        created_at=datetime(2025, 7, 1, tzinfo=UTC),
        activation_eligible=True,
    )


def main() -> None:
    """Register two fictional versions and explicitly select one."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--activate",
        choices=("synthetic-1", "synthetic-2"),
        default="synthetic-2",
        help="fictional version that should finish active",
    )
    args = parser.parse_args()

    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    versions = (
        register_model(factory, registration=_registration("synthetic-1", mae="18")),
        register_model(factory, registration=_registration("synthetic-2", mae="12")),
    )
    first_activation = activate_model(
        factory,
        task=ModelTask.CASH_FLOW_FORECASTING,
        model_id=versions[0].model_id,
    )
    activation = first_activation
    if args.activate == "synthetic-2":
        activation = activate_model(
            factory,
            task=ModelTask.CASH_FLOW_FORECASTING,
            model_id=versions[1].model_id,
        )

    print("CashFlow AI synthetic model-registry check")
    print(f"registered versions: {len(versions)}")
    print(f"active task: {activation.task.value}")
    print(f"active version: {activation.active_model.model_version}")
    print(
        "previous active version replaced: "
        f"{str(activation.previous_active_model_id is not None).lower()}"
    )
    print(
        "active synthetic final-test MAE: "
        f"GBP {activation.active_model.metrics[0].value}"
    )
    print("transaction-level financial data stored: false")


if __name__ == "__main__":  # pragma: no cover - exercised through console entry point
    main()
