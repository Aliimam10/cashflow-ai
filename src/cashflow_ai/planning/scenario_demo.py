"""Readable synthetic demonstration of isolated financial scenarios."""

from __future__ import annotations

import argparse
import calendar
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.forecasting.model import train_primary_forecaster
from cashflow_ai.forecasting.path_demo import _seed_database, _synthetic_dataset
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    BudgetRecord,
    CategoryRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    SavingsGoalRecord,
    StatementCoverageRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.planning.scenarios import evaluate_financial_scenario
from cashflow_ai.schemas.forecast_models import ForecastModelPolicy
from cashflow_ai.schemas.forecast_paths import ForecastPathPlan, ForecastPathPolicy
from cashflow_ai.schemas.forecasting import ForecastDataset
from cashflow_ai.schemas.freshness import FreshnessPolicy
from cashflow_ai.schemas.planning import PlanningEvaluationPlan
from cashflow_ai.schemas.recurrence import RecurrenceFrequency
from cashflow_ai.schemas.scenarios import FinancialScenario, FinancialScenarioType


def _month_end(value: date) -> date:
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def _seed_planning_evidence(
    factory: sessionmaker[Session], dataset: ForecastDataset
) -> None:
    forecast_start = dataset.weekly_targets[-1].week_end + timedelta(days=1)
    as_of = forecast_start - timedelta(days=1)
    month_start = as_of.replace(day=1)
    evidence_time = datetime.combine(as_of, time.max, tzinfo=UTC)
    transaction_date = max(month_start, as_of - timedelta(days=2))
    digest = sha256(b"fictional-scenario-demo").hexdigest()
    with session_scope(factory) as session:
        session.add(
            CategoryRecord(
                id="food",
                name="Food",
                taxonomy_version="1.0",
                is_active=True,
            )
        )
        batch = ImportBatchRecord(
            id="scenario-demo-batch",
            account_id="synthetic-account",
            source_type="csv",
            source_filename="fictional-scenario-demo.csv",
            file_hash=digest,
            mime_type="text/csv",
            byte_size=100,
            verification_status="verified",
            imported_at=evidence_time,
        )
        session.add(batch)
        context = ImportContextRecord(
            id="scenario-demo-context",
            import_batch_id=batch.id,
            flags_json=[],
            note=None,
            created_at=evidence_time,
        )
        session.add(context)
        session.flush()
        session.add(
            StatementCoverageRecord(
                id="scenario-demo-coverage",
                import_context_id=context.id,
                statement_start_date=month_start,
                statement_end_date=as_of,
                coverage_status="complete",
                missing_periods_json=[],
            )
        )
        raw = RawTransactionRecord(
            id="scenario-demo-raw",
            import_batch_id=batch.id,
            source_type="csv",
            source_row_number=2,
            page_number=None,
            page_record_number=None,
            raw_payload={"synthetic": True},
            original_date_text=transaction_date.isoformat(),
            original_description="Fictional food purchase",
            original_amount_text="-40.00",
            parser_name="synthetic_parser",
            parser_version="1.0",
            source_fingerprint=digest,
            canonical_fingerprint=sha256(b"fictional-demo-canonical").hexdigest(),
            issues_json=[],
            review_status="confirmed",
            created_at=evidence_time,
        )
        session.add(raw)
        session.add(
            VerifiedTransactionRecord(
                id="scenario-demo-transaction",
                raw_transaction_id=raw.id,
                account_id="synthetic-account",
                transaction_date=transaction_date,
                posting_date=None,
                description="Fictional food purchase",
                merchant="Fictional Grocer",
                amount=Decimal("-40.00"),
                balance_after=None,
                currency="GBP",
                external_id="fictional-scenario-demo",
                transaction_type="synthetic",
                direction="outflow",
                category_id="food",
                financial_role_id="expense",
                verified_at=evidence_time,
            )
        )
        session.add_all(
            (
                BudgetRecord(
                    id="scenario-demo-food-budget",
                    user_profile_id="synthetic-profile",
                    budget_type="monthly_category",
                    category_id="food",
                    period_start=month_start,
                    period_end=_month_end(as_of),
                    amount_limit=Decimal("300.00"),
                    currency="GBP",
                ),
                BudgetRecord(
                    id="scenario-demo-week-budget",
                    user_profile_id="synthetic-profile",
                    budget_type="weekly_discretionary",
                    category_id=None,
                    period_start=forecast_start,
                    period_end=forecast_start + timedelta(days=6),
                    amount_limit=Decimal("500.00"),
                    currency="GBP",
                ),
                SavingsGoalRecord(
                    id="scenario-demo-floor",
                    account_id="synthetic-account",
                    goal_type="minimum_balance",
                    name="Fictional balance floor",
                    target_amount=Decimal("600.00"),
                    current_amount=Decimal("0.00"),
                    target_date=None,
                    created_at=evidence_time,
                ),
                SavingsGoalRecord(
                    id="scenario-demo-savings",
                    account_id="synthetic-account",
                    goal_type="savings_target",
                    name="Fictional savings target",
                    target_amount=Decimal("900.00"),
                    current_amount=Decimal("300.00"),
                    target_date=as_of + timedelta(days=180),
                    created_at=evidence_time,
                ),
            )
        )


