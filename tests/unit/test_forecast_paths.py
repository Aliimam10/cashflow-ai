"""Tests for residual-bootstrap uncertainty and daily balance paths."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_forecast_model import _dataset, _policy

import cashflow_ai.forecasting.path_demo as path_demo
import cashflow_ai.forecasting.paths as path_module
from cashflow_ai.forecasting import (
    ForecastPathError,
    ForecastPathErrorCode,
    build_balance_forecast_path,
    build_next_forecast_inference_row,
    train_primary_forecaster,
)
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    BalanceSnapshotRecord,
    FinancialRoleRecord,
    ForecastRunRecord,
    RecurringPaymentCandidateRecord,
    RecurringSeriesRecord,
    ScenarioRecord,
    UserProfileRecord,
)
from cashflow_ai.schemas import (
    BalanceForecastPath,
    BalanceSnapshotSource,
    Currency,
    DailyBalancePathPoint,
    DailyForecastObservation,
    FinancialRole,
    ForecastBaselineName,
    ForecastDataset,
    ForecastDayStatus,
    ForecastIntervalMethod,
    ForecastIntervalPerformance,
    ForecastModelName,
    ForecastOpeningBalance,
    ForecastPathPlan,
    ForecastPathPolicy,
    ForecastPathWarningCode,
    ForecastScenario,
    ForecastScenarioAdjustment,
    FreshnessPolicy,
    FreshnessWarningCode,
    RecurringForecastOccurrence,
    ScenarioAdjustmentKind,
    WeeklySpendingPath,
)


@pytest.fixture
def engine() -> Engine:
    value = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(value)
    return value


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _daily_dataset(*, weeks: int = 36, predictable: bool = True) -> ForecastDataset:
    base = _dataset(weeks=weeks, predictable=predictable)
    observations: list[DailyForecastObservation] = []
    for target in base.weekly_targets:
        allocations = [target.discretionary_spending / 7 for _ in range(6)]
        allocations.append(
            target.discretionary_spending - sum(allocations, start=Decimal("0"))
        )
        for offset, amount in enumerate(allocations):
            observation_date = target.week_start + timedelta(days=offset)
            observations.append(
                DailyForecastObservation(
                    observation_date=observation_date,
                    status=ForecastDayStatus.COVERED,
                    discretionary_spending=amount.quantize(Decimal("0.01")),
                    transaction_count=1,
                    known_at=datetime.combine(observation_date, time.max, tzinfo=UTC),
                )
            )
    return base.model_copy(update={"daily_calendar": tuple(observations)})


def _path_policy(**changes: object) -> ForecastPathPolicy:
    values: dict[str, object] = {
        "interval_probability": Decimal("0.80"),
        "simulation_count": 100,
        "minimum_residual_samples": 3,
        "minimum_weekly_uncertainty": Decimal("20.00"),
        "low_confidence_multiplier": Decimal("1.50"),
        "stale_data_multiplier": Decimal("2.00"),
        "random_seed": 17,
        "freshness": FreshnessPolicy(
            max_transaction_age_days=1,
            max_balance_age_days=1,
            max_coverage_age_days=1,
            minimum_contiguous_coverage_days=30,
        ),
        **changes,
    }
    return ForecastPathPolicy.model_validate(values)


def _path_plan(dataset: ForecastDataset, **changes: object) -> ForecastPathPlan:
    values: dict[str, object] = {
        "user_profile_id": dataset.plan.user_profile_id,
        "account_id": dataset.plan.account_ids[0],
        "forecast_start": dataset.weekly_targets[-1].week_start + timedelta(weeks=1),
        "horizon_days": 30,
        "knowledge_cutoff_at": dataset.plan.knowledge_cutoff_at,
        "policy": _path_policy(),
        **changes,
    }
    return ForecastPathPlan.model_validate(values)


def _seed_evidence(
    factory: sessionmaker[Session],
    dataset: ForecastDataset,
    *,
    balance_age_days: int = 0,
    include_recurring: bool = True,
    active: bool = True,
) -> None:
    plan = _path_plan(dataset)
    evidence_time = plan.knowledge_cutoff_at - timedelta(hours=1)
    with session_scope(factory) as session:
        session.add(
            UserProfileRecord(
                id=str(plan.user_profile_id),
                display_name="Synthetic Forecast User",
                base_currency="GBP",
                timezone="Europe/London",
            )
        )
        session.flush()
        session.add(
            AccountRecord(
                id=str(plan.account_id),
                user_profile_id=str(plan.user_profile_id),
                name="Synthetic Forecast Account",
                account_type="current",
                currency="GBP",
                is_active=active,
            )
        )
        session.add_all(
            FinancialRoleRecord(
                id=role.value, name=role.value.replace("_", " ").title()
            )
            for role in (
                FinancialRole.INCOME,
                FinancialRole.EXPENSE,
                FinancialRole.TRANSFER_OUT,
            )
        )
        session.flush()
        session.add(
            BalanceSnapshotRecord(
                id="balance-valid",
                account_id=str(plan.account_id),
                import_batch_id=None,
                balance=Decimal("1000.00"),
                currency="GBP",
                as_of_date=plan.forecast_start - timedelta(days=1 + balance_age_days),
                recorded_at=evidence_time,
                source="manual",
                verification_status="verified",
            )
        )
        session.add_all(
            (
                BalanceSnapshotRecord(
                    id="balance-unverified",
                    account_id=str(plan.account_id),
                    import_batch_id=None,
                    balance=Decimal("9000.00"),
                    currency="GBP",
                    as_of_date=plan.forecast_start - timedelta(days=1),
                    recorded_at=evidence_time,
                    source="manual",
                    verification_status="unverified",
                ),
                BalanceSnapshotRecord(
                    id="balance-learned-late",
                    account_id=str(plan.account_id),
                    import_batch_id=None,
                    balance=Decimal("8000.00"),
                    currency="GBP",
                    as_of_date=plan.forecast_start - timedelta(days=1),
                    recorded_at=plan.knowledge_cutoff_at + timedelta(hours=1),
                    source="manual",
                    verification_status="verified",
                ),
            )
        )
        if include_recurring:
            for suffix, role, amount, day_offset in (
                ("income", FinancialRole.INCOME, Decimal("500.00"), 2),
                ("expense", FinancialRole.EXPENSE, Decimal("-100.00"), 5),
                ("transfer", FinancialRole.TRANSFER_OUT, Decimal("-75.00"), 4),
            ):
                series = RecurringSeriesRecord(
                    id=f"series-{suffix}",
                    account_id=str(plan.account_id),
                    merchant_pattern=f"synthetic-{suffix}",
                    expected_amount=amount,
                    interval_days=30,
                    financial_role_id=role.value,
                    is_active=True,
                    created_at=evidence_time,
                )
                session.add(series)
                session.flush()
                session.add(
                    RecurringPaymentCandidateRecord(
                        id=f"candidate-{suffix}",
                        account_id=str(plan.account_id),
                        recurring_series_id=series.id,
                        merchant_group=f"synthetic-{suffix}",
                        currency="GBP",
                        direction=("inflow" if amount > 0 else "outflow"),
                        financial_role_id=role.value,
                        expected_amount=amount,
                        frequency="monthly",
                        interval_days=30,
                        next_expected_date=plan.forecast_start
                        + timedelta(days=day_offset),
                        confidence=Decimal("0.900000"),
                        covered_missed_count=0,
                        status="confirmed",
                        detected_at=evidence_time,
                        evidence_as_of_date=plan.forecast_start - timedelta(days=1),
                        knowledge_cutoff_at=evidence_time,
                        reviewed_at=evidence_time,
                    )
                )


def test_advanced_model_builds_reproducible_daily_balance_intervals(
    factory: sessionmaker[Session],
) -> None:
    dataset = _daily_dataset()
    _seed_evidence(factory, dataset)
    trained = train_primary_forecaster(dataset, policy=_policy())
    plan = _path_plan(dataset)

    first = build_balance_forecast_path(
        factory, dataset=dataset, trained=trained, plan=plan
    )
    second = build_balance_forecast_path(
        factory, dataset=dataset, trained=trained, plan=plan
    )

    assert first == second
    assert first.selected_model is ForecastModelName.HIST_GRADIENT_BOOSTING
    assert first.interval_method is ForecastIntervalMethod.RESIDUAL_BOOTSTRAP
    assert first.opening_balance.balance == Decimal("1000.00")
    assert first.opening_balance.source is BalanceSnapshotSource.MANUAL
    assert first.warnings == ()
    assert first.freshness_warnings == ()
    assert first.widening_multiplier == Decimal("1")
    assert len(first.daily_balances) == 30
    assert len(first.weekly_spending) == 5
    assert tuple(item.signed_amount for item in first.recurring_occurrences) == (
        Decimal("500.00"),
        Decimal("-100.00"),
    )
    assert first.interval_performance is not None
    assert first.interval_performance.sample_count == 4
    assert first.interval_performance.mean_interval_width > 0
    assert first.lower_final_balance <= first.expected_final_balance
    assert first.expected_final_balance <= first.upper_final_balance
    assert any(
        item.recurring_net_flow == Decimal("500.00") for item in first.daily_balances
    )
    assert any(
        item.recurring_net_flow == Decimal("-100.00") for item in first.daily_balances
    )
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ForecastRunRecord)) == 0
        assert session.scalar(select(func.count()).select_from(ScenarioRecord)) == 0


def test_scenario_adjustments_change_only_the_hypothetical_path(
    factory: sessionmaker[Session],
) -> None:
    dataset = _daily_dataset()
    _seed_evidence(factory, dataset)
    trained = train_primary_forecaster(dataset, policy=_policy())
    plan = _path_plan(dataset)
    scenario = ForecastScenario(
        scenario_id="synthetic-scenario",
        discretionary_spending_multiplier=Decimal("0"),
        adjustments=(
            ForecastScenarioAdjustment(
                adjustment_id="synthetic-repair",
                adjustment_date=plan.forecast_start + timedelta(days=10),
                kind=ScenarioAdjustmentKind.OUTFLOW,
                amount=Decimal("-50.00"),
            ),
        ),
    )
    result = build_balance_forecast_path(
        factory,
        dataset=dataset,
        trained=trained,
        plan=plan,
        scenario=scenario,
    )

    assert result.expected_final_balance == Decimal("1350.00")
    assert all(
        item.expected_discretionary_outflow == 0 for item in result.daily_balances
    )
    assert sum(
        (item.scenario_adjustment for item in result.daily_balances),
        start=Decimal("0"),
    ) == Decimal("-50.00")


def test_stale_balance_and_baseline_model_widen_intervals(
    factory: sessionmaker[Session],
) -> None:
    dataset = _daily_dataset(predictable=False)
    _seed_evidence(factory, dataset, balance_age_days=10, include_recurring=False)
    trained = train_primary_forecaster(dataset, policy=_policy())
    result = build_balance_forecast_path(
        factory,
        dataset=dataset,
        trained=trained,
        plan=_path_plan(dataset),
    )

    assert result.selected_model is ForecastBaselineName.HISTORICAL_MEAN
    assert result.warnings == (
        ForecastPathWarningCode.LOW_CONFIDENCE_MODEL,
        ForecastPathWarningCode.STALE_DATA,
    )
    assert result.freshness_warnings == (FreshnessWarningCode.BALANCE_STALE,)
    assert result.widening_multiplier == Decimal("3.0000")
    assert result.upper_final_balance > result.lower_final_balance


def test_low_data_reports_limited_residual_history_without_fake_coverage(
    factory: sessionmaker[Session],
) -> None:
    dataset = _daily_dataset(weeks=12)
    _seed_evidence(factory, dataset, include_recurring=False)
    trained = train_primary_forecaster(dataset, policy=_policy())
    plan = _path_plan(
        dataset,
        policy=_path_policy(minimum_residual_samples=10),
    )
    result = build_balance_forecast_path(
        factory, dataset=dataset, trained=trained, plan=plan
    )

    assert result.selected_model is ForecastBaselineName.RECENT_ROLLING_MEAN
    assert result.interval_performance is None
    assert result.warnings == (
        ForecastPathWarningCode.LOW_CONFIDENCE_MODEL,
        ForecastPathWarningCode.LIMITED_RESIDUAL_HISTORY,
    )


def test_path_service_rejects_missing_or_misaligned_evidence(
    factory: sessionmaker[Session],
) -> None:
    dataset = _daily_dataset()
    trained = train_primary_forecaster(dataset, policy=_policy())
    plan = _path_plan(dataset)
    with pytest.raises(ForecastPathError) as missing_account:
        build_balance_forecast_path(
            factory, dataset=dataset, trained=trained, plan=plan
        )
    assert missing_account.value.code is ForecastPathErrorCode.ACCOUNT_NOT_FOUND

    _seed_evidence(factory, dataset)
    wrong_scope = dataset.model_copy(
        update={"plan": dataset.plan.model_copy(update={"account_ids": ("other",)})}
    )
    with pytest.raises(ForecastPathError) as scope:
        build_balance_forecast_path(
            factory, dataset=wrong_scope, trained=trained, plan=plan
        )
    assert scope.value.code is ForecastPathErrorCode.ACCOUNT_SCOPE_MISMATCH

    changed_cutoff = plan.model_copy(
        update={"knowledge_cutoff_at": plan.knowledge_cutoff_at - timedelta(hours=1)}
    )
    with pytest.raises(ForecastPathError) as cutoff:
        build_balance_forecast_path(
            factory, dataset=dataset, trained=trained, plan=changed_cutoff
        )
    assert cutoff.value.code is ForecastPathErrorCode.FORECAST_EVIDENCE_MISALIGNED

    outside = ForecastScenario(
        adjustments=(
            ForecastScenarioAdjustment(
                adjustment_id="outside",
                adjustment_date=plan.forecast_start - timedelta(days=1),
                kind=ScenarioAdjustmentKind.INFLOW,
                amount=Decimal("1.00"),
            ),
        )
    )
    with pytest.raises(ForecastPathError) as scenario:
        build_balance_forecast_path(
            factory,
            dataset=dataset,
            trained=trained,
            plan=plan,
            scenario=outside,
        )
    assert scenario.value.code is ForecastPathErrorCode.SCENARIO_OUTSIDE_HORIZON


def test_path_requires_verified_balance_known_by_cutoff(
    factory: sessionmaker[Session],
) -> None:
    dataset = _daily_dataset()
    plan = _path_plan(dataset)
    _seed_evidence(factory, dataset, include_recurring=False)
    with session_scope(factory) as session:
        valid = session.get(BalanceSnapshotRecord, "balance-valid")
        assert valid is not None
        valid.verification_status = "rejected"
    trained = train_primary_forecaster(dataset, policy=_policy())
    with pytest.raises(ForecastPathError) as exc_info:
        build_balance_forecast_path(
            factory, dataset=dataset, trained=trained, plan=plan
        )
    assert exc_info.value.code is ForecastPathErrorCode.BALANCE_NOT_FOUND


def test_freshness_warnings_cover_absent_gapped_and_old_evidence(
    factory: sessionmaker[Session],
) -> None:
    full = _daily_dataset()
    unknown_day = DailyForecastObservation(
        observation_date=full.plan.period.start_date,
        status=ForecastDayStatus.UNKNOWN,
        discretionary_spending=None,
        transaction_count=None,
        known_at=None,
    )
    empty = full.model_copy(update={"daily_calendar": (unknown_day,)})
    _seed_evidence(factory, full, include_recurring=False, active=False)
    trained = train_primary_forecaster(full, policy=_policy())
    absent = build_balance_forecast_path(
        factory,
        dataset=empty,
        trained=trained,
        plan=_path_plan(full),
    )
    assert absent.freshness_warnings == (
        FreshnessWarningCode.ACCOUNT_INACTIVE,
        FreshnessWarningCode.NO_VERIFIED_TRANSACTIONS,
        FreshnessWarningCode.NO_VERIFIED_COVERAGE,
    )

    with session_scope(factory) as session:
        account = session.get(AccountRecord, str(full.plan.account_ids[0]))
        assert account is not None
        account.is_active = True
    observations = full.daily_calendar
    no_transactions = tuple(
        item.model_copy(update={"transaction_count": 0})
        for item in (*observations[:5], *observations[-5:])
    )
    split = full.model_copy(update={"daily_calendar": no_transactions})
    split_result = build_balance_forecast_path(
        factory,
        dataset=split,
        trained=trained,
        plan=_path_plan(full),
    )
    assert split_result.freshness_warnings == (
        FreshnessWarningCode.NO_VERIFIED_TRANSACTIONS,
        FreshnessWarningCode.INSUFFICIENT_CONTIGUOUS_COVERAGE,
    )

    old_only = full.model_copy(update={"daily_calendar": observations[:5]})
    old_result = build_balance_forecast_path(
        factory,
        dataset=old_only,
        trained=trained,
        plan=_path_plan(full),
    )
    assert old_result.freshness_warnings == (
        FreshnessWarningCode.TRANSACTIONS_STALE,
        FreshnessWarningCode.COVERAGE_STALE,
        FreshnessWarningCode.INSUFFICIENT_CONTIGUOUS_COVERAGE,
    )

    old_with_transactions = observations[:5]
    recent_without_transactions = tuple(
        item.model_copy(update={"transaction_count": 0}) for item in observations[-5:]
    )
    outside = full.model_copy(
        update={
            "daily_calendar": (
                *old_with_transactions,
                *recent_without_transactions,
            )
        }
    )
    outside_result = build_balance_forecast_path(
        factory,
        dataset=outside,
        trained=trained,
        plan=_path_plan(full),
    )
    assert outside_result.freshness_warnings == (
        FreshnessWarningCode.TRANSACTIONS_STALE,
        FreshnessWarningCode.INSUFFICIENT_CONTIGUOUS_COVERAGE,
        FreshnessWarningCode.LATEST_TRANSACTION_OUTSIDE_CONTIGUOUS_COVERAGE,
    )


def test_recurrence_calendar_quantile_and_baseline_helpers(
    factory: sessionmaker[Session],
) -> None:
    assert path_module._quantile((Decimal("7"),), Decimal("0.8")) == Decimal("7")
    assert path_module._advance_recurrence(date(2024, 1, 1), "weekly") == date(
        2024, 1, 8
    )
    assert path_module._advance_recurrence(date(2024, 1, 1), "fortnightly") == date(
        2024, 1, 15
    )
    assert path_module._advance_recurrence(date(2024, 2, 29), "annual") == date(
        2025, 2, 28
    )

    dataset = _daily_dataset(weeks=60)
    _seed_evidence(factory, dataset)
    plan = _path_plan(dataset)
    with session_scope(factory) as session:
        candidate = session.get(RecurringPaymentCandidateRecord, "candidate-expense")
        assert candidate is not None
        candidate.next_expected_date = plan.forecast_start - timedelta(days=31)
    trained = train_primary_forecaster(dataset, policy=_policy())
    result = build_balance_forecast_path(
        factory, dataset=dataset, trained=trained, plan=plan
    )
    assert any(
        item.candidate_id == "candidate-expense"
        and item.occurrence_date >= plan.forecast_start
        for item in result.recurring_occurrences
    )

    row = build_next_forecast_inference_row(dataset)
    history = tuple((week, amount) for week, amount, _known in trained.target_history)
    seasonal = replace(
        trained,
        estimator=None,
        comparison=trained.comparison.model_copy(
            update={
                "selected": False,
                "selected_model": ForecastBaselineName.SEASONAL_NAIVE,
            }
        ),
    )
    assert path_module._baseline_value(seasonal, row, history) == history[-52][1]
    historical = replace(
        seasonal,
        comparison=seasonal.comparison.model_copy(
            update={"selected_model": ForecastBaselineName.HISTORICAL_MEAN}
        ),
    )
    assert path_module._baseline_value(historical, row, ()) == 0
    zero = replace(
        seasonal,
        comparison=seasonal.comparison.model_copy(
            update={"selected_model": ForecastBaselineName.RECURRING_ONLY}
        ),
    )
    assert path_module._baseline_value(zero, row, history) == 0


def test_eight_week_history_uses_minimum_uncertainty_pool(
    factory: sessionmaker[Session],
) -> None:
    dataset = _daily_dataset(weeks=8)
    _seed_evidence(factory, dataset, include_recurring=False)
    trained = train_primary_forecaster(dataset, policy=_policy())
    result = build_balance_forecast_path(
        factory,
        dataset=dataset,
        trained=trained,
        plan=_path_plan(dataset),
    )
    assert ForecastPathWarningCode.LIMITED_RESIDUAL_HISTORY in result.warnings
    assert result.upper_final_balance > result.lower_final_balance


def test_dataset_and_model_next_weeks_must_match(
    factory: sessionmaker[Session],
) -> None:
    dataset = _daily_dataset()
    _seed_evidence(factory, dataset)
    trained = train_primary_forecaster(dataset, policy=_policy())
    shifted_targets = tuple(
        target.model_copy(
            update={
                "week_start": target.week_start + timedelta(weeks=1),
                "week_end": target.week_end + timedelta(weeks=1),
            }
        )
        for target in dataset.weekly_targets[-8:]
    )
    assert dataset.next_recurring_outflow is not None
    changed = dataset.model_copy(
        update={
            "weekly_targets": (*dataset.weekly_targets[:-8], *shifted_targets),
            "next_recurring_outflow": dataset.next_recurring_outflow.model_copy(
                update={
                    "week_start": dataset.next_recurring_outflow.week_start
                    + timedelta(weeks=1)
                }
            ),
        }
    )
    with pytest.raises(ForecastPathError) as exc_info:
        build_balance_forecast_path(
            factory,
            dataset=changed,
            trained=trained,
            plan=_path_plan(dataset),
        )
    assert exc_info.value.code is ForecastPathErrorCode.FORECAST_EVIDENCE_MISALIGNED


def test_forecast_path_contracts_fail_closed() -> None:
    dataset = _daily_dataset()
    plan = _path_plan(dataset)
    with pytest.raises(ValidationError):
        ForecastPathPlan.model_validate(
            {
                **plan.model_dump(),
                "forecast_start": plan.forecast_start + timedelta(days=1),
            }
        )
    with pytest.raises(ValidationError):
        ForecastPathPlan.model_validate(
            {**plan.model_dump(), "knowledge_cutoff_at": datetime(2024, 1, 1)}
        )
    with pytest.raises(ValidationError):
        ForecastPathPlan.model_validate(
            {
                **plan.model_dump(),
                "knowledge_cutoff_at": datetime.combine(
                    plan.forecast_start, time.min, tzinfo=UTC
                ),
            }
        )
    with pytest.raises(ValidationError):
        ForecastPathPlan.model_validate(
            {
                **plan.model_dump(),
                "knowledge_cutoff_at": datetime.combine(
                    plan.forecast_start - timedelta(days=1),
                    time(23, 30),
                    tzinfo=timezone(timedelta(hours=-4)),
                ),
            }
        )
    for kind, amount in (
        (ScenarioAdjustmentKind.INFLOW, Decimal("-1.00")),
        (ScenarioAdjustmentKind.OUTFLOW, Decimal("1.00")),
        (ScenarioAdjustmentKind.INFLOW, Decimal("0.00")),
    ):
        with pytest.raises(ValidationError):
            ForecastScenarioAdjustment(
                adjustment_id="invalid",
                adjustment_date=plan.forecast_start,
                kind=kind,
                amount=amount,
            )
    duplicate = ForecastScenarioAdjustment(
        adjustment_id="duplicate",
        adjustment_date=plan.forecast_start,
        kind=ScenarioAdjustmentKind.INFLOW,
        amount=Decimal("1.00"),
    )
    with pytest.raises(ValidationError):
        ForecastScenario(adjustments=(duplicate, duplicate))

    valid_occurrence = RecurringForecastOccurrence(
        candidate_id="candidate",
        occurrence_date=plan.forecast_start,
        signed_amount=Decimal("10.00"),
        financial_role=FinancialRole.INCOME,
        known_at=plan.knowledge_cutoff_at,
    )
    for occurrence_update in (
        {"financial_role": FinancialRole.TRANSFER_OUT},
        {"signed_amount": Decimal("-10.00")},
        {"known_at": datetime(2024, 1, 1)},
    ):
        with pytest.raises(ValidationError):
            RecurringForecastOccurrence.model_validate(
                {**valid_occurrence.model_dump(), **occurrence_update}
            )
    with pytest.raises(ValidationError):
        RecurringForecastOccurrence.model_validate(
            {
                **valid_occurrence.model_dump(),
                "financial_role": FinancialRole.EXPENSE,
                "signed_amount": Decimal("10.00"),
            }
        )

    weekly = WeeklySpendingPath(
        week_start=plan.forecast_start,
        week_end=plan.forecast_start + timedelta(days=6),
        expected_discretionary_spending=Decimal("10.00"),
        lower_discretionary_spending=Decimal("5.00"),
        upper_discretionary_spending=Decimal("15.00"),
    )
    for weekly_update in (
        {"week_end": weekly.week_end + timedelta(days=1)},
        {"lower_discretionary_spending": Decimal("11.00")},
    ):
        with pytest.raises(ValidationError):
            WeeklySpendingPath.model_validate({**weekly.model_dump(), **weekly_update})
    daily = DailyBalancePathPoint(
        forecast_date=plan.forecast_start,
        expected_discretionary_outflow=Decimal("1.00"),
        recurring_net_flow=Decimal("0.00"),
        scenario_adjustment=Decimal("0.00"),
        expected_balance=Decimal("9.00"),
        lower_balance=Decimal("8.00"),
        upper_balance=Decimal("10.00"),
    )
    with pytest.raises(ValidationError):
        DailyBalancePathPoint.model_validate(
            {**daily.model_dump(), "upper_balance": Decimal("8.50")}
        )

    performance = ForecastIntervalPerformance(
        nominal_coverage=Decimal("0.8"),
        empirical_coverage=Decimal("0.75"),
        mean_interval_width=Decimal("10.00"),
        sample_count=4,
    )
    opening = ForecastOpeningBalance(
        balance=Decimal("10.00"),
        currency=Currency.GBP,
        as_of_date=plan.forecast_start - timedelta(days=1),
        recorded_at=plan.knowledge_cutoff_at,
        source=BalanceSnapshotSource.MANUAL,
    )
    with pytest.raises(ValidationError):
        ForecastOpeningBalance.model_validate(
            {**opening.model_dump(), "recorded_at": datetime(2024, 1, 1)}
        )
    base = BalanceForecastPath(
        plan=plan.model_copy(update={"horizon_days": 1}),
        scenario=ForecastScenario(),
        opening_balance=opening,
        selected_model=ForecastModelName.HIST_GRADIENT_BOOSTING,
        interval_method=ForecastIntervalMethod.RESIDUAL_BOOTSTRAP,
        widening_multiplier=Decimal("1"),
        warnings=(),
        freshness_warnings=(),
        recurring_occurrences=(),
        weekly_spending=(weekly,),
        daily_balances=(daily,),
        interval_performance=performance,
        expected_final_balance=Decimal("9.00"),
        lower_final_balance=Decimal("8.00"),
        upper_final_balance=Decimal("10.00"),
    )
    invalid_updates: tuple[dict[str, object], ...] = (
        {
            "daily_balances": (
                daily.model_copy(
                    update={"forecast_date": daily.forecast_date + timedelta(days=1)}
                ),
            )
        },
        {"weekly_spending": (weekly, weekly)},
        {"freshness_warnings": (FreshnessWarningCode.BALANCE_STALE,)},
        {"expected_final_balance": Decimal("8.00")},
    )
    for path_update in invalid_updates:
        with pytest.raises(ValidationError):
            BalanceForecastPath.model_validate({**base.model_dump(), **path_update})


def test_manual_path_demo_and_safe_parameters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["forecast-path-demo", "--horizon-days", "30", "--simulations", "100"],
    )
    path_demo.main()
    output = capsys.readouterr().out
    assert "CashFlow AI synthetic balance-path check" in output
    assert "selected model: hist_gradient_boosting" in output
    assert "forecast days: 30" in output
    assert "confirmed recurring events: 2" in output
    assert "likely range: £" in output
    assert "warnings: none" in output
    assert "held-out interval coverage:" in output

    monkeypatch.setattr(
        "sys.argv",
        [
            "forecast-path-demo",
            "--horizon-days",
            "14",
            "--simulations",
            "100",
            "--stale-balance-days",
            "10",
            "--scenario-outflow",
            "50",
        ],
    )
    path_demo.main()
    changed_output = capsys.readouterr().out
    assert "forecast days: 14" in changed_output
    assert "warnings: stale_data" in changed_output

    monkeypatch.setattr(
        "sys.argv",
        [
            "forecast-path-demo",
            "--history-weeks",
            "8",
            "--simulations",
            "100",
        ],
    )
    path_demo.main()
    low_data_output = capsys.readouterr().out
    assert "selected model: recent_rolling_mean" in low_data_output
    assert "held-out interval coverage:" not in low_data_output

    invalid_arguments = (
        ("--horizon-days", "6"),
        ("--history-weeks", "7"),
        ("--simulations", "99"),
        ("--stale-balance-days", "-1"),
        ("--scenario-outflow", "-1"),
    )
    for option, value in invalid_arguments:
        monkeypatch.setattr("sys.argv", ["forecast-path-demo", option, value])
        with pytest.raises(SystemExit):
            path_demo.main()
