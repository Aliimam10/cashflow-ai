"""Tests for isolated baseline-versus-scenario financial comparisons."""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.forecasting.model import (
    TrainedPrimaryForecaster,
    train_primary_forecaster,
)
from cashflow_ai.forecasting.path_demo import _seed_database, _synthetic_dataset
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    BudgetRecord,
    CategoryRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    RecurringPaymentCandidateRecord,
    SavingsGoalRecord,
    ScenarioRecord,
    StatementCoverageRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.planning import (
    ScenarioPlanningError,
    ScenarioPlanningErrorCode,
    evaluate_financial_scenario,
)
from cashflow_ai.planning.scenario_demo import main as scenario_demo_main
from cashflow_ai.planning.scenarios import (
    _advance,
    _validate_uncertainty_inheritance,
)
from cashflow_ai.schemas import (
    FinancialScenario,
    FinancialScenarioComparison,
    FinancialScenarioType,
    ForecastDataset,
    ForecastModelPolicy,
    ForecastPathPlan,
    ForecastPathPolicy,
    FreshnessPolicy,
    PlanningEvaluationPlan,
    RecurrenceFrequency,
    ScenarioBalanceEffect,
    ScenarioBudgetEffect,
    ScenarioComparisonWarningCode,
    ScenarioGoalEffect,
    ScenarioSafeSpendingEffect,
)


@pytest.fixture(scope="module")
def model_context() -> tuple[ForecastDataset, TrainedPrimaryForecaster]:
    dataset = _synthetic_dataset(weeks=36)
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
    return dataset, trained


def _forecast_plan(
    dataset: ForecastDataset, *, horizon_days: int = 90
) -> ForecastPathPlan:
    forecast_start = dataset.weekly_targets[-1].week_end + timedelta(days=1)
    return ForecastPathPlan(
        user_profile_id="synthetic-profile",
        account_id="synthetic-account",
        forecast_start=forecast_start,
        horizon_days=horizon_days,
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


def _seed_planning(
    factory: sessionmaker[Session],
    dataset: ForecastDataset,
    *,
    incomplete_coverage: bool = False,
) -> None:
    forecast_start = dataset.weekly_targets[-1].week_end + timedelta(days=1)
    as_of = forecast_start - timedelta(days=1)
    month_start = as_of.replace(day=1)
    if as_of.month == 12:
        month_end = date(as_of.year, 12, 31)
    else:
        month_end = date(as_of.year, as_of.month + 1, 1) - timedelta(days=1)
    evidence_time = datetime.combine(as_of, time.max, tzinfo=UTC)
    transaction_date = max(month_start, as_of - timedelta(days=2))
    digest = sha256(b"fictional-scenario-fixture").hexdigest()
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
            id="scenario-batch",
            account_id="synthetic-account",
            source_type="csv",
            source_filename="fictional-scenario.csv",
            file_hash=digest,
            mime_type="text/csv",
            byte_size=100,
            verification_status="verified",
            imported_at=evidence_time,
        )
        session.add(batch)
        context = ImportContextRecord(
            id="scenario-context",
            import_batch_id=batch.id,
            flags_json=[],
            note=None,
            created_at=evidence_time,
        )
        session.add(context)
        session.flush()
        session.add(
            StatementCoverageRecord(
                id="scenario-coverage",
                import_context_id=context.id,
                statement_start_date=month_start,
                statement_end_date=(
                    as_of - timedelta(days=2) if incomplete_coverage else as_of
                ),
                coverage_status="complete",
                missing_periods_json=[],
            )
        )
        raw = RawTransactionRecord(
            id="scenario-raw",
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
            canonical_fingerprint=sha256(b"fictional-canonical").hexdigest(),
            issues_json=[],
            review_status="confirmed",
            created_at=evidence_time,
        )
        session.add(raw)
        session.add(
            VerifiedTransactionRecord(
                id="scenario-transaction",
                raw_transaction_id=raw.id,
                account_id="synthetic-account",
                transaction_date=transaction_date,
                posting_date=None,
                description="Fictional food purchase",
                merchant="Fictional Grocer",
                amount=Decimal("-40.00"),
                balance_after=None,
                currency="GBP",
                external_id="fictional-scenario-transaction",
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
                    id="food-budget",
                    user_profile_id="synthetic-profile",
                    budget_type="monthly_category",
                    category_id="food",
                    period_start=month_start,
                    period_end=month_end,
                    amount_limit=Decimal("300.00"),
                    currency="GBP",
                ),
                BudgetRecord(
                    id="forecast-week-budget",
                    user_profile_id="synthetic-profile",
                    budget_type="weekly_discretionary",
                    category_id=None,
                    period_start=forecast_start,
                    period_end=forecast_start + timedelta(days=6),
                    amount_limit=Decimal("500.00"),
                    currency="GBP",
                ),
                BudgetRecord(
                    id="current-week-budget",
                    user_profile_id="synthetic-profile",
                    budget_type="weekly_discretionary",
                    category_id=None,
                    period_start=as_of - timedelta(days=6),
                    period_end=as_of,
                    amount_limit=Decimal("100.00"),
                    currency="GBP",
                ),
                SavingsGoalRecord(
                    id="minimum-goal",
                    account_id="synthetic-account",
                    goal_type="minimum_balance",
                    name="Fictional floor",
                    target_amount=Decimal("600.00"),
                    current_amount=Decimal("0.00"),
                    target_date=None,
                    created_at=evidence_time,
                ),
                SavingsGoalRecord(
                    id="savings-goal",
                    account_id="synthetic-account",
                    goal_type="savings_target",
                    name="Fictional savings",
                    target_amount=Decimal("900.00"),
                    current_amount=Decimal("300.00"),
                    target_date=as_of + timedelta(days=180),
                    created_at=evidence_time,
                ),
            )
        )


