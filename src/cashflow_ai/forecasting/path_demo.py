"""Readable synthetic demonstration of uncertainty-aware balance forecasting."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.forecasting.model import train_primary_forecaster
from cashflow_ai.forecasting.paths import build_balance_forecast_path
from cashflow_ai.forecasting.service import build_forecast_feature_rows
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    BalanceSnapshotRecord,
    FinancialRoleRecord,
    RecurringPaymentCandidateRecord,
    RecurringSeriesRecord,
    UserProfileRecord,
)
from cashflow_ai.schemas.forecast_models import ForecastModelPolicy
from cashflow_ai.schemas.forecast_paths import (
    ForecastPathPlan,
    ForecastPathPolicy,
    ForecastScenario,
    ForecastScenarioAdjustment,
    ScenarioAdjustmentKind,
)
from cashflow_ai.schemas.forecasting import (
    DailyForecastObservation,
    ForecastDataset,
    ForecastDatasetPlan,
    ForecastDayStatus,
    RecurringOutflowProjection,
    WeeklyForecastTarget,
)
from cashflow_ai.schemas.freshness import FreshnessPolicy
from cashflow_ai.schemas.statements import DateRange


def _synthetic_dataset(*, weeks: int = 36) -> ForecastDataset:
    first = date(2024, 1, 1)
    targets: list[WeeklyForecastTarget] = []
    daily: list[DailyForecastObservation] = []
    for index in range(weeks):
        week_start = first + timedelta(weeks=index)
        amount = Decimal(30 if index % 2 else 180)
        known_at = datetime.combine(
            week_start + timedelta(days=6), time.max, tzinfo=UTC
        )
        targets.append(
            WeeklyForecastTarget(
                week_start=week_start,
                week_end=week_start + timedelta(days=6),
                discretionary_spending=amount,
                known_recurring_outflow=(
                    Decimal("10.00") if index % 4 == 0 else Decimal("0.00")
                ),
                known_at=known_at,
            )
        )
        allocations = [(amount / 7).quantize(Decimal("0.01")) for _ in range(6)]
        allocations.append(amount - sum(allocations, start=Decimal("0")))
        for offset, daily_amount in enumerate(allocations):
            observation_date = week_start + timedelta(days=offset)
            daily.append(
                DailyForecastObservation(
                    observation_date=observation_date,
                    status=ForecastDayStatus.COVERED,
                    discretionary_spending=daily_amount,
                    transaction_count=1,
                    known_at=datetime.combine(observation_date, time.max, tzinfo=UTC),
                )
            )
    period_end = targets[-1].week_end
    cutoff = datetime.combine(period_end, time.max, tzinfo=UTC)
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic-profile",
        account_ids=("synthetic-account",),
        period=DateRange(start_date=first, end_date=period_end),
        knowledge_cutoff_at=cutoff,
        payday_days=(1, 15),
    )
    target_tuple = tuple(targets)
    return ForecastDataset(
        plan=plan,
        daily_calendar=tuple(daily),
        weekly_targets=target_tuple,
        feature_rows=build_forecast_feature_rows(target_tuple, plan.payday_days),
        next_recurring_outflow=RecurringOutflowProjection(
            week_start=period_end + timedelta(days=1),
            amount=Decimal("0.00"),
            known_at=cutoff,
        ),
    )


def _seed_database(
    dataset: ForecastDataset, *, stale_balance_days: int
) -> sessionmaker[Session]:
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    forecast_start = dataset.weekly_targets[-1].week_start + timedelta(weeks=1)
    evidence_time = dataset.plan.knowledge_cutoff_at - timedelta(hours=1)
    with session_scope(factory) as session:
        session.add(
            UserProfileRecord(
                id="synthetic-profile",
                display_name="Synthetic User",
                base_currency="GBP",
                timezone="Europe/London",
            )
        )
        session.flush()
        session.add(
            AccountRecord(
                id="synthetic-account",
                user_profile_id="synthetic-profile",
                name="Synthetic Current Account",
                account_type="current",
                currency="GBP",
                is_active=True,
            )
        )
        session.add_all(
            (
                FinancialRoleRecord(id="income", name="Income"),
                FinancialRoleRecord(id="expense", name="Expense"),
            )
        )
        session.flush()
        session.add(
            BalanceSnapshotRecord(
                id="synthetic-balance",
                account_id="synthetic-account",
                import_batch_id=None,
                balance=Decimal("1000.00"),
                currency="GBP",
                as_of_date=forecast_start - timedelta(days=1 + stale_balance_days),
                recorded_at=evidence_time,
                source="manual",
                verification_status="verified",
            )
        )
        for suffix, role, amount, offset in (
            ("income", "income", Decimal("500.00"), 2),
            ("expense", "expense", Decimal("-100.00"), 5),
        ):
            series = RecurringSeriesRecord(
                id=f"synthetic-series-{suffix}",
                account_id="synthetic-account",
                merchant_pattern=f"synthetic-{suffix}",
                expected_amount=amount,
                interval_days=30,
                financial_role_id=role,
                is_active=True,
                created_at=evidence_time,
            )
            session.add(series)
            session.flush()
            session.add(
                RecurringPaymentCandidateRecord(
                    id=f"synthetic-candidate-{suffix}",
                    account_id="synthetic-account",
                    recurring_series_id=series.id,
                    merchant_group=f"synthetic-{suffix}",
                    currency="GBP",
                    direction="inflow" if amount > 0 else "outflow",
                    financial_role_id=role,
                    expected_amount=amount,
                    frequency="monthly",
                    interval_days=30,
                    next_expected_date=forecast_start + timedelta(days=offset),
                    confidence=Decimal("0.900000"),
                    covered_missed_count=0,
                    status="confirmed",
                    detected_at=evidence_time,
                    evidence_as_of_date=forecast_start - timedelta(days=1),
                    knowledge_cutoff_at=evidence_time,
                    reviewed_at=evidence_time,
                )
            )
    return factory


def main() -> None:
    """Print one fictional 30-day balance forecast and likely range."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizon-days", type=int, default=30)
    parser.add_argument("--history-weeks", type=int, default=36)
    parser.add_argument("--simulations", type=int, default=200)
    parser.add_argument("--stale-balance-days", type=int, default=0)
    parser.add_argument("--scenario-outflow", type=Decimal, default=Decimal("0"))
    args = parser.parse_args()
    if not 7 <= args.horizon_days <= 90:
        parser.error("--horizon-days must be from 7 through 90")
    if args.history_weeks < 8:
        parser.error("--history-weeks must be at least 8")
    if not 100 <= args.simulations <= 20_000:
        parser.error("--simulations must be from 100 through 20000")
    if args.stale_balance_days < 0:
        parser.error("--stale-balance-days cannot be negative")
    if args.scenario_outflow < 0:
        parser.error("--scenario-outflow must be a non-negative magnitude")

    dataset = _synthetic_dataset(weeks=args.history_weeks)
    factory = _seed_database(dataset, stale_balance_days=args.stale_balance_days)
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
    forecast_start = dataset.weekly_targets[-1].week_start + timedelta(weeks=1)
    plan = ForecastPathPlan(
        user_profile_id="synthetic-profile",
        account_id="synthetic-account",
        forecast_start=forecast_start,
        horizon_days=args.horizon_days,
        knowledge_cutoff_at=dataset.plan.knowledge_cutoff_at,
        policy=ForecastPathPolicy(
            interval_probability=Decimal("0.80"),
            simulation_count=args.simulations,
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
    scenario = ForecastScenario(
        scenario_id="synthetic-what-if" if args.scenario_outflow else None,
        adjustments=(
            (
                ForecastScenarioAdjustment(
                    adjustment_id="synthetic-one-off-outflow",
                    adjustment_date=forecast_start + timedelta(days=10),
                    kind=ScenarioAdjustmentKind.OUTFLOW,
                    amount=-args.scenario_outflow,
                ),
            )
            if args.scenario_outflow
            else ()
        ),
    )
    result = build_balance_forecast_path(
        factory,
        dataset=dataset,
        trained=trained,
        plan=plan,
        scenario=scenario,
    )
    print("CashFlow AI synthetic balance-path check")
    print(f"selected model: {result.selected_model.value}")
    print(f"forecast days: {len(result.daily_balances)}")
    print(f"simulated paths: {plan.policy.simulation_count}")
    print(f"confirmed recurring events: {len(result.recurring_occurrences)}")
    print(
        f"expected balance in {plan.horizon_days} days: "
        f"£{result.expected_final_balance}"
    )
    print(f"likely range: £{result.lower_final_balance}-£{result.upper_final_balance}")
    print("warnings: " + (", ".join(item.value for item in result.warnings) or "none"))
    if result.interval_performance is not None:
        print(
            "held-out interval coverage: "
            f"{result.interval_performance.empirical_coverage}"
        )
