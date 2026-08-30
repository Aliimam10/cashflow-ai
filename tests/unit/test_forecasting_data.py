"""Tests for leakage-safe forecast calendars, features, and baselines."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
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
    validate_forecast_dataset,
)
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    FinancialRoleAuditRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
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
    ForecastFeatureRow,
    RecurringOutflowProjection,
    WeeklyForecastTarget,
)

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


def _known_after_week(week_start: date) -> datetime:
    return datetime.combine(week_start + timedelta(days=6), time.max, tzinfo=UTC)


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


def _confirm_role(
    session: Session,
    transaction_id: str,
    role: FinancialRole,
    *,
    changed_at: datetime = NOW,
) -> None:
    session.add(
        FinancialRoleAuditRecord(
            id=f"role-{transaction_id}-{changed_at.isoformat()}",
            verified_transaction_id=transaction_id,
            previous_role_id=FinancialRole.UNKNOWN.value,
            new_role_id=role.value,
            source="user_confirmation",
            changed_at=changed_at,
        )
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
            transaction = _add_transaction(
                session,
                f"forecast-{index}",
                transaction_date=start + timedelta(weeks=index),
                amount=str(-(70 + index % 4 * 10)),
                role=FinancialRole.EXPENSE,
            )
            _confirm_role(session, transaction.id, FinancialRole.EXPENSE)
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
    assert by_date[start + timedelta(days=1)].known_at == NOW
    gap = start + timedelta(weeks=10)
    assert by_date[gap].status is ForecastDayStatus.UNKNOWN
    assert by_date[gap].discretionary_spending is None
    assert all(item.week_start != gap for item in dataset.weekly_targets)
    assert all(
        item.week_start < gap or item.week_start >= gap + timedelta(weeks=9)
        for item in dataset.feature_rows
    )
    assert dataset.feature_rows == ()


def test_coverage_is_known_only_after_confirmation_and_overlaps_union(
    factory: sessionmaker[Session],
) -> None:
    start = date(2026, 1, 5)
    end = start + timedelta(days=13)
    with session_scope(factory) as session:
        late = _add_coverage(
            session,
            "late-coverage",
            start=start,
            end=end,
        )
        late_context = session.get(ImportContextRecord, late.import_context_id)
        assert late_context is not None
        late_context.created_at = NOW + timedelta(seconds=1)
    late_dataset = build_forecast_dataset(factory, plan=_plan(start, end))
    assert all(
        item.status is ForecastDayStatus.UNKNOWN for item in late_dataset.daily_calendar
    )

    with session_scope(factory) as session:
        _add_coverage(
            session,
            "complete-overlap",
            start=start,
            end=end,
        )
        _add_coverage(
            session,
            "gapped-overlap",
            start=start,
            end=end,
            coverage_status="gapped",
            missing=[
                {
                    "start_date": start.isoformat(),
                    "end_date": (start + timedelta(days=6)).isoformat(),
                }
            ],
        )
    overlap_dataset = build_forecast_dataset(factory, plan=_plan(start, end))
    assert all(
        item.status is ForecastDayStatus.COVERED
        for item in overlap_dataset.daily_calendar
    )


def test_financial_roles_are_reconstructed_at_the_knowledge_cutoff(
    factory: sessionmaker[Session],
) -> None:
    start = date(2026, 1, 5)
    end = start + timedelta(days=6)
    with session_scope(factory) as session:
        _add_coverage(session, "role-coverage", start=start, end=end)
        known = _add_transaction(
            session,
            "known-expense",
            transaction_date=start,
            amount="-20.00",
            role=FinancialRole.EXPENSE,
        )
        future = _add_transaction(
            session,
            "future-expense",
            transaction_date=start + timedelta(days=1),
            amount="-90.00",
            role=FinancialRole.EXPENSE,
        )
        _confirm_role(session, known.id, FinancialRole.EXPENSE)
        _confirm_role(
            session,
            future.id,
            FinancialRole.EXPENSE,
            changed_at=NOW + timedelta(seconds=1),
        )
    dataset = build_forecast_dataset(factory, plan=_plan(start, end))
    by_date = {item.observation_date: item for item in dataset.daily_calendar}
    assert by_date[start].discretionary_spending == Decimal("20.00")
    assert by_date[start].known_at == NOW
    assert by_date[start + timedelta(days=1)].status is ForecastDayStatus.UNKNOWN
    assert dataset.weekly_targets == ()


def test_forecast_transactions_require_trusted_owned_source_lineage(
    factory: sessionmaker[Session],
) -> None:
    start = date(2026, 1, 5)
    end = start + timedelta(days=6)
    with session_scope(factory) as session:
        _add_coverage(session, "trusted-lineage-coverage", start=start, end=end)
        transactions = {
            name: _add_transaction(
                session,
                name,
                transaction_date=start,
                amount=amount,
                role=FinancialRole.EXPENSE,
            )
            for name, amount in (
                ("trusted-csv", "-10.00"),
                ("reviewed-csv", "-20.00"),
                ("trusted-pdf", "-30.00"),
                ("unverified-csv", "-40.00"),
                ("unreviewed-pdf", "-50.00"),
                ("source-mismatch", "-60.00"),
                ("wrong-account-lineage", "-70.00"),
                ("future-import", "-80.00"),
                ("unconfirmed-raw", "-90.00"),
            )
        }
        for transaction in transactions.values():
            _confirm_role(session, transaction.id, FinancialRole.EXPENSE)

        reviewed_csv_batch = session.get(ImportBatchRecord, "batch-reviewed-csv")
        assert reviewed_csv_batch is not None
        reviewed_csv_batch.verification_status = "needs_review"

        for transaction_id, status in (
            ("trusted-pdf", "verified"),
            ("unreviewed-pdf", "needs_review"),
        ):
            transaction = transactions[transaction_id]
            raw = session.get(RawTransactionRecord, transaction.raw_transaction_id)
            batch = session.get(ImportBatchRecord, f"batch-{transaction_id}")
            assert raw is not None
            assert batch is not None
            raw.source_type = "digital_pdf"
            raw.source_row_number = None
            raw.page_number = 1
            raw.page_record_number = 1
            batch.source_type = "digital_pdf"
            batch.verification_status = status

        unverified_batch = session.get(ImportBatchRecord, "batch-unverified-csv")
        mismatch_batch = session.get(ImportBatchRecord, "batch-source-mismatch")
        wrong_account_batch = session.get(
            ImportBatchRecord, "batch-wrong-account-lineage"
        )
        future_batch = session.get(ImportBatchRecord, "batch-future-import")
        unconfirmed_raw = session.get(
            RawTransactionRecord,
            transactions["unconfirmed-raw"].raw_transaction_id,
        )
        assert unverified_batch is not None
        assert mismatch_batch is not None
        assert wrong_account_batch is not None
        assert future_batch is not None
        assert unconfirmed_raw is not None
        unverified_batch.verification_status = "unverified"
        mismatch_batch.source_type = "digital_pdf"
        wrong_account_batch.account_id = "other-1"
        future_batch.imported_at = NOW + timedelta(seconds=1)
        unconfirmed_raw.review_status = "needs_review"

    dataset = build_forecast_dataset(factory, plan=_plan(start, end))

    observation = dataset.daily_calendar[0]
    assert observation.status is ForecastDayStatus.COVERED
    assert observation.discretionary_spending == Decimal("60.00")
    assert observation.transaction_count == 3


def test_transaction_import_time_is_part_of_point_in_time_availability(
    factory: sessionmaker[Session],
) -> None:
    start = date(2026, 1, 5)
    old_evidence_at = datetime(2026, 1, 6, 12, tzinfo=UTC)
    source_available_at = NOW - timedelta(hours=1)
    with session_scope(factory) as session:
        coverage = _add_coverage(
            session,
            "source-time-coverage",
            start=start,
            end=start,
        )
        context = session.get(ImportContextRecord, coverage.import_context_id)
        coverage_batch = session.get(ImportBatchRecord, "source-time-coverage")
        assert context is not None
        assert coverage_batch is not None
        context.created_at = old_evidence_at
        coverage_batch.imported_at = old_evidence_at
        transaction = _add_transaction(
            session,
            "source-time-expense",
            transaction_date=start,
            amount="-12.00",
            role=FinancialRole.EXPENSE,
        )
        transaction.verified_at = old_evidence_at
        raw = session.get(RawTransactionRecord, transaction.raw_transaction_id)
        batch = session.get(ImportBatchRecord, "batch-source-time-expense")
        assert raw is not None
        assert batch is not None
        raw.created_at = old_evidence_at
        batch.imported_at = source_available_at
        _confirm_role(
            session,
            transaction.id,
            FinancialRole.EXPENSE,
            changed_at=old_evidence_at,
        )

    observation = build_forecast_dataset(
        factory, plan=_plan(start, start)
    ).daily_calendar[0]
    assert observation.discretionary_spending == Decimal("12.00")
    assert observation.known_at == source_available_at


def test_partial_current_day_cannot_complete_a_week(
    factory: sessionmaker[Session],
) -> None:
    start = date(2026, 8, 10)
    sunday = start + timedelta(days=6)
    sunday_noon = datetime.combine(sunday, time(12), tzinfo=UTC)
    with session_scope(factory) as session:
        _add_coverage(session, "partial-sunday", start=start, end=sunday)

    plan = ForecastDatasetPlan(
        user_profile_id="profile-1",
        account_ids=("current-1",),
        period=DateRange(start_date=start, end_date=sunday),
        knowledge_cutoff_at=sunday_noon,
        payday_days=(1, 15),
    )
    dataset = build_forecast_dataset(factory, plan=plan)

    assert dataset.daily_calendar[-2].known_at == datetime.combine(
        sunday - timedelta(days=1), time.max, tzinfo=UTC
    )
    assert dataset.daily_calendar[-1].status is ForecastDayStatus.UNKNOWN
    assert dataset.daily_calendar[-1].known_at is None
    assert dataset.weekly_targets == ()


def test_features_use_only_consecutive_prior_weeks_and_payday_calendar() -> None:
    first = date(2026, 1, 5)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(index * 10),
            known_recurring_outflow=Decimal("5.00"),
            known_at=_known_after_week(first + timedelta(weeks=index)),
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


def test_late_known_lag_suppresses_only_origins_that_could_not_know_it() -> None:
    first = date(2026, 1, 5)
    targets = [
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(index),
            known_recurring_outflow=Decimal("0"),
            known_at=_known_after_week(first + timedelta(weeks=index)),
        )
        for index in range(10)
    ]
    first_forecast_origin = datetime.combine(
        first + timedelta(weeks=8), time.min, tzinfo=UTC
    )
    targets[7] = targets[7].model_copy(
        update={"known_at": first_forecast_origin + timedelta(days=1)}
    )

    rows = build_forecast_feature_rows(tuple(targets), (1, 15))

    assert tuple(row.week_start for row in rows) == (first + timedelta(weeks=9),)
    assert rows[0].lag_2 == Decimal("7")


def test_confirmed_recurring_members_are_removed_from_discretionary_target(
    factory: sessionmaker[Session],
) -> None:
    start, end = _covered_history(factory, weeks=20)
    confirmed_at = datetime(2026, 2, 10, 12, tzinfo=UTC)
    with session_scope(factory) as session:
        transaction = _add_transaction(
            session,
            "confirmed-recurring",
            transaction_date=start,
            amount="-25.00",
            role=FinancialRole.EXPENSE,
        )
        transaction.verified_at = confirmed_at
        raw = session.get(RawTransactionRecord, transaction.raw_transaction_id)
        batch = session.get(ImportBatchRecord, "batch-confirmed-recurring")
        assert raw is not None
        assert batch is not None
        raw.created_at = confirmed_at
        batch.imported_at = confirmed_at
        _confirm_role(session, transaction.id, FinancialRole.EXPENSE)
        session.add(
            RecurringSeriesRecord(
                id="series-forecast",
                account_id="current-1",
                merchant_pattern="synthetic recurring",
                expected_amount=Decimal("-25.00"),
                interval_days=30,
                financial_role_id="expense",
                is_active=True,
                created_at=confirmed_at,
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
                created_at=confirmed_at,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentCandidateRecord(
                id="candidate-forecast",
                account_id="current-1",
                recurring_series_id="series-forecast",
                merchant_group="synthetic recurring",
                currency="GBP",
                direction="outflow",
                financial_role_id="expense",
                expected_amount=Decimal("-25.00"),
                frequency="monthly",
                interval_days=30,
                next_expected_date=date(2026, 3, 1),
                confidence=Decimal("0.9"),
                covered_missed_count=0,
                status="confirmed",
                detected_at=confirmed_at,
                evidence_as_of_date=start,
                knowledge_cutoff_at=confirmed_at,
                reviewed_at=confirmed_at,
            )
        )
        session.add(
            RecurringPaymentCandidateRecord(
                id="candidate-outside-period",
                account_id="current-1",
                recurring_series_id="series-outside-period",
                merchant_group="synthetic future recurring",
                currency="GBP",
                direction="outflow",
                financial_role_id="expense",
                expected_amount=Decimal("-10.00"),
                frequency="monthly",
                interval_days=30,
                next_expected_date=date(2026, 12, 1),
                confidence=Decimal("0.9"),
                covered_missed_count=0,
                status="confirmed",
                detected_at=confirmed_at,
                evidence_as_of_date=start,
                knowledge_cutoff_at=confirmed_at,
                reviewed_at=confirmed_at,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentMemberRecord(
                candidate_id="candidate-forecast",
                verified_transaction_id=transaction.id,
                identified_at=confirmed_at,
            )
        )
    dataset = build_forecast_dataset(factory, plan=_plan(start, end))
    assert dataset.weekly_targets[0].discretionary_spending == Decimal("70.00")
    before_confirmation = next(
        item for item in dataset.weekly_targets if item.week_start == date(2026, 2, 2)
    )
    assert before_confirmation.known_recurring_outflow == Decimal("0.00")
    first_projected_week = next(
        item for item in dataset.weekly_targets if item.week_start == date(2026, 3, 2)
    )
    later_projected_week = next(
        item for item in dataset.weekly_targets if item.week_start == date(2026, 3, 30)
    )
    assert first_projected_week.known_recurring_outflow == Decimal("25.00")
    assert later_projected_week.known_recurring_outflow == Decimal("25.00")


def test_recurrence_learned_after_cutoff_is_not_used_or_excluded(
    factory: sessionmaker[Session],
) -> None:
    start = date(2026, 1, 5)
    end = start + timedelta(days=13)
    evidence_known_at = datetime(2026, 1, 6, 12, tzinfo=UTC)
    reviewed_at = datetime(2026, 1, 7, 12, tzinfo=UTC)
    with session_scope(factory) as session:
        _add_coverage(session, "late-candidate-coverage", start=start, end=end)
        transaction = _add_transaction(
            session,
            "late-candidate-member",
            transaction_date=start,
            amount="-25.00",
            role=FinancialRole.EXPENSE,
        )
        transaction.verified_at = evidence_known_at
        raw = session.get(RawTransactionRecord, transaction.raw_transaction_id)
        batch = session.get(ImportBatchRecord, "batch-late-candidate-member")
        assert raw is not None
        assert batch is not None
        raw.created_at = evidence_known_at
        batch.imported_at = evidence_known_at
        _confirm_role(
            session,
            transaction.id,
            FinancialRole.EXPENSE,
            changed_at=evidence_known_at,
        )
        session.add(
            RecurringSeriesRecord(
                id="late-candidate-series",
                account_id="current-1",
                merchant_pattern="synthetic weekly bill",
                expected_amount=Decimal("-25.00"),
                interval_days=7,
                financial_role_id="expense",
                is_active=True,
                created_at=reviewed_at,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentCandidateRecord(
                id="late-knowledge-candidate",
                account_id="current-1",
                recurring_series_id="late-candidate-series",
                merchant_group="synthetic weekly bill",
                currency="GBP",
                direction="outflow",
                financial_role_id="expense",
                expected_amount=Decimal("-25.00"),
                frequency="weekly",
                interval_days=7,
                next_expected_date=start + timedelta(weeks=1),
                confidence=Decimal("0.9"),
                covered_missed_count=0,
                status="confirmed",
                detected_at=reviewed_at,
                evidence_as_of_date=start,
                knowledge_cutoff_at=NOW + timedelta(microseconds=1),
                reviewed_at=reviewed_at,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentMemberRecord(
                candidate_id="late-knowledge-candidate",
                verified_transaction_id=transaction.id,
                identified_at=reviewed_at,
            )
        )

    dataset = build_forecast_dataset(factory, plan=_plan(start, end))

    assert dataset.weekly_targets[0].discretionary_spending == Decimal("25.00")
    assert all(
        target.known_recurring_outflow == Decimal("0.00")
        for target in dataset.weekly_targets
    )


def test_migrated_recurrence_uses_marker_as_effective_confirmation(
    factory: sessionmaker[Session],
) -> None:
    start = date(2026, 1, 5)
    end = start + timedelta(days=20)
    original_review = datetime(2026, 1, 1, tzinfo=UTC)
    migration_marker = datetime(2026, 1, 7, 12, tzinfo=UTC)
    with session_scope(factory) as session:
        coverage = _add_coverage(
            session, "legacy-marker-coverage", start=start, end=end
        )
        context = session.get(ImportContextRecord, coverage.import_context_id)
        coverage_batch = session.get(ImportBatchRecord, "legacy-marker-coverage")
        assert context is not None
        assert coverage_batch is not None
        context.created_at = original_review
        coverage_batch.imported_at = original_review
        transaction = _add_transaction(
            session,
            "legacy-marker-member",
            transaction_date=start,
            amount="-25.00",
            role=FinancialRole.EXPENSE,
        )
        transaction.verified_at = original_review
        raw = session.get(RawTransactionRecord, transaction.raw_transaction_id)
        batch = session.get(ImportBatchRecord, "batch-legacy-marker-member")
        assert raw is not None
        assert batch is not None
        raw.created_at = original_review
        batch.imported_at = original_review
        _confirm_role(
            session,
            transaction.id,
            FinancialRole.EXPENSE,
            changed_at=original_review,
        )
        session.add(
            RecurringSeriesRecord(
                id="legacy-marker-series",
                account_id="current-1",
                merchant_pattern="synthetic legacy weekly bill",
                expected_amount=Decimal("-25.00"),
                interval_days=7,
                financial_role_id="expense",
                is_active=True,
                created_at=original_review,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentCandidateRecord(
                id="legacy-marker-candidate",
                account_id="current-1",
                recurring_series_id="legacy-marker-series",
                merchant_group="synthetic legacy weekly bill",
                currency="GBP",
                direction="outflow",
                financial_role_id="expense",
                expected_amount=Decimal("-25.00"),
                frequency="weekly",
                interval_days=7,
                next_expected_date=start + timedelta(weeks=1),
                confidence=Decimal("0.9"),
                covered_missed_count=0,
                status="confirmed",
                detected_at=original_review,
                evidence_as_of_date=start,
                knowledge_cutoff_at=migration_marker,
                reviewed_at=original_review,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentMemberRecord(
                candidate_id="legacy-marker-candidate",
                verified_transaction_id=transaction.id,
                identified_at=migration_marker,
            )
        )

    dataset = build_forecast_dataset(factory, plan=_plan(start, end))
    daily = {item.observation_date: item for item in dataset.daily_calendar}
    weekly = {item.week_start: item for item in dataset.weekly_targets}

    assert daily[start].discretionary_spending == Decimal("0.00")
    assert daily[start].known_at == migration_marker
    assert weekly[start + timedelta(weeks=1)].known_recurring_outflow == Decimal(
        "25.00"
    )


def test_midweek_recurrence_confirmation_starts_with_a_later_forecast_origin(
    factory: sessionmaker[Session],
) -> None:
    start, end = _covered_history(factory, weeks=4)
    confirmation = datetime(2026, 1, 14, 12, tzinfo=UTC)
    with session_scope(factory) as session:
        evidence = _add_transaction(
            session,
            "midweek-recurring-evidence",
            transaction_date=date(2026, 1, 8),
            amount="-25.00",
            role=FinancialRole.EXPENSE,
        )
        evidence.verified_at = confirmation
        raw = session.get(RawTransactionRecord, evidence.raw_transaction_id)
        batch = session.get(ImportBatchRecord, "batch-midweek-recurring-evidence")
        assert raw is not None
        assert batch is not None
        raw.created_at = confirmation
        batch.imported_at = confirmation
        _confirm_role(session, evidence.id, FinancialRole.EXPENSE)
        session.add(
            RecurringSeriesRecord(
                id="midweek-series",
                account_id="current-1",
                merchant_pattern="synthetic weekly bill",
                expected_amount=Decimal("-25.00"),
                interval_days=7,
                financial_role_id="expense",
                is_active=True,
                created_at=confirmation,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentCandidateRecord(
                id="midweek-candidate",
                account_id="current-1",
                recurring_series_id="midweek-series",
                merchant_group="synthetic weekly bill",
                currency="GBP",
                direction="outflow",
                financial_role_id="expense",
                expected_amount=Decimal("-25.00"),
                frequency="weekly",
                interval_days=7,
                next_expected_date=date(2026, 1, 15),
                confidence=Decimal("0.9"),
                covered_missed_count=0,
                status="confirmed",
                detected_at=confirmation,
                evidence_as_of_date=evidence.transaction_date,
                knowledge_cutoff_at=confirmation,
                reviewed_at=confirmation,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentMemberRecord(
                candidate_id="midweek-candidate",
                verified_transaction_id=evidence.id,
                identified_at=confirmation,
            )
        )

    by_week = {
        item.week_start: item
        for item in build_forecast_dataset(
            factory, plan=_plan(start, end)
        ).weekly_targets
    }
    assert by_week[date(2026, 1, 12)].known_recurring_outflow == Decimal("0.00")
    assert by_week[date(2026, 1, 19)].known_recurring_outflow == Decimal("25.00")


def test_later_backdated_member_does_not_reanchor_confirmed_recurrence(
    factory: sessionmaker[Session],
) -> None:
    start = date(2026, 1, 5)
    end = date(2026, 4, 5)
    coverage_known_at = datetime(2026, 1, 4, 12, tzinfo=UTC)
    original_known_at = datetime(2026, 2, 2, 12, tzinfo=UTC)
    review_at = datetime(2026, 2, 10, 12, tzinfo=UTC)
    later_identified_at = datetime(2026, 3, 1, 12, tzinfo=UTC)
    knowledge_cutoff_at = datetime(2026, 4, 10, 12, tzinfo=UTC)
    with session_scope(factory) as session:
        coverage = _add_coverage(
            session,
            "immutable-anchor-coverage",
            start=start,
            end=end,
        )
        context = session.get(ImportContextRecord, coverage.import_context_id)
        coverage_batch = session.get(ImportBatchRecord, "immutable-anchor-coverage")
        assert context is not None
        assert coverage_batch is not None
        context.created_at = coverage_known_at
        coverage_batch.imported_at = coverage_known_at

        original = _add_transaction(
            session,
            "original-month-end-member",
            transaction_date=date(2026, 1, 31),
            amount="-25.00",
            role=FinancialRole.EXPENSE,
        )
        backdated = _add_transaction(
            session,
            "later-backdated-member",
            transaction_date=date(2026, 1, 15),
            amount="-25.00",
            role=FinancialRole.EXPENSE,
        )
        for transaction, available_at in (
            (original, original_known_at),
            (backdated, datetime(2026, 1, 16, 12, tzinfo=UTC)),
        ):
            transaction.verified_at = available_at
            raw = session.get(RawTransactionRecord, transaction.raw_transaction_id)
            batch = session.get(ImportBatchRecord, f"batch-{transaction.id}")
            assert raw is not None
            assert batch is not None
            raw.created_at = available_at
            batch.imported_at = available_at
            _confirm_role(
                session,
                transaction.id,
                FinancialRole.EXPENSE,
                changed_at=available_at,
            )

        session.add(
            RecurringSeriesRecord(
                id="immutable-anchor-series",
                account_id="current-1",
                merchant_pattern="synthetic month end",
                expected_amount=Decimal("-25.00"),
                interval_days=30,
                financial_role_id="expense",
                is_active=True,
                created_at=review_at,
            )
        )
        session.flush()
        session.add(
            RecurringPaymentCandidateRecord(
                id="immutable-anchor-candidate",
                account_id="current-1",
                recurring_series_id="immutable-anchor-series",
                merchant_group="synthetic month end",
                currency="GBP",
                direction="outflow",
                financial_role_id="expense",
                expected_amount=Decimal("-25.00"),
                frequency="monthly",
                interval_days=30,
                next_expected_date=date(2026, 2, 28),
                confidence=Decimal("0.9"),
                covered_missed_count=0,
                status="confirmed",
                detected_at=original_known_at,
                evidence_as_of_date=original.transaction_date,
                knowledge_cutoff_at=original_known_at,
                reviewed_at=review_at,
            )
        )
        session.flush()
        session.add_all(
            [
                RecurringPaymentMemberRecord(
                    candidate_id="immutable-anchor-candidate",
                    verified_transaction_id=original.id,
                    identified_at=original_known_at,
                ),
                RecurringPaymentMemberRecord(
                    candidate_id="immutable-anchor-candidate",
                    verified_transaction_id=backdated.id,
                    identified_at=later_identified_at,
                ),
            ]
        )

    plan = _plan(start, end).model_copy(
        update={"knowledge_cutoff_at": knowledge_cutoff_at}
    )
    dataset = build_forecast_dataset(factory, plan=plan)
    daily = {item.observation_date: item for item in dataset.daily_calendar}
    weekly = {item.week_start: item for item in dataset.weekly_targets}

    assert daily[backdated.transaction_date].discretionary_spending == Decimal("0.00")
    assert daily[backdated.transaction_date].transaction_count == 0
    assert daily[backdated.transaction_date].known_at == later_identified_at
    assert weekly[date(2026, 2, 23)].known_recurring_outflow == Decimal("25.00")


def test_future_recurrence_review_and_recurring_income_do_not_change_expenses(
    factory: sessionmaker[Session],
) -> None:
    start, end = _covered_history(factory, weeks=12)
    future = NOW + timedelta(seconds=1)
    with session_scope(factory) as session:
        transaction = _add_transaction(
            session,
            "future-recurring-member",
            transaction_date=start,
            amount="-25.00",
            role=FinancialRole.EXPENSE,
        )
        _confirm_role(session, transaction.id, FinancialRole.EXPENSE)
        session.add_all(
            [
                RecurringSeriesRecord(
                    id="future-series",
                    account_id="current-1",
                    merchant_pattern="synthetic future expense",
                    expected_amount=Decimal("-25.00"),
                    interval_days=7,
                    financial_role_id="expense",
                    is_active=True,
                    created_at=future,
                ),
                RecurringSeriesRecord(
                    id="income-series",
                    account_id="current-1",
                    merchant_pattern="synthetic income",
                    expected_amount=Decimal("100.00"),
                    interval_days=7,
                    financial_role_id="income",
                    is_active=True,
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                RecurringPaymentCandidateRecord(
                    id="future-candidate",
                    account_id="current-1",
                    recurring_series_id="future-series",
                    merchant_group="synthetic future expense",
                    currency="GBP",
                    direction="outflow",
                    financial_role_id="expense",
                    expected_amount=Decimal("-25.00"),
                    frequency="weekly",
                    interval_days=7,
                    next_expected_date=start + timedelta(weeks=1),
                    confidence=Decimal("0.9"),
                    covered_missed_count=0,
                    status="confirmed",
                    detected_at=future,
                    evidence_as_of_date=start,
                    knowledge_cutoff_at=future,
                    reviewed_at=future,
                ),
                RecurringPaymentCandidateRecord(
                    id="income-candidate",
                    account_id="current-1",
                    recurring_series_id="income-series",
                    merchant_group="synthetic income",
                    currency="GBP",
                    direction="inflow",
                    financial_role_id="income",
                    expected_amount=Decimal("100.00"),
                    frequency="weekly",
                    interval_days=7,
                    next_expected_date=start + timedelta(weeks=1),
                    confidence=Decimal("0.9"),
                    covered_missed_count=0,
                    status="confirmed",
                    detected_at=NOW,
                    evidence_as_of_date=start,
                    knowledge_cutoff_at=NOW,
                    reviewed_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add(
            RecurringPaymentMemberRecord(
                candidate_id="future-candidate",
                verified_transaction_id=transaction.id,
                identified_at=future,
            )
        )
    dataset = build_forecast_dataset(factory, plan=_plan(start, end))
    assert dataset.weekly_targets[0].discretionary_spending == Decimal("95.00")
    assert all(item.known_recurring_outflow == 0 for item in dataset.weekly_targets)


def test_baselines_use_final_chronological_test_and_expanding_training() -> None:
    first = date(2025, 1, 6)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(100 + index),
            known_recurring_outflow=Decimal("0"),
            known_at=_known_after_week(first + timedelta(weeks=index)),
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
    assert seasonal.predictions == tuple(Decimal(value) for value in range(108, 112))
    recent = next(
        item
        for item in evaluation.metrics
        if item.baseline is ForecastBaselineName.RECENT_ROLLING_MEAN
    )
    assert recent.predictions == (
        Decimal("157.5"),
        Decimal("158.5"),
        Decimal("159.5"),
        Decimal("160.5"),
    )
    assert {item.baseline for item in evaluation.expanding_metrics} == set(
        ForecastBaselineName
    )
    assert all(item.mae >= 0 and item.rmse >= 0 for item in evaluation.metrics)


def test_baselines_and_folds_use_only_targets_available_at_each_origin() -> None:
    first = date(2025, 1, 6)
    final_test_week = first + timedelta(weeks=60)
    late_week = first + timedelta(weeks=8)
    late_known_at = datetime.combine(
        final_test_week + timedelta(days=2), time.min, tzinfo=UTC
    )
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(100 + index),
            known_recurring_outflow=Decimal("0"),
            known_at=(
                late_known_at
                if index == 8
                else _known_after_week(first + timedelta(weeks=index))
            ),
        )
        for index in range(61)
    )
    period_end = targets[-1].week_end
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic-profile",
        account_ids=("synthetic-account",),
        period=DateRange(start_date=first, end_date=period_end),
        knowledge_cutoff_at=datetime.combine(period_end, time.max, tzinfo=UTC),
    )
    dataset = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets,
        feature_rows=build_forecast_feature_rows(targets, plan.payday_days),
    )

    evaluation = evaluate_forecast_baselines(
        dataset, initial_training_weeks=5, final_test_weeks=1
    )

    assert late_week not in evaluation.final_training_week_starts
    assert all(
        late_week not in fold.training_week_starts
        for fold in evaluation.expanding_folds
        if fold.test_week_starts[0] < late_known_at.date()
    )
    expected_historical = sum(
        (
            target.discretionary_spending
            for target in targets[:-1]
            if target.week_start != late_week
        ),
        start=Decimal("0"),
    ) / Decimal(59)
    metrics = {item.baseline: item for item in evaluation.metrics}
    assert metrics[ForecastBaselineName.HISTORICAL_MEAN].predictions == (
        expected_historical,
    )
    assert metrics[ForecastBaselineName.SEASONAL_NAIVE].predictions == (
        expected_historical,
    )
    assert metrics[ForecastBaselineName.RECENT_ROLLING_MEAN].predictions == (
        Decimal("157.5"),
    )


def test_final_evaluation_rejects_a_horizon_crossing_unknown_weeks() -> None:
    first = date(2026, 1, 5)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(index),
            known_recurring_outflow=Decimal("0"),
            known_at=_known_after_week(first + timedelta(weeks=index)),
        )
        for index in range(22)
        if index != 12
    )
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic",
        account_ids=("synthetic",),
        period=DateRange(
            start_date=first,
            end_date=first + timedelta(weeks=22, days=-1),
        ),
        knowledge_cutoff_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    rows = build_forecast_feature_rows(targets, plan.payday_days)
    dataset = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets,
        feature_rows=rows,
    )
    with pytest.raises(ForecastingDataError, match="must be consecutive"):
        evaluate_forecast_baselines(
            dataset,
            initial_training_weeks=1,
            final_test_weeks=2,
        )


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
            known_at=datetime.combine(date(2026, 1, 1), time.max, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        DailyForecastObservation(
            observation_date=date(2026, 1, 1),
            status=ForecastDayStatus.COVERED,
            discretionary_spending=Decimal("0"),
            transaction_count=0,
            known_at=datetime(2026, 1, 1),
        )
    with pytest.raises(ValidationError):
        DailyForecastObservation(
            observation_date=date(2026, 1, 2),
            status=ForecastDayStatus.COVERED,
            discretionary_spending=Decimal("0"),
            transaction_count=0,
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        DailyForecastObservation(
            observation_date=date(2026, 1, 1),
            status=ForecastDayStatus.COVERED,
            discretionary_spending=Decimal("0"),
            transaction_count=0,
        )
    with pytest.raises(ValidationError):
        DailyForecastObservation(
            observation_date=date(2026, 1, 1),
            status=ForecastDayStatus.COVERED,
            discretionary_spending=Decimal("0"),
            transaction_count=0,
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    observation = DailyForecastObservation(
        observation_date=date(2026, 1, 1),
        status=ForecastDayStatus.COVERED,
        discretionary_spending=Decimal("0"),
        transaction_count=0,
        known_at=datetime.combine(date(2026, 1, 1), time.max, tzinfo=UTC),
    )
    assert observation.known_at == datetime.combine(
        date(2026, 1, 1), time.max, tzinfo=UTC
    )
    with pytest.raises(ValidationError):
        WeeklyForecastTarget(
            week_start=date(2026, 1, 6),
            week_end=date(2026, 1, 12),
            discretionary_spending=Decimal("1"),
            known_recurring_outflow=Decimal("0"),
            known_at=datetime(2026, 1, 12, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        WeeklyForecastTarget(
            week_start=date(2026, 1, 5),
            week_end=date(2026, 1, 11),
            discretionary_spending=Decimal("-1"),
            known_recurring_outflow=Decimal("0"),
            known_at=datetime(2026, 1, 11, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        WeeklyForecastTarget(
            week_start=date(2026, 1, 5),
            week_end=date(2026, 1, 11),
            discretionary_spending=Decimal("1"),
            known_recurring_outflow=Decimal("0"),
            known_at=datetime(2026, 1, 11),
        )
    with pytest.raises(ValidationError):
        WeeklyForecastTarget(
            week_start=date(2026, 1, 5),
            week_end=date(2026, 1, 11),
            discretionary_spending=Decimal("1"),
            known_recurring_outflow=Decimal("0"),
            known_at=datetime(2026, 1, 10, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        ExpandingWindowFold(
            training_week_starts=(date(2026, 2, 1),),
            test_week_starts=(date(2026, 1, 1),),
        )


def test_point_in_time_contracts_fail_closed() -> None:
    first = date(2026, 1, 5)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(index + 1),
            known_recurring_outflow=Decimal("0"),
            known_at=_known_after_week(first + timedelta(weeks=index)),
        )
        for index in range(11)
    )
    cutoff = datetime(2026, 4, 30, tzinfo=UTC)
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic",
        account_ids=("synthetic",),
        period=DateRange(
            start_date=first, end_date=first + timedelta(weeks=11, days=-1)
        ),
        knowledge_cutoff_at=cutoff,
    )
    rows = build_forecast_feature_rows(targets, plan.payday_days)
    valid = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets,
        feature_rows=rows,
        next_recurring_outflow=RecurringOutflowProjection(
            week_start=targets[-1].week_start + timedelta(weeks=1),
            amount=Decimal("0"),
            known_at=targets[-1].known_at,
        ),
    )
    row_payload = rows[0].model_dump()
    for update in (
        {"forecast_origin_at": datetime.combine(rows[0].week_start, time.min)},
        {
            "week_start": rows[0].week_start + timedelta(days=1),
            "forecast_origin_at": rows[0].forecast_origin_at + timedelta(days=1),
        },
        {"target_known_at": datetime.combine(targets[8].week_end, time.max)},
        {
            "target_known_at": datetime.combine(
                targets[8].week_end - timedelta(days=1), time.max, tzinfo=UTC
            )
        },
    ):
        with pytest.raises(ValidationError):
            ForecastFeatureRow.model_validate({**row_payload, **update})

    with pytest.raises(ValidationError):
        ForecastDataset(
            plan=plan,
            daily_calendar=(),
            weekly_targets=(
                targets[-1].model_copy(
                    update={"known_at": cutoff + timedelta(seconds=1)}
                ),
            ),
            feature_rows=(),
        )
    with pytest.raises(ValidationError):
        ForecastDataset(
            plan=plan,
            daily_calendar=(),
            weekly_targets=targets,
            feature_rows=(
                rows[0].model_copy(
                    update={"target_known_at": rows[0].target_known_at + timedelta(1)}
                ),
                *rows[1:],
            ),
        )

    for projection in (
        {
            "week_start": targets[-1].week_start + timedelta(weeks=1, days=1),
            "amount": Decimal("0"),
            "known_at": targets[-1].known_at,
        },
        {
            "week_start": targets[-1].week_start + timedelta(weeks=1),
            "amount": Decimal("0"),
            "known_at": datetime(2026, 4, 1),
        },
    ):
        with pytest.raises(ValidationError):
            RecurringOutflowProjection.model_validate(projection)
    with pytest.raises(ValidationError):
        ForecastDataset(
            plan=plan,
            daily_calendar=(),
            weekly_targets=targets,
            feature_rows=rows,
            next_recurring_outflow=RecurringOutflowProjection(
                week_start=targets[-1].week_start + timedelta(weeks=2),
                amount=Decimal("0"),
                known_at=targets[-1].known_at,
            ),
        )
    with pytest.raises(ValidationError):
        ForecastDataset(
            plan=plan,
            daily_calendar=(),
            weekly_targets=targets,
            feature_rows=rows,
            next_recurring_outflow=RecurringOutflowProjection(
                week_start=targets[-1].week_start + timedelta(weeks=1),
                amount=Decimal("0"),
                known_at=cutoff + timedelta(seconds=1),
            ),
        )

    forged = valid.model_copy(
        update={
            "feature_rows": (
                rows[0].model_copy(update={"lag_1": Decimal("999")}),
                *rows[1:],
            )
        }
    )
    with pytest.raises(ForecastingDataError) as exc_info:
        evaluate_forecast_baselines(
            forged, initial_training_weeks=1, final_test_weeks=1
        )
    assert exc_info.value.code is ForecastingDataErrorCode.INVALID_EVALUATION_POLICY

    for invalid in (
        valid.model_copy(update={"weekly_targets": tuple(reversed(targets))}),
        valid.model_copy(update={"feature_rows": tuple(reversed(rows))}),
        valid.model_copy(
            update={
                "feature_rows": (
                    rows[0].model_copy(update={"target": Decimal("999")}),
                    *rows[1:],
                )
            }
        ),
    ):
        with pytest.raises(ForecastingDataError) as exc_info:
            validate_forecast_dataset(invalid)
        assert exc_info.value.code is ForecastingDataErrorCode.INVALID_EVALUATION_POLICY


def test_baseline_evaluation_rejects_no_point_in_time_validation_fold() -> None:
    first = date(2026, 1, 5)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(index + 1),
            known_recurring_outflow=Decimal("0"),
            known_at=_known_after_week(first + timedelta(weeks=index)),
        )
        for index in range(11)
    )
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic",
        account_ids=("synthetic",),
        period=DateRange(
            start_date=first, end_date=first + timedelta(weeks=11, days=-1)
        ),
        knowledge_cutoff_at=datetime(2026, 4, 30, tzinfo=UTC),
    )
    rows = build_forecast_feature_rows(targets, plan.payday_days)
    late_time = rows[1].forecast_origin_at
    updated_targets = tuple(
        item.model_copy(update={"known_at": late_time}) if index == 8 else item
        for index, item in enumerate(targets)
    )
    updated_rows = build_forecast_feature_rows(updated_targets, plan.payday_days)
    dataset = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=updated_targets,
        feature_rows=updated_rows,
    )
    with pytest.raises(ForecastingDataError) as exc_info:
        evaluate_forecast_baselines(
            dataset, initial_training_weeks=1, final_test_weeks=1
        )
    assert exc_info.value.code is ForecastingDataErrorCode.TOO_FEW_COMPLETE_WEEKS


def test_baseline_evaluation_rejects_when_no_prior_row_was_known() -> None:
    first = date(2025, 1, 6)
    late_origin = datetime.combine(first + timedelta(weeks=18), time.min, tzinfo=UTC)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(index + 1),
            known_recurring_outflow=Decimal("0"),
            known_at=(
                late_origin
                if index == 8
                else _known_after_week(first + timedelta(weeks=index))
            ),
        )
        for index in range(19)
    )
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic",
        account_ids=("synthetic",),
        period=DateRange(start_date=first, end_date=targets[-1].week_end),
        knowledge_cutoff_at=late_origin + timedelta(days=7),
    )
    rows = build_forecast_feature_rows(targets, plan.payday_days)
    assert tuple(item.week_start for item in rows) == (
        targets[8].week_start,
        targets[17].week_start,
        targets[18].week_start,
    )
    dataset = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets,
        feature_rows=rows,
    )
    with pytest.raises(ForecastingDataError) as exc_info:
        evaluate_forecast_baselines(
            dataset, initial_training_weeks=1, final_test_weeks=1
        )
    assert exc_info.value.code is ForecastingDataErrorCode.TOO_FEW_COMPLETE_WEEKS


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
    assert forecast_service._advance_recurrence(date(2026, 1, 31), "monthly") == date(
        2026, 2, 28
    )
    assert forecast_service._advance_recurrence(date(2026, 1, 1), "weekly") == date(
        2026, 1, 8
    )
    assert forecast_service._advance_recurrence(
        date(2026, 1, 1), "fortnightly"
    ) == date(2026, 1, 15)
    assert forecast_service._advance_recurrence(date(2026, 1, 30), "quarterly") == date(
        2026, 4, 30
    )
    assert forecast_service._advance_recurrence(date(2024, 2, 29), "annual") == date(
        2025, 2, 28
    )

    first = date(2026, 1, 5)
    targets = tuple(
        WeeklyForecastTarget(
            week_start=first + timedelta(weeks=index),
            week_end=first + timedelta(weeks=index, days=6),
            discretionary_spending=Decimal(index + 1),
            known_recurring_outflow=Decimal("0"),
            known_at=_known_after_week(first + timedelta(weeks=index)),
        )
        for index in range(11)
    )
    plan = ForecastDatasetPlan(
        user_profile_id="synthetic",
        account_ids=("synthetic",),
        period=DateRange(
            start_date=first, end_date=first + timedelta(weeks=11, days=-1)
        ),
        knowledge_cutoff_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    insufficient = ForecastDataset(
        plan=plan,
        daily_calendar=(),
        weekly_targets=targets[:-1],
        feature_rows=build_forecast_feature_rows(targets[:-1], plan.payday_days),
    )
    with pytest.raises(ForecastingDataError) as exc_info:
        evaluate_forecast_baselines(
            insufficient, initial_training_weeks=1, final_test_weeks=1
        )
    assert exc_info.value.code is ForecastingDataErrorCode.TOO_FEW_COMPLETE_WEEKS
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
    for arguments in (
        ["forecast-demo", "--weeks", "13", "--test-weeks", "4"],
        [
            "forecast-demo",
            "--weeks",
            "13",
            "--test-weeks",
            "3",
            "--gap-week",
            "6",
        ],
        ["forecast-demo", "--test-weeks", "0"],
        ["forecast-demo", "--gap-week", "20"],
    ):
        monkeypatch.setattr("sys.argv", arguments)
        with pytest.raises(SystemExit):
            forecast_demo.main()