def _factory(
    dataset: ForecastDataset,
    *,
    incomplete_coverage: bool = False,
    stale_balance_days: int = 0,
) -> sessionmaker[Session]:
    factory = _seed_database(dataset, stale_balance_days=stale_balance_days)
    _seed_planning(factory, dataset, incomplete_coverage=incomplete_coverage)
    return factory


def _planning_plan(plan: ForecastPathPlan) -> PlanningEvaluationPlan:
    return PlanningEvaluationPlan(
        user_profile_id=plan.user_profile_id,
        account_ids=(plan.account_id,),
        as_of_date=plan.forecast_start - timedelta(days=1),
    )


def _scenario(
    plan: ForecastPathPlan,
    scenario_type: FinancialScenarioType = FinancialScenarioType.ONE_OFF_PURCHASE,
    **changes: object,
) -> FinancialScenario:
    values: dict[str, object] = {
        "scenario_id": f"synthetic-{scenario_type.value}",
        "user_profile_id": plan.user_profile_id,
        "account_id": plan.account_id,
        "scenario_type": scenario_type,
        "name": "Fictional what-if",
        "start_date": plan.forecast_start + timedelta(days=3),
        "amount": Decimal("25.00"),
    }
    if scenario_type in {
        FinancialScenarioType.NEW_SUBSCRIPTION,
        FinancialScenarioType.RENT_INCREASE,
        FinancialScenarioType.INCOME_INCREASE,
        FinancialScenarioType.INCOME_REDUCTION,
        FinancialScenarioType.CATEGORY_SPENDING_REDUCTION,
        FinancialScenarioType.NEW_SAVINGS_TRANSFER,
    }:
        values["frequency"] = RecurrenceFrequency.MONTHLY
    if scenario_type is FinancialScenarioType.CATEGORY_SPENDING_REDUCTION:
        values["category_id"] = "food"
    if scenario_type is FinancialScenarioType.CANCELLED_SUBSCRIPTION:
        values["amount"] = None
        values["recurring_payment_id"] = "synthetic-candidate-expense"
    values.update(changes)
    return FinancialScenario(**values)  # type: ignore[arg-type]