def _scenario(
    scenario_type: FinancialScenarioType,
    *,
    forecast_start: date,
    amount: Decimal,
) -> FinancialScenario:
    recurring = scenario_type in {
        FinancialScenarioType.NEW_SUBSCRIPTION,
        FinancialScenarioType.RENT_INCREASE,
        FinancialScenarioType.INCOME_INCREASE,
        FinancialScenarioType.INCOME_REDUCTION,
        FinancialScenarioType.CATEGORY_SPENDING_REDUCTION,
        FinancialScenarioType.NEW_SAVINGS_TRANSFER,
    }
    category = (
        "food"
        if scenario_type
        in {
            FinancialScenarioType.ONE_OFF_PURCHASE,
            FinancialScenarioType.TRAVEL_EXPENSE,
            FinancialScenarioType.NEW_SUBSCRIPTION,
            FinancialScenarioType.CANCELLED_SUBSCRIPTION,
            FinancialScenarioType.CATEGORY_SPENDING_REDUCTION,
        }
        else None
    )
    cancellation = scenario_type is FinancialScenarioType.CANCELLED_SUBSCRIPTION
    return FinancialScenario(
        scenario_id=f"fictional-{scenario_type.value}",
        user_profile_id="synthetic-profile",
        account_id="synthetic-account",
        scenario_type=scenario_type,
        name="Fictional what-if",
        start_date=forecast_start + timedelta(days=3),
        amount=None if cancellation else amount,
        frequency=RecurrenceFrequency.MONTHLY if recurring else None,
        category_id=category,
        recurring_payment_id=("synthetic-candidate-expense" if cancellation else None),
    )


def main() -> None:
    """Print one baseline-versus-scenario comparison using fictional data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario-type",
        choices=tuple(item.value for item in FinancialScenarioType),
        default=FinancialScenarioType.ONE_OFF_PURCHASE.value,
    )
    parser.add_argument("--amount", type=Decimal, default=Decimal("250.00"))
    args = parser.parse_args()
    if args.amount <= 0:
        parser.error("--amount must be a positive magnitude")

    dataset = _synthetic_dataset(weeks=36)
    factory = _seed_database(dataset, stale_balance_days=0)
    _seed_planning_evidence(factory, dataset)
    trained = train_primary_forecaster(
        dataset,
        policy=ForecastModelPolicy(
            initial_training_weeks=8,
            final_test_weeks=4,
            minimum_training_weeks=8,
            minimum_relative_mae_improvement=0.05,
            maximum_relative_rmse_regression=0,
            maximum_absolute_bias_increase=Decimal("1.00"),
            maximum_iterations=30,
            learning_rate=0.1,
            maximum_leaf_nodes=10,
            minimum_samples_leaf=2,
            random_seed=7,
        ),
    )
    forecast_start = dataset.weekly_targets[-1].week_end + timedelta(days=1)
    forecast_plan = ForecastPathPlan(
        user_profile_id="synthetic-profile",
        account_id="synthetic-account",
        forecast_start=forecast_start,
        horizon_days=90,
        knowledge_cutoff_at=dataset.plan.knowledge_cutoff_at,
        policy=ForecastPathPolicy(
            interval_probability=Decimal("0.80"),
            simulation_count=100,
            minimum_residual_samples=3,
            minimum_weekly_uncertainty=Decimal("20.00"),
            low_confidence_multiplier=Decimal("1.50"),
            stale_data_multiplier=Decimal("2.00"),
            random_seed=17,
            freshness=FreshnessPolicy(
                max_transaction_age_days=1,
                max_balance_age_days=1,
                max_coverage_age_days=1,
                minimum_contiguous_coverage_days=30,
            ),
        ),
    )
    result = evaluate_financial_scenario(
        factory,
        dataset=dataset,
        trained=trained,
        forecast_plan=forecast_plan,
        planning_plan=PlanningEvaluationPlan(
            user_profile_id="synthetic-profile",
            account_ids=("synthetic-account",),
            as_of_date=forecast_start - timedelta(days=1),
        ),
        scenario=_scenario(
            FinancialScenarioType(args.scenario_type),
            forecast_start=forecast_start,
            amount=args.amount,
        ),
    )
    print("CashFlow AI synthetic scenario comparison")
    print(f"scenario type: {result.scenario.scenario_type.value}")
    print(f"generated changes: {len(result.overlay.adjustments)}")
    print(f"baseline end balance: GBP {result.balance_effect.baseline_end_balance}")
    print(f"scenario end balance: GBP {result.balance_effect.scenario_end_balance}")
    print(f"end balance difference: GBP {result.balance_effect.end_balance_difference}")
    print(
        "lowest cautious balance difference: GBP "
        f"{result.balance_effect.lowest_balance_difference}"
    )
    food_effect = next(
        item
        for item in result.budget_effects
        if item.budget_id == "scenario-demo-food-budget"
    )
    print(
        "food budget projected-use difference: GBP "
        f"{food_effect.projected_use_difference}"
    )
    print(f"safe weekly difference: GBP {result.safe_spending_effect.difference}")
    print(f"uncertainty inherited: {str(result.uncertainty.inherited).lower()}")
    print("warnings: " + (", ".join(item.value for item in result.warnings) or "none"))
    print(f"hypothetical: {str(result.hypothetical).lower()}")


if __name__ == "__main__":  # pragma: no cover - console entry point
    main()
