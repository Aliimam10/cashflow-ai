"""Tests for leakage-safe forecast calendars, features, and baselines."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_cash_flow_analytics import _add_coverage, _add_transaction, _seed_foundation

import cashflow_ai.forecasting.demo as forecast_demo
import cashflow_ai.forecasting.service as forecast_service
from cashflow_ai.forecasting import (
    ForecastingDataError,
    ForecastingDataErrorCode,
    build_forecast_dataset,
    build_forecast_feature_rows,
    evaluate_forecast_baselines,
)
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    RecurringPaymentCandidateRecord,
    RecurringPaymentMemberRecord,
    RecurringSeriesRecord,
)
from cashflow_ai.schemas import (
    DailyForecastObservation,
    DateRange,
    ExpandingWindowFold,
    FinancialRole,
    ForecastBaselineName,
    ForecastDataset,
    ForecastDatasetPlan,
    ForecastDayStatus,
    WeeklyForecastTarget,
)

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine: Engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    _seed_foundation(result)
    return result


def _plan(start: date, end: date) -> ForecastDatasetPlan:
    return ForecastDatasetPlan(
        user_profile_id="profile-1",
        account_ids=("current-1",),
        period=DateRange(start_date=start, end_date=end),
        knowledge_cutoff_at=NOW,
        payday_days=(1, 15),
    )


def _covered_history(
    factory: sessionmaker[Session], *, weeks: int = 20, gap_week: int | None = None
) -> tuple[date, date]:
    start = date(2026, 1, 5)
    end = start + timedelta(weeks=weeks, days=-1)
    missing = None
    status = "complete"
    if gap_week is not None:
        gap_start = start + timedelta(weeks=gap_week)
        missing = [
            {
                "start_date": gap_start.isoformat(),
                "end_date": (gap_start + timedelta(days=6)).isoformat(),
            }
        ]
        status = "gapped"
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "forecast-coverage",
            start=start,
            end=end,
            coverage_status=status,
            missing=missing,
        )
        for index in range(weeks):
            _add_transaction(
                session,
                f"forecast-{index}",
                transaction_date=start + timedelta(weeks=index),
                amount=str(-(70 + index % 4 * 10)),
                role=FinancialRole.EXPENSE,
            )
    return start, end


def test_daily_calendar_keeps_covered_zeroes_and_unknown_gaps(
    factory: sessionmaker[Session],
) -> None:
    start, end = _covered_history(factory, weeks=20, gap_week=10)
    dataset = build_forecast_dataset(factory, plan=_plan(start, end))
    by_date = {item.observation_date: item for item in dataset.daily_calendar}
    assert by_date[start].discretionary_spending == Decimal("70.00")
    assert by_date[start + timedelta(days=1)].status is ForecastDayStatus.COVERED
    assert by_date[start + timedelta(days=1)].discretionary_spending == Decimal("0.00")
    gap = start + timedelta(weeks=10)
    assert by_date[gap].status is ForecastDayStatus.UNKNOWN
    assert by_date[gap].discretionary_spending is None
    assert all(item.week_start != gap for item in dataset.weekly_targets)
    assert all(
        item.week_start < gap or item.week_start >= gap + timedelta(weeks=9)
        for item in dataset.feature_rows
    )


def test_features_use_only_consecutive_prior_weeks_and_payday_calendar() -> None:
    first = date(2026, 1, 5)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(index * 10),
            known_recurring_outflow=Decimal("5.00"),
        )
        for index in range(10)
    )
    rows = build_forecast_feature_rows(targets, (1, 15))
    assert len(rows) == 2
    assert rows[0].target == Decimal("80.00")
    assert rows[0].lag_1 == Decimal("70.00")
    assert rows[0].lag_2 == Decimal("60.00")
    assert rows[0].lag_4 == Decimal("40.00")
    assert rows[0].rolling_mean_4 == Decimal("55.00")
    assert rows[0].rolling_mean_8 == Decimal("35.00")
    assert rows[0].month == 3
    assert rows[0].known_recurring_outflow == Decimal("5.00")


def test_confirmed_recurring_members_are_removed_from_discretionary_target(
    factory: sessionmaker[Session],
) -> None:
    start, end = _covered_history(factory, weeks=10)
    with session_scope(factory) as session:
        transaction = _add_transaction(
            session,
            "confirmed-recurring",
            transaction_date=start,
            amount="-25.00",
            role=FinancialRole.EXPENSE,
        )
        session.add(
            RecurringSeriesRecord(
                id="series-forecast",
                account_id="current-1",
                merchant_pattern="synthetic recurring",
                expected_amount=Decimal("-25.00"),
                interval_days=30,
                financial_role_id="expense",
                is_active=True,
                created_at=NOW,
            )
        )
        session.add(
            RecurringSeriesRecord(
                id="series-outside-period",
                account_id="current-1",
                merchant_pattern="synthetic future recurring",
                expected_amount=Decimal("-10.00"),
                interval_days=30,
                financial_role_id="expense",
                is_active=True,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentCandidateRecord(
                id="candidate-forecast",
                account_id="current-1",
                recurring_series_id="series-forecast",
                merchant_group="synthetic recurring",
                expected_amount=Decimal("-25.00"),
                frequency="monthly",
                interval_days=30,
                next_expected_date=date(2026, 3, 1),
                confidence=Decimal("0.9"),
                covered_missed_count=0,
                status="confirmed",
                detected_at=NOW,
                reviewed_at=NOW,
            )
        )
        session.add(
            RecurringPaymentCandidateRecord(
                id="candidate-outside-period",
                account_id="current-1",
                recurring_series_id="series-outside-period",
                merchant_group="synthetic future recurring",
                expected_amount=Decimal("-10.00"),
                frequency="monthly",
                interval_days=30,
                next_expected_date=date(2026, 12, 1),
                confidence=Decimal("0.9"),
                covered_missed_count=0,
                status="confirmed",
                detected_at=NOW,
                reviewed_at=NOW,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentMemberRecord(
                candidate_id="candidate-forecast",
                verified_transaction_id=transaction.id,
            )
        )
    dataset = build_forecast_dataset(factory, plan=_plan(start, end))
    assert dataset.weekly_targets[0].discretionary_spending == Decimal("70.00")
    recurring_week = next(
        item for item in dataset.weekly_targets if item.week_start == date(2026, 2, 23)
    )
    assert recurring_week.known_recurring_outflow == Decimal("25.00")


def test_baselines_use_final_chronological_test_and_expanding_training() -> None:
    first = date(2025, 1, 6)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(100 + index),
            known_recurring_outflow=Decimal("0"),
        )
        for index in range(64)
    )
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic-profile",
        account_ids=("synthetic-account",),
        period=DateRange(
            start_date=first, end_date=first + timedelta(weeks=64, days=-1)
        ),
        knowledge_cutoff_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    dataset = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets,
        feature_rows=build_forecast_feature_rows(targets, plan.payday_days),
    )
    evaluation = evaluate_forecast_baselines(
        dataset, initial_training_weeks=10, final_test_weeks=4
    )
    assert max(evaluation.final_training_week_starts) < min(
        evaluation.final_test_week_starts
    )
    assert all(
        max(fold.training_week_starts) < min(fold.test_week_starts)
        for fold in evaluation.expanding_folds
    )
    assert {item.baseline for item in evaluation.metrics} == set(ForecastBaselineName)
    seasonal = next(
        item
        for item in evaluation.metrics
        if item.baseline is ForecastBaselineName.SEASONAL_NAIVE
    )
    assert len(seasonal.predictions) == 4
    assert all(item.mae >= 0 and item.rmse >= 0 for item in evaluation.metrics)


def test_scope_cutoffs_and_evaluation_errors_are_controlled(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(ValidationError):
        ForecastDatasetPlan(
            user_profile_id="profile-1",
            account_ids=("current-1", "current-1"),
            period=DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 2)),
            knowledge_cutoff_at=datetime(2026, 1, 1),
            payday_days=(0,),
        )
    valid = _plan(date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(ForecastingDataError) as exc_info:
        build_forecast_dataset(
            factory, plan=valid.model_copy(update={"user_profile_id": "missing"})
        )
    assert exc_info.value.code is ForecastingDataErrorCode.PROFILE_NOT_FOUND
    with pytest.raises(ForecastingDataError) as exc_info:
        build_forecast_dataset(
            factory, plan=valid.model_copy(update={"account_ids": ("missing",)})
        )
    assert exc_info.value.code is ForecastingDataErrorCode.ACCOUNT_SCOPE_NOT_FOUND
    empty = ForecastDataset(
        plan=valid, daily_calendar=(), weekly_targets=(), feature_rows=()
    )
    with pytest.raises(ForecastingDataError) as exc_info:
        evaluate_forecast_baselines(empty, initial_training_weeks=1, final_test_weeks=1)
    assert exc_info.value.code is ForecastingDataErrorCode.TOO_FEW_COMPLETE_WEEKS
    with pytest.raises(ForecastingDataError) as exc_info:
        evaluate_forecast_baselines(empty, initial_training_weeks=0, final_test_weeks=1)
    assert exc_info.value.code is ForecastingDataErrorCode.INVALID_EVALUATION_POLICY


def test_forecast_contract_invariants() -> None:
    with pytest.raises(ValidationError):
        ForecastDatasetPlan(
            user_profile_id="profile-1",
            account_ids=("current-1",),
            period=DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 2)),
            knowledge_cutoff_at=datetime(2026, 1, 1),
        )
    with pytest.raises(ValidationError):
        ForecastDatasetPlan(
            user_profile_id="profile-1",
            account_ids=("current-1",),
            period=DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 3)),
            knowledge_cutoff_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        ForecastDatasetPlan(
            user_profile_id="profile-1",
            account_ids=("current-1",),
            period=DateRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 2)),
            knowledge_cutoff_at=datetime(2026, 1, 2, tzinfo=UTC),
            payday_days=(1, 1),
        )
    with pytest.raises(ValidationError):
        DailyForecastObservation(
            observation_date=date(2026, 1, 1),
            status=ForecastDayStatus.UNKNOWN,
            discretionary_spending=Decimal("0"),
            transaction_count=0,
        )
    with pytest.raises(ValidationError):
        DailyForecastObservation(
            observation_date=date(2026, 1, 1),
            status=ForecastDayStatus.COVERED,
            discretionary_spending=Decimal("-1"),
            transaction_count=1,
        )
    with pytest.raises(ValidationError):
        WeeklyForecastTarget(
            week_start=date(2026, 1, 6),
            week_end=date(2026, 1, 12),
            discretionary_spending=Decimal("1"),
            known_recurring_outflow=Decimal("0"),
        )
    with pytest.raises(ValidationError):
        WeeklyForecastTarget(
            week_start=date(2026, 1, 5),
            week_end=date(2026, 1, 11),
            discretionary_spending=Decimal("-1"),
            known_recurring_outflow=Decimal("0"),
        )
    with pytest.raises(ValidationError):
        ExpandingWindowFold(
            training_week_starts=(date(2026, 2, 1),),
            test_week_starts=(date(2026, 1, 1),),
        )


def test_partial_future_gap_empty_calendar_and_minimal_fold_paths(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        _add_coverage(
            session,
            "partial-forecast",
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            coverage_status="partial",
        )
        _add_coverage(
            session,
            "future-gap-forecast",
            start=date(2026, 2, 1),
            end=date(2026, 9, 1),
            coverage_status="gapped",
            missing=[{"start_date": "2026-06-01", "end_date": "2026-07-01"}],
        )
    dataset = build_forecast_dataset(
        factory,
        plan=_plan(date(2026, 1, 1), date(2026, 5, 31)),
    )
    assert dataset.daily_calendar[0].status is ForecastDayStatus.UNKNOWN
    assert forecast_service._weekly_targets((), {}) == ()

    first = date(2026, 1, 5)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(index + 1),
            known_recurring_outflow=Decimal("0"),
        )
        for index in range(10)
    )
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic",
        account_ids=("synthetic",),
        period=DateRange(
            start_date=first, end_date=first + timedelta(weeks=10, days=-1)
        ),
        knowledge_cutoff_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    compact = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets,
        feature_rows=build_forecast_feature_rows(targets, plan.payday_days),
    )
    evaluation = evaluate_forecast_baselines(
        compact, initial_training_weeks=1, final_test_weeks=1
    )
    assert len(evaluation.expanding_folds) == 1


def test_manual_demo_output_and_parameter_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["forecast-demo", "--weeks", "20", "--test-weeks", "3", "--gap-week", "2"],
    )
    forecast_demo.main()
    output = capsys.readouterr().out
    assert "CashFlow AI synthetic forecast-data check" in output
    assert "gap retained" in output
    monkeypatch.setattr(
        "sys.argv", ["forecast-demo", "--weeks", "20", "--test-weeks", "3"]
    )
    forecast_demo.main()
    assert "gap retained" not in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["forecast-demo", "--weeks", "11"])
    with pytest.raises(SystemExit):
        forecast_demo.main()