def test_one_off_purchase_compares_paths_plans_and_preserves_database(
    model_context: tuple[ForecastDataset, TrainedPrimaryForecaster],
) -> None:
    dataset, trained = model_context
    factory = _factory(dataset)
    plan = _forecast_plan(dataset)
    scenario = _scenario(
        plan,
        category_id="food",
        amount=Decimal("250.00"),
    )
    with session_scope(factory) as session:
        before = (
            session.scalar(select(func.count()).select_from(VerifiedTransactionRecord)),
            session.scalar(
                select(func.count()).select_from(RecurringPaymentCandidateRecord)
            ),
            session.scalar(select(func.count()).select_from(ScenarioRecord)),
        )

    result = evaluate_financial_scenario(
        factory,
        dataset=dataset,
        trained=trained,
        forecast_plan=plan,
        planning_plan=_planning_plan(plan),
        scenario=scenario,
    )

    assert result.hypothetical is True
    assert result.balance_effect.end_balance_difference == Decimal("-250.00")
    assert result.balance_effect.scenario_lowest_lower_balance < (
        result.balance_effect.baseline_lowest_lower_balance
    )
    assert result.uncertainty.inherited is True
    assert result.baseline_forecast.interval_performance == (
        result.scenario_forecast.interval_performance
    )
    assert result.overlay.adjustments[0].amount == Decimal("-250.00")
    food_effect = next(
        item for item in result.budget_effects if item.budget_id == "food-budget"
    )
    assert food_effect.projected_use_difference == Decimal("250.00")
    assert result.safe_spending_effect.difference <= 0
    with session_scope(factory) as session:
        after = (
            session.scalar(select(func.count()).select_from(VerifiedTransactionRecord)),
            session.scalar(
                select(func.count()).select_from(RecurringPaymentCandidateRecord)
            ),
            session.scalar(select(func.count()).select_from(ScenarioRecord)),
        )
    assert after == before


@pytest.mark.parametrize(
    ("scenario_type", "positive_effect"),
    [
        (FinancialScenarioType.TRAVEL_EXPENSE, False),
        (FinancialScenarioType.NEW_SUBSCRIPTION, False),
        (FinancialScenarioType.RENT_INCREASE, False),
        (FinancialScenarioType.INCOME_INCREASE, True),
        (FinancialScenarioType.INCOME_REDUCTION, False),
        (FinancialScenarioType.CATEGORY_SPENDING_REDUCTION, True),
        (FinancialScenarioType.NEW_SAVINGS_TRANSFER, False),
    ],
)
def test_supported_scenarios_generate_expected_isolated_cash_direction(
    model_context: tuple[ForecastDataset, TrainedPrimaryForecaster],
    scenario_type: FinancialScenarioType,
    positive_effect: bool,
) -> None:
    dataset, trained = model_context
    factory = _factory(dataset)
    plan = _forecast_plan(dataset)
    scenario = _scenario(plan, scenario_type)

    result = evaluate_financial_scenario(
        factory,
        dataset=dataset,
        trained=trained,
        forecast_plan=plan,
        planning_plan=_planning_plan(plan),
        scenario=scenario,
    )

    difference = result.balance_effect.end_balance_difference
    assert (difference > 0) is positive_effect
    expected_occurrences = (
        1 if scenario_type is FinancialScenarioType.TRAVEL_EXPENSE else 3
    )
    assert len(result.overlay.adjustments) == expected_occurrences


def test_cancelled_subscription_neutralises_only_confirmed_baseline_occurrences(
    model_context: tuple[ForecastDataset, TrainedPrimaryForecaster],
) -> None:
    dataset, trained = model_context
    factory = _factory(dataset)
    plan = _forecast_plan(dataset)
    result = evaluate_financial_scenario(
        factory,
        dataset=dataset,
        trained=trained,
        forecast_plan=plan,
        planning_plan=_planning_plan(plan),
        scenario=_scenario(plan, FinancialScenarioType.CANCELLED_SUBSCRIPTION),
    )

    assert result.overlay.adjustments
    assert all(item.amount == Decimal("100.00") for item in result.overlay.adjustments)
    assert result.balance_effect.end_balance_difference == sum(
        (item.amount for item in result.overlay.adjustments), start=Decimal("0.00")
    )


def test_baseline_limitations_and_missing_budget_coverage_remain_visible(
    model_context: tuple[ForecastDataset, TrainedPrimaryForecaster],
) -> None:
    dataset, trained = model_context
    factory = _factory(dataset, incomplete_coverage=True, stale_balance_days=10)
    plan = _forecast_plan(dataset)
    result = evaluate_financial_scenario(
        factory,
        dataset=dataset,
        trained=trained,
        forecast_plan=plan,
        planning_plan=_planning_plan(plan),
        scenario=_scenario(plan),
    )

    assert result.warnings == (
        ScenarioComparisonWarningCode.BASELINE_FORECAST_LIMITATION,
        ScenarioComparisonWarningCode.INCOMPLETE_BASELINE_COVERAGE,
    )
    assert all(item.scenario_projected_use is None for item in result.budget_effects)


def test_scenario_service_rejects_scope_dates_categories_and_unknown_cancellation(
    model_context: tuple[ForecastDataset, TrainedPrimaryForecaster],
) -> None:
    dataset, trained = model_context
    factory = _factory(dataset)
    plan = _forecast_plan(dataset)
    calls = (
        (
            _scenario(plan, user_profile_id="different-profile"),
            _planning_plan(plan),
            ScenarioPlanningErrorCode.PLAN_SCOPE_MISMATCH,
        ),
        (
            _scenario(plan, account_id="different-account"),
            _planning_plan(plan),
            ScenarioPlanningErrorCode.PLAN_SCOPE_MISMATCH,
        ),
        (
            _scenario(plan),
            _planning_plan(plan).model_copy(
                update={"user_profile_id": "different-profile"}
            ),
            ScenarioPlanningErrorCode.PLAN_SCOPE_MISMATCH,
        ),
        (
            _scenario(plan),
            _planning_plan(plan).model_copy(
                update={"account_ids": ("different-account",)}
            ),
            ScenarioPlanningErrorCode.PLAN_SCOPE_MISMATCH,
        ),
        (
            _scenario(plan),
            _planning_plan(plan).model_copy(
                update={"as_of_date": plan.forecast_start - timedelta(days=2)}
            ),
            ScenarioPlanningErrorCode.PLAN_SCOPE_MISMATCH,
        ),
        (
            _scenario(plan, start_date=plan.forecast_start - timedelta(days=1)),
            _planning_plan(plan),
            ScenarioPlanningErrorCode.SCENARIO_OUTSIDE_HORIZON,
        ),
        (
            _scenario(
                plan,
                start_date=plan.forecast_start + timedelta(days=plan.horizon_days),
            ),
            _planning_plan(plan),
            ScenarioPlanningErrorCode.SCENARIO_OUTSIDE_HORIZON,
        ),
        (
            _scenario(
                plan,
                FinancialScenarioType.NEW_SUBSCRIPTION,
                end_date=plan.forecast_start + timedelta(days=plan.horizon_days),
            ),
            _planning_plan(plan),
            ScenarioPlanningErrorCode.SCENARIO_OUTSIDE_HORIZON,
        ),
        (
            _scenario(plan, category_id="missing-category"),
            _planning_plan(plan),
            ScenarioPlanningErrorCode.CATEGORY_NOT_FOUND,
        ),
        (
            _scenario(
                plan,
                FinancialScenarioType.CANCELLED_SUBSCRIPTION,
                recurring_payment_id="missing-candidate",
            ),
            _planning_plan(plan),
            ScenarioPlanningErrorCode.RECURRING_PAYMENT_NOT_FOUND,
        ),
        (
            _scenario(
                plan,
                FinancialScenarioType.CANCELLED_SUBSCRIPTION,
                recurring_payment_id="synthetic-candidate-income",
            ),
            _planning_plan(plan),
            ScenarioPlanningErrorCode.RECURRING_PAYMENT_NOT_FOUND,
        ),
    )
    for scenario, planning_plan, expected in calls:
        with pytest.raises(ScenarioPlanningError) as error:
            evaluate_financial_scenario(
                factory,
                dataset=dataset,
                trained=trained,
                forecast_plan=plan,
                planning_plan=planning_plan,
                scenario=scenario,
            )
        assert error.value.code is expected

    with session_scope(factory) as session:
        category = session.get(CategoryRecord, "food")
        assert category is not None
        category.is_active = False
    with pytest.raises(ScenarioPlanningError) as inactive:
        evaluate_financial_scenario(
            factory,
            dataset=dataset,
            trained=trained,
            forecast_plan=plan,
            planning_plan=_planning_plan(plan),
            scenario=_scenario(plan, category_id="food"),
        )
    assert inactive.value.code is ScenarioPlanningErrorCode.CATEGORY_INACTIVE


def test_recurrence_advancement_supports_fixed_and_calendar_frequencies() -> None:
    assert _advance(date(2024, 1, 1), RecurrenceFrequency.WEEKLY) == date(2024, 1, 8)
    assert _advance(date(2024, 1, 1), RecurrenceFrequency.FORTNIGHTLY) == date(
        2024, 1, 15
    )
    assert _advance(date(2024, 1, 31), RecurrenceFrequency.MONTHLY) == date(2024, 2, 29)
    assert _advance(date(2024, 2, 20), RecurrenceFrequency.QUARTERLY) == date(
        2024, 5, 20
    )
    assert _advance(date(2024, 2, 29), RecurrenceFrequency.ANNUAL) == date(2025, 2, 28)


def test_uncertainty_inheritance_fails_closed_on_changed_interval_width(
    model_context: tuple[ForecastDataset, TrainedPrimaryForecaster],
) -> None:
    dataset, trained = model_context
    factory = _factory(dataset)
    plan = _forecast_plan(dataset)
    result = evaluate_financial_scenario(
        factory,
        dataset=dataset,
        trained=trained,
        forecast_plan=plan,
        planning_plan=_planning_plan(plan),
        scenario=_scenario(plan),
    )
    changed_day = result.scenario_forecast.daily_balances[0].model_copy(
        update={
            "upper_balance": result.scenario_forecast.daily_balances[0].upper_balance
            + Decimal("1.00")
        }
    )
    changed_path = result.scenario_forecast.model_copy(
        update={
            "daily_balances": (
                changed_day,
                *result.scenario_forecast.daily_balances[1:],
            )
        }
    )

    with pytest.raises(ScenarioPlanningError) as error:
        _validate_uncertainty_inheritance(result.baseline_forecast, changed_path)
    assert error.value.code is ScenarioPlanningErrorCode.UNCERTAINTY_INHERITANCE_FAILED


def test_scenario_contract_rejects_incompatible_fields() -> None:
    base = {
        "scenario_id": "invalid",
        "user_profile_id": "synthetic-profile",
        "account_id": "synthetic-account",
        "name": "Invalid fictional scenario",
        "start_date": date(2026, 1, 1),
    }
    invalid = (
        {
            **base,
            "scenario_type": FinancialScenarioType.ONE_OFF_PURCHASE,
            "amount": Decimal("20.00"),
            "frequency": RecurrenceFrequency.MONTHLY,
        },
        {
            **base,
            "scenario_type": FinancialScenarioType.NEW_SUBSCRIPTION,
            "amount": Decimal("20.00"),
        },
        {
            **base,
            "scenario_type": FinancialScenarioType.CANCELLED_SUBSCRIPTION,
        },
        {
            **base,
            "scenario_type": FinancialScenarioType.CATEGORY_SPENDING_REDUCTION,
            "amount": Decimal("20.00"),
            "frequency": RecurrenceFrequency.MONTHLY,
        },
        {
            **base,
            "scenario_type": FinancialScenarioType.INCOME_INCREASE,
            "amount": Decimal("20.00"),
            "frequency": RecurrenceFrequency.MONTHLY,
            "category_id": "food",
        },
        {
            **base,
            "scenario_type": FinancialScenarioType.TRAVEL_EXPENSE,
            "amount": Decimal("20.00"),
            "end_date": date(2025, 12, 31),
        },
    )
    for values in invalid:
        with pytest.raises(ValidationError):
            FinancialScenario(**values)  # type: ignore[arg-type]


def test_scenario_response_contracts_reject_inconsistent_derived_values(
    model_context: tuple[ForecastDataset, TrainedPrimaryForecaster],
) -> None:
    dataset, trained = model_context
    factory = _factory(dataset)
    plan = _forecast_plan(dataset)
    result = evaluate_financial_scenario(
        factory,
        dataset=dataset,
        trained=trained,
        forecast_plan=plan,
        planning_plan=_planning_plan(plan),
        scenario=_scenario(plan, category_id="food"),
    )

    balance_values = result.balance_effect.model_dump()
    for field in ("end_balance_difference", "lowest_balance_difference"):
        with pytest.raises(ValidationError):
            ScenarioBalanceEffect(**{**balance_values, field: Decimal("0.01")})

    complete_budget = next(
        item
        for item in result.budget_effects
        if item.baseline_projected_use is not None
    )
    budget_values = complete_budget.model_dump()
    invalid_budgets = (
        {
            **budget_values,
            "baseline_projected_use": None,
        },
        {
            **budget_values,
            "projected_use_difference": Decimal("0.01"),
        },
        {
            **budget_values,
            "scenario_projected_overrun": Decimal("999.00"),
        },
    )
    for values in invalid_budgets:
        with pytest.raises(ValidationError):
            ScenarioBudgetEffect(**values)

    minimum_goal = next(
        item
        for item in result.goal_effects
        if item.goal_type.value == "minimum_balance"
    )
    with pytest.raises(ValidationError):
        ScenarioGoalEffect(
            **{
                **minimum_goal.model_dump(),
                "required_monthly_contribution": Decimal("1.00"),
            }
        )
    savings_goal = next(
        item for item in result.goal_effects if item.goal_type.value == "savings_target"
    )
    with pytest.raises(ValidationError):
        ScenarioGoalEffect(
            **{
                **savings_goal.model_dump(),
                "required_monthly_contribution": None,
            }
        )
    with pytest.raises(ValidationError):
        ScenarioSafeSpendingEffect(
            **{
                **result.safe_spending_effect.model_dump(),
                "difference": Decimal("0.01"),
            }
        )

    comparison_values = {
        name: getattr(result, name) for name in type(result).model_fields
    }
    corrupt_comparisons = (
        {**comparison_values, "hypothetical": False},
        {
            **comparison_values,
            "baseline_forecast": result.baseline_forecast.model_copy(
                update={"scenario": result.overlay}
            ),
        },
        {
            **comparison_values,
            "overlay": result.overlay.model_copy(
                update={"scenario_id": "different-scenario"}
            ),
        },
        {
            **comparison_values,
            "scenario_forecast": result.scenario_forecast.model_copy(
                update={
                    "scenario": result.overlay.model_copy(update={"adjustments": ()})
                }
            ),
        },
        {
            **comparison_values,
            "warnings": (ScenarioComparisonWarningCode.BASELINE_FORECAST_LIMITATION,),
        },
        {
            **comparison_values,
            "warnings": (ScenarioComparisonWarningCode.INCOMPLETE_BASELINE_COVERAGE,),
        },
    )
    for values in corrupt_comparisons:
        with pytest.raises(ValidationError):
            FinancialScenarioComparison(**values)

    partial_effect = result.budget_effects[0].model_copy(
        update={"coverage_status": "partial"}
    )
    with pytest.raises(ValidationError):
        FinancialScenarioComparison(
            **{
                **comparison_values,
                "budget_effects": (partial_effect, *result.budget_effects[1:]),
                "warnings": (
                    ScenarioComparisonWarningCode.INCOMPLETE_BASELINE_COVERAGE,
                    ScenarioComparisonWarningCode.INCOMPLETE_BASELINE_COVERAGE,
                ),
            }
        )


@pytest.mark.parametrize(
    ("arguments", "expected_type", "expected_direction"),
    [
        ([], "one_off_purchase", "end balance difference: GBP -250.00"),
        (
            ["--scenario-type", "income_increase", "--amount", "100"],
            "income_increase",
            "end balance difference: GBP 300.00",
        ),
        (
            ["--scenario-type", "cancelled_subscription"],
            "cancelled_subscription",
            "end balance difference: GBP 300.00",
        ),
    ],
)
def test_manual_scenario_demo_prints_readable_hypothetical_comparison(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    expected_type: str,
    expected_direction: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["cashflow-scenario-demo", *arguments])

    scenario_demo_main()

    output = capsys.readouterr().out
    assert f"scenario type: {expected_type}" in output
    assert expected_direction in output
    assert "uncertainty inherited: true" in output
    assert "hypothetical: true" in output


def test_manual_scenario_demo_rejects_non_positive_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["cashflow-scenario-demo", "--amount", "0"])

    with pytest.raises(SystemExit):
        scenario_demo_main()
