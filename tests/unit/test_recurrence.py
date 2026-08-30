"""Tests for coverage-aware recurring-payment detection and review."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_categorisation import _add_transaction, _seed_foundation

import cashflow_ai.recurrence.service as recurrence_service
from cashflow_ai.analytics import compute_cash_flow_analytics
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
    StatementCoverageRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.recurrence import (
    RecurrenceServiceError,
    RecurrenceServiceErrorCode,
    detect_recurring_payments,
    review_recurring_payment,
)
from cashflow_ai.schemas import (
    AnalyticsScope,
    AnalyticsView,
    Currency,
    DateRange,
    Direction,
    FinancialRole,
    RecurrenceDetectionPolicy,
    RecurrenceFrequency,
    RecurrenceReview,
    RecurrenceReviewAction,
    RecurrenceStatus,
    RecurringPaymentCandidate,
)

NOW = datetime(2026, 12, 31, 12, tzinfo=UTC)
POLICY = RecurrenceDetectionPolicy(
    minimum_occurrences=2,
    maximum_amount_variation=Decimal("2.00"),
    maximum_interval_variation_days=3,
    maximum_skipped_occurrences=2,
    minimum_confidence=0,
)


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> sessionmaker[Session]:
    monkeypatch.setattr(recurrence_service, "utc_now", lambda: NOW)
    engine: Engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    _seed_foundation(result)
    return result


def _series(
    factory: sessionmaker[Session], merchant: str, dates: tuple[date, ...]
) -> None:
    with session_scope(factory) as session:
        for index, transaction_date in enumerate(dates):
            transaction = _add_transaction(
                session,
                f"{merchant.lower().replace(' ', '-')}-{index}",
                merchant=merchant,
                amount=str(Decimal("-20.00") + Decimal(index) / 10),
                transaction_date=transaction_date,
            )
            _audit_role(session, transaction.id, FinancialRole.EXPENSE)


def _audit_role(
    session: Session,
    transaction_id: str,
    role: FinancialRole,
    *,
    changed_at: datetime = NOW,
) -> None:
    session.add(
        FinancialRoleAuditRecord(
            id=f"role-{transaction_id}",
            verified_transaction_id=transaction_id,
            previous_role_id="unknown",
            new_role_id=role.value,
            source="user_override",
            changed_at=changed_at,
        )
    )


def _coverage(
    factory: sessionmaker[Session],
    transaction_id: str,
    *,
    status: str = "complete",
    missing: list[dict[str, str]] | None = None,
) -> None:
    with session_scope(factory) as session:
        session.add(
            StatementCoverageRecord(
                id=f"coverage-{transaction_id}",
                import_context_id=f"context-{transaction_id}",
                statement_start_date=date(2026, 1, 1),
                statement_end_date=date(2026, 12, 31),
                coverage_status=status,
                missing_periods_json=missing or [],
            )
        )


@pytest.mark.parametrize(
    ("dates", "frequency"),
    [
        (
            (date(2026, 8, 1), date(2026, 8, 8), date(2026, 8, 15)),
            RecurrenceFrequency.WEEKLY,
        ),
        (
            (date(2026, 7, 1), date(2026, 7, 15), date(2026, 7, 29)),
            RecurrenceFrequency.FORTNIGHTLY,
        ),
        (
            (date(2026, 5, 31), date(2026, 6, 30), date(2026, 7, 31)),
            RecurrenceFrequency.MONTHLY,
        ),
        (
            (date(2026, 1, 1), date(2026, 4, 2), date(2026, 7, 2)),
            RecurrenceFrequency.QUARTERLY,
        ),
        ((date(2025, 8, 1), date(2026, 8, 1)), RecurrenceFrequency.ANNUAL),
    ],
)
def test_detects_supported_frequencies_with_normalised_merchant_groups(
    factory: sessionmaker[Session],
    dates: tuple[date, ...],
    frequency: RecurrenceFrequency,
) -> None:
    _series(factory, "Synthetic-Utility!", dates)
    result = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 15),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )
    assert len(result) == 1
    assert result[0].merchant_group == "synthetic utility"
    assert result[0].frequency is frequency
    assert result[0].status is RecurrenceStatus.PENDING
    assert result[0].next_expected_date > dates[-1]


def test_only_covered_expected_dates_count_as_missed_and_detection_is_idempotent(
    factory: sessionmaker[Session],
) -> None:
    dates = (date(2026, 8, 1), date(2026, 8, 8), date(2026, 8, 15))
    _series(factory, "Synthetic Weekly", dates)
    _coverage(
        factory,
        "synthetic-weekly-0",
        status="gapped",
        missing=[{"start_date": "2026-08-22", "end_date": "2026-08-29"}],
    )
    first = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 29),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )
    second = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 29),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )
    assert first == second
    assert first[0].covered_missed_count == 0
    assert first[0].next_expected_date == date(2026, 9, 5)
    with session_scope(factory) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(RecurringPaymentCandidateRecord)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(RecurringPaymentMemberRecord)
            )
            == 3
        )


def test_complete_coverage_counts_missed_payment_and_reduces_confidence(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory,
        "Synthetic Weekly",
        (date(2026, 8, 1), date(2026, 8, 8), date(2026, 8, 15)),
    )
    _coverage(factory, "synthetic-weekly-0")
    result = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 29),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )
    assert result[0].covered_missed_count == 2
    assert result[0].confidence < 0.7


def test_covered_dates_after_latest_occurrence_obey_skip_limit(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory,
        "Expired Monthly",
        (date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)),
    )
    _coverage(factory, "expired-monthly-0")

    permissive = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 6, 30),
        knowledge_cutoff_at=NOW,
        policy=POLICY.model_copy(update={"maximum_skipped_occurrences": 3}),
    )
    rejected = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 6, 30),
        knowledge_cutoff_at=NOW,
        policy=POLICY.model_copy(update={"maximum_skipped_occurrences": 0}),
    )

    assert permissive[0].covered_missed_count == 3
    assert permissive[0].next_expected_date == date(2026, 7, 31)
    assert rejected == ()


def test_noise_and_untrusted_groups_are_not_detected(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory, "Irregular", (date(2026, 1, 1), date(2026, 2, 20), date(2026, 8, 1))
    )
    _coverage(factory, "irregular-0")
    _series(factory, "Variable", (date(2026, 7, 1), date(2026, 8, 1)))
    with session_scope(factory) as session:
        item = session.get(RecurringSeriesRecord, "does-not-exist")
        assert item is None
        variable = session.get(VerifiedTransactionRecord, "variable-1")
        assert variable is not None
        variable.amount = Decimal("-99.00")
        # A same-day duplicate merchant cannot prove an interval.
        same_1 = _add_transaction(
            session, "same-1", merchant="Same Day", transaction_date=date(2026, 8, 1)
        )
        same_2 = _add_transaction(
            session, "same-2", merchant="Same Day", transaction_date=date(2026, 8, 1)
        )
        _audit_role(session, same_1.id, FinancialRole.EXPENSE)
        _audit_role(session, same_2.id, FinancialRole.EXPENSE)
        # Unknown roles are ineligible.
        _add_transaction(
            session,
            "unknown-1",
            merchant="Unknown Role",
            transaction_date=date(2026, 7, 1),
        )
        _add_transaction(
            session,
            "unknown-2",
            merchant="Unknown Role",
            transaction_date=date(2026, 8, 1),
        )
        for transaction_id in ("unknown-1", "unknown-2"):
            transaction = session.get(VerifiedTransactionRecord, transaction_id)
            assert transaction is not None
            transaction.financial_role_id = "unknown"
    result = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 31),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )
    assert all(
        item.merchant_group not in {"irregular", "same day", "unknown role"}
        for item in result
    )


def test_user_can_confirm_or_cancel_once(factory: sessionmaker[Session]) -> None:
    _series(
        factory, "Confirm Me", (date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1))
    )
    _series(
        factory, "Cancel Me", (date(2026, 6, 5), date(2026, 7, 5), date(2026, 8, 5))
    )
    candidates = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 10),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )
    confirmed = review_recurring_payment(
        factory,
        review=RecurrenceReview(
            user_profile_id="profile-1",
            candidate_id=candidates[1].candidate_id,
            action=RecurrenceReviewAction.CONFIRM,
            reviewed_at=NOW,
        ),
    )
    cancelled = review_recurring_payment(
        factory,
        review=RecurrenceReview(
            user_profile_id="profile-1",
            candidate_id=candidates[0].candidate_id,
            action=RecurrenceReviewAction.CANCEL,
            reviewed_at=NOW,
        ),
    )
    assert confirmed.status is RecurrenceStatus.CONFIRMED
    assert confirmed.recurring_series_id is not None
    assert cancelled.status is RecurrenceStatus.CANCELLED
    assert cancelled.recurring_series_id is None
    redetected = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 10),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )
    assert [item.status for item in redetected] == [RecurrenceStatus.CONFIRMED]
    with pytest.raises(RecurrenceServiceError) as exc_info:
        review_recurring_payment(
            factory,
            review=RecurrenceReview(
                user_profile_id="profile-1",
                candidate_id=candidates[0].candidate_id,
                action=RecurrenceReviewAction.CANCEL,
                reviewed_at=NOW,
            ),
        )
    assert exc_info.value.code is RecurrenceServiceErrorCode.CANDIDATE_ALREADY_REVIEWED


def test_only_confirmed_members_feed_recurring_expense_analytics(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory,
        "Analytics Series",
        (date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)),
    )
    _coverage(factory, "analytics-series-0")
    candidate = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 10),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )[0]
    scope = AnalyticsScope(
        user_profile_id="profile-1",
        account_ids=("current-1",),
        period=DateRange(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31)),
        view=AnalyticsView.ACCOUNT,
    )
    before = compute_cash_flow_analytics(factory, scope)
    assert before.spending_cadence is not None
    assert before.spending_cadence.recurring_count == 0
    review_recurring_payment(
        factory,
        review=RecurrenceReview(
            user_profile_id="profile-1",
            candidate_id=candidate.candidate_id,
            action=RecurrenceReviewAction.CONFIRM,
            reviewed_at=NOW,
        ),
    )
    after = compute_cash_flow_analytics(factory, scope)
    assert after.spending_cadence is not None
    assert after.spending_cadence.recurring_count == 1
    assert after.spending_cadence.recurring == Decimal("19.80")


def test_contracts_and_missing_scope_fail_closed(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(ValidationError):
        RecurrenceReview(
            user_profile_id="profile-1",
            candidate_id="candidate",
            action=RecurrenceReviewAction.CONFIRM,
            reviewed_at=datetime(2026, 8, 1),
        )
    with pytest.raises(ValidationError):
        RecurringPaymentCandidate(
            candidate_id="candidate",
            account_id="current-1",
            merchant_group="synthetic",
            currency=Currency.GBP,
            direction=Direction.OUTFLOW,
            financial_role=FinancialRole.EXPENSE,
            expected_amount=Decimal("-10"),
            frequency=RecurrenceFrequency.MONTHLY,
            interval_days=30,
            occurrence_dates=(date(2026, 8, 1), date(2026, 7, 1)),
            next_expected_date=date(2026, 9, 1),
            confidence=0.8,
            covered_missed_count=0,
            status=RecurrenceStatus.PENDING,
            evidence_as_of_date=date(2026, 8, 15),
            knowledge_cutoff_at=NOW,
        )
    with pytest.raises(ValidationError):
        RecurringPaymentCandidate(
            candidate_id="candidate",
            account_id="current-1",
            merchant_group="synthetic",
            currency=Currency.GBP,
            direction=Direction.OUTFLOW,
            financial_role=FinancialRole.EXPENSE,
            expected_amount=Decimal("-10"),
            frequency=RecurrenceFrequency.MONTHLY,
            interval_days=30,
            occurrence_dates=(date(2026, 7, 1), date(2026, 8, 1)),
            next_expected_date=date(2026, 8, 1),
            confidence=0.8,
            covered_missed_count=0,
            status=RecurrenceStatus.PENDING,
            evidence_as_of_date=date(2026, 8, 15),
            knowledge_cutoff_at=NOW,
        )
    with pytest.raises(RecurrenceServiceError) as exc_info:
        detect_recurring_payments(
            factory,
            user_profile_id="missing",
            as_of_date=date(2026, 8, 1),
            knowledge_cutoff_at=NOW,
            policy=POLICY,
        )
    assert exc_info.value.code is RecurrenceServiceErrorCode.PROFILE_NOT_FOUND
    with pytest.raises(RecurrenceServiceError) as exc_info:
        review_recurring_payment(
            factory,
            review=RecurrenceReview(
                user_profile_id="profile-1",
                candidate_id="missing",
                action=RecurrenceReviewAction.CONFIRM,
                reviewed_at=NOW,
            ),
        )
    assert exc_info.value.code is RecurrenceServiceErrorCode.CANDIDATE_NOT_FOUND


def test_candidate_contract_rejects_inconsistent_cutoff_dates() -> None:
    values = {
        "candidate_id": "candidate",
        "account_id": "current-1",
        "merchant_group": "synthetic",
        "currency": Currency.GBP,
        "direction": Direction.OUTFLOW,
        "financial_role": FinancialRole.EXPENSE,
        "expected_amount": Decimal("-10.00"),
        "frequency": RecurrenceFrequency.MONTHLY,
        "interval_days": 30,
        "occurrence_dates": (date(2026, 7, 1), date(2026, 8, 1)),
        "next_expected_date": date(2026, 9, 1),
        "confidence": 0.8,
        "covered_missed_count": 0,
        "status": RecurrenceStatus.PENDING,
        "evidence_as_of_date": date(2026, 8, 15),
        "knowledge_cutoff_at": NOW,
    }
    RecurringPaymentCandidate.model_validate(values)
    invalid_updates = (
        {"occurrence_dates": (date(2026, 8, 1), date(2026, 7, 1))},
        {"next_expected_date": date(2026, 8, 1)},
        {"knowledge_cutoff_at": datetime(2026, 8, 15)},
        {
            "next_expected_date": date(2026, 8, 15),
            "evidence_as_of_date": date(2026, 8, 15),
        },
        {
            "evidence_as_of_date": date(2027, 1, 1),
            "next_expected_date": date(2027, 2, 1),
        },
    )
    for update in invalid_updates:
        with pytest.raises(ValidationError):
            RecurringPaymentCandidate.model_validate(values | update)


def test_detector_edge_filters_and_coverage_subtraction(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        no_merchant = _add_transaction(
            session, "no-merchant", merchant=None, transaction_date=date(2026, 8, 1)
        )
        single = _add_transaction(
            session, "single", merchant="Single", transaction_date=date(2026, 8, 1)
        )
        _audit_role(session, no_merchant.id, FinancialRole.EXPENSE)
        _audit_role(session, single.id, FinancialRole.EXPENSE)
    assert (
        detect_recurring_payments(
            factory,
            user_profile_id="profile-1",
            as_of_date=date(2026, 8, 2),
            knowledge_cutoff_at=NOW,
            policy=POLICY,
        )
        == ()
    )
    _series(
        factory,
        "Low Confidence",
        (date(2026, 8, 1), date(2026, 8, 8), date(2026, 8, 15)),
    )
    assert (
        detect_recurring_payments(
            factory,
            user_profile_id="profile-1",
            as_of_date=date(2026, 8, 15),
            knowledge_cutoff_at=NOW,
            policy=POLICY.model_copy(update={"minimum_confidence": 1.0}),
        )
        == ()
    )
    candidate = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 15),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )[0]
    review_recurring_payment(
        factory,
        review=RecurrenceReview(
            user_profile_id="profile-1",
            candidate_id=candidate.candidate_id,
            action=RecurrenceReviewAction.CANCEL,
            reviewed_at=NOW,
        ),
    )
    assert (
        detect_recurring_payments(
            factory,
            user_profile_id="profile-1",
            as_of_date=date(2026, 8, 15),
            knowledge_cutoff_at=NOW,
            policy=POLICY,
        )
        == ()
    )
    _coverage(factory, "low-confidence-0", status="partial")
    with session_scope(factory) as session:
        assert (
            recurrence_service._known_ranges(
                session, "current-1", knowledge_cutoff_at=NOW
            )
            == ()
        )

    _coverage(
        factory,
        "single",
        status="gapped",
        missing=[
            {"start_date": "2025-12-01", "end_date": "2025-12-02"},
            {"start_date": "2026-01-01", "end_date": "2026-12-31"},
        ],
    )
    with session_scope(factory) as session:
        recurrence_service._known_ranges(session, "current-1", knowledge_cutoff_at=NOW)

    assert (
        detect_recurring_payments(
            factory,
            user_profile_id="profile-2",
            as_of_date=date(2026, 8, 15),
            knowledge_cutoff_at=NOW,
            policy=POLICY,
        )
        == ()
    )


def test_high_tolerance_schedule_still_projects_after_latest_occurrence(
    factory: sessionmaker[Session],
) -> None:
    _series(factory, "Sparse", (date(2026, 1, 1), date(2026, 3, 1)))
    result = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 3, 2),
        knowledge_cutoff_at=NOW,
        policy=POLICY.model_copy(update={"maximum_interval_variation_days": 31}),
    )
    assert result[0].frequency is RecurrenceFrequency.QUARTERLY
    assert result[0].next_expected_date == date(2026, 7, 1)


def test_irregular_zero_tolerance_schedule_is_not_a_recurrence(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory,
        "Irregular",
        (date(2026, 1, 1), date(2026, 1, 9), date(2026, 1, 20)),
    )
    assert (
        detect_recurring_payments(
            factory,
            user_profile_id="profile-1",
            as_of_date=date(2026, 1, 20),
            knowledge_cutoff_at=NOW,
            policy=POLICY.model_copy(update={"maximum_interval_variation_days": 0}),
        )
        == ()
    )


def test_uncovered_skipped_month_preserves_calendar_monthly_pattern(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory,
        "Gap Safe",
        (date(2026, 1, 31), date(2026, 3, 31), date(2026, 4, 30)),
    )
    _coverage(
        factory,
        "gap-safe-0",
        status="gapped",
        missing=[{"start_date": "2026-02-01", "end_date": "2026-02-28"}],
    )

    result = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 5, 1),
        knowledge_cutoff_at=NOW,
        policy=POLICY.model_copy(update={"maximum_skipped_occurrences": 0}),
    )

    assert result[0].frequency is RecurrenceFrequency.MONTHLY
    assert result[0].covered_missed_count == 0
    assert result[0].next_expected_date == date(2026, 5, 31)


def test_covered_skipped_month_counts_as_missed_and_can_reject_pattern(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory,
        "Covered Miss",
        (date(2026, 1, 31), date(2026, 3, 31), date(2026, 4, 30)),
    )
    _coverage(factory, "covered-miss-0")

    permissive = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 5, 1),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )
    rejected = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 5, 1),
        knowledge_cutoff_at=NOW,
        policy=POLICY.model_copy(update={"minimum_confidence": 0.8}),
    )
    rejected_by_skip_limit = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 5, 1),
        knowledge_cutoff_at=NOW,
        policy=POLICY.model_copy(update={"maximum_skipped_occurrences": 0}),
    )

    assert permissive[0].covered_missed_count == 1
    assert rejected == ()
    assert rejected_by_skip_limit == ()


@pytest.mark.parametrize(
    ("dates", "frequency", "next_date"),
    [
        (
            (date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)),
            RecurrenceFrequency.MONTHLY,
            date(2024, 4, 30),
        ),
        (
            (date(2024, 1, 31), date(2024, 4, 30), date(2024, 7, 31)),
            RecurrenceFrequency.QUARTERLY,
            date(2024, 10, 31),
        ),
        (
            (date(2024, 2, 29), date(2025, 2, 28)),
            RecurrenceFrequency.ANNUAL,
            date(2026, 2, 28),
        ),
    ],
)
def test_calendar_frequencies_preserve_month_end_and_leap_years(
    factory: sessionmaker[Session],
    dates: tuple[date, ...],
    frequency: RecurrenceFrequency,
    next_date: date,
) -> None:
    _series(factory, "Calendar Anchor", dates)

    result = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=dates[-1],
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )

    assert result[0].frequency is frequency
    assert result[0].next_expected_date == next_date


def test_confirmed_candidate_refreshes_when_the_next_occurrence_arrives(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory,
        "Lifecycle",
        (date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)),
    )
    candidate = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 2),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )[0]
    review = review_recurring_payment(
        factory,
        review=RecurrenceReview(
            user_profile_id="profile-1",
            candidate_id=candidate.candidate_id,
            action=RecurrenceReviewAction.CONFIRM,
            reviewed_at=NOW,
        ),
    )
    with session_scope(factory) as session:
        transaction = _add_transaction(
            session,
            "lifecycle-3",
            merchant="Lifecycle",
            amount="-20.30",
            transaction_date=date(2026, 9, 1),
        )
        _audit_role(session, transaction.id, FinancialRole.EXPENSE)

    refreshed = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 9, 2),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )[0]

    assert refreshed.status is RecurrenceStatus.CONFIRMED
    assert refreshed.occurrence_dates[-1] == date(2026, 9, 1)
    assert refreshed.next_expected_date == date(2026, 10, 1)
    with session_scope(factory) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RecurringPaymentMemberRecord)
                .where(
                    RecurringPaymentMemberRecord.candidate_id == candidate.candidate_id
                )
            )
            == 4
        )
        series = session.get(RecurringSeriesRecord, review.recurring_series_id)
        assert series is not None
        assert series.expected_amount == Decimal("-19.90")


def test_full_identity_keeps_same_merchant_expense_and_refund_series_separate(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory,
        "Shared Merchant",
        (date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)),
    )
    with session_scope(factory) as session:
        for index, transaction_date in enumerate(
            (date(2026, 6, 5), date(2026, 7, 5), date(2026, 8, 5))
        ):
            transaction = _add_transaction(
                session,
                f"shared-refund-{index}",
                merchant="Shared Merchant",
                amount="20.00",
                transaction_date=transaction_date,
                role=FinancialRole.REFUND,
            )
            _audit_role(session, transaction.id, FinancialRole.REFUND)

    candidates = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 10),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )

    assert {(item.direction, item.financial_role) for item in candidates} == {
        (Direction.OUTFLOW, FinancialRole.EXPENSE),
        (Direction.INFLOW, FinancialRole.REFUND),
    }
    assert all(item.currency is Currency.GBP for item in candidates)
    refund = next(
        item for item in candidates if item.financial_role is FinancialRole.REFUND
    )
    with session_scope(factory) as session:
        for transaction_id in (
            "shared-refund-0",
            "shared-refund-1",
            "shared-refund-2",
        ):
            stored_transaction = session.get(VerifiedTransactionRecord, transaction_id)
            assert stored_transaction is not None
            stored_transaction.financial_role_id = FinancialRole.INCOME.value
    review = review_recurring_payment(
        factory,
        review=RecurrenceReview(
            user_profile_id="profile-1",
            candidate_id=refund.candidate_id,
            action=RecurrenceReviewAction.CONFIRM,
            reviewed_at=NOW,
        ),
    )
    with session_scope(factory) as session:
        series = session.get(RecurringSeriesRecord, review.recurring_series_id)
        assert series is not None
        assert series.financial_role_id == FinancialRole.REFUND.value


def test_cutoff_filters_future_evidence_and_preserves_confirmed_csv_rows(
    factory: sessionmaker[Session],
) -> None:
    cutoff = datetime(2026, 11, 30, 23, 59, tzinfo=UTC)
    evidence_time = datetime(2026, 10, 1, tzinfo=UTC)
    future_time = datetime(2026, 12, 1, tzinfo=UTC)
    _series(
        factory,
        "Cutoff Weekly",
        (date(2026, 8, 1), date(2026, 8, 8), date(2026, 8, 15)),
    )
    _coverage(factory, "cutoff-weekly-0")
    with session_scope(factory) as session:
        for index in range(3):
            transaction = session.get(
                VerifiedTransactionRecord, f"cutoff-weekly-{index}"
            )
            batch = session.get(ImportBatchRecord, f"batch-cutoff-weekly-{index}")
            audit = session.get(FinancialRoleAuditRecord, f"role-cutoff-weekly-{index}")
            assert transaction is not None
            assert batch is not None
            assert audit is not None
            transaction.verified_at = evidence_time
            batch.imported_at = evidence_time
            batch.verification_status = "verified" if index == 0 else "needs_review"
            audit.changed_at = evidence_time
        context = session.get(ImportContextRecord, "context-cutoff-weekly-0")
        assert context is not None
        context.created_at = future_time
        future_transaction = session.get(VerifiedTransactionRecord, "cutoff-weekly-2")
        assert future_transaction is not None
        future_transaction.verified_at = future_time

    result = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 29),
        knowledge_cutoff_at=cutoff,
        policy=POLICY,
    )

    assert result[0].occurrence_dates == (date(2026, 8, 1), date(2026, 8, 8))
    assert result[0].covered_missed_count == 0


def test_future_raw_creation_time_is_ineligible_at_cutoff(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory,
        "Future Raw Weekly",
        (date(2026, 8, 1), date(2026, 8, 8)),
    )
    with session_scope(factory) as session:
        raw = session.get(RawTransactionRecord, "raw-future-raw-weekly-1")
        assert raw is not None
        raw.created_at = datetime(2027, 1, 1, tzinfo=UTC)

    result = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 15),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )

    assert result == ()


def test_cross_account_batch_lineage_is_ineligible(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory,
        "Cross Account Weekly",
        (date(2026, 8, 1), date(2026, 8, 8)),
    )
    with session_scope(factory) as session:
        batch = session.get(ImportBatchRecord, "batch-cross-account-weekly-1")
        assert batch is not None
        batch.account_id = "savings-1"

    result = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 15),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )

    assert result == ()


def test_unverified_document_and_future_role_decisions_are_ineligible(
    factory: sessionmaker[Session],
) -> None:
    _series(factory, "Untrusted PDF", (date(2026, 7, 1), date(2026, 8, 1)))
    _series(factory, "Trusted PDF", (date(2026, 7, 3), date(2026, 8, 3)))
    _series(factory, "Future Role", (date(2026, 7, 2), date(2026, 8, 2)))
    cutoff = datetime(2026, 12, 1, tzinfo=UTC)
    evidence_time = datetime(2026, 10, 1, tzinfo=UTC)
    with session_scope(factory) as session:
        for prefix in ("untrusted-pdf", "trusted-pdf", "future-role"):
            for index in range(2):
                transaction = session.get(
                    VerifiedTransactionRecord, f"{prefix}-{index}"
                )
                batch = session.get(ImportBatchRecord, f"batch-{prefix}-{index}")
                assert transaction is not None
                assert batch is not None
                transaction.verified_at = evidence_time
                batch.imported_at = evidence_time
        for index in range(2):
            pdf_batch = session.get(ImportBatchRecord, f"batch-untrusted-pdf-{index}")
            pdf_raw = session.get(RawTransactionRecord, f"raw-untrusted-pdf-{index}")
            pdf_audit = session.get(
                FinancialRoleAuditRecord, f"role-untrusted-pdf-{index}"
            )
            future_audit = session.get(
                FinancialRoleAuditRecord, f"role-future-role-{index}"
            )
            assert pdf_batch is not None
            assert pdf_raw is not None
            assert pdf_audit is not None
            assert future_audit is not None
            pdf_batch.source_type = "digital_pdf"
            pdf_raw.source_type = "digital_pdf"
            pdf_raw.source_row_number = None
            pdf_raw.page_number = 1
            pdf_raw.page_record_number = index + 1
            pdf_batch.verification_status = "needs_review"
            pdf_audit.changed_at = evidence_time
            future_audit.changed_at = NOW
            trusted_batch = session.get(ImportBatchRecord, f"batch-trusted-pdf-{index}")
            trusted_raw = session.get(RawTransactionRecord, f"raw-trusted-pdf-{index}")
            trusted_audit = session.get(
                FinancialRoleAuditRecord, f"role-trusted-pdf-{index}"
            )
            assert trusted_batch is not None
            assert trusted_raw is not None
            assert trusted_audit is not None
            trusted_batch.source_type = "digital_pdf"
            trusted_raw.source_type = "digital_pdf"
            trusted_raw.source_row_number = None
            trusted_raw.page_number = 1
            trusted_raw.page_record_number = index + 1
            trusted_batch.verification_status = "verified"
            trusted_audit.changed_at = evidence_time

    result = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 10),
        knowledge_cutoff_at=cutoff,
        policy=POLICY,
    )
    assert tuple(item.merchant_group for item in result) == ("trusted pdf",)


def test_cutoff_and_review_chronology_fail_closed(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RecurrenceServiceError) as exc_info:
        detect_recurring_payments(
            factory,
            user_profile_id="profile-1",
            as_of_date=date(2026, 8, 1),
            knowledge_cutoff_at=datetime(2026, 8, 1),
            policy=POLICY,
        )
    assert exc_info.value.code is RecurrenceServiceErrorCode.INVALID_KNOWLEDGE_CUTOFF
    with pytest.raises(RecurrenceServiceError) as exc_info:
        detect_recurring_payments(
            factory,
            user_profile_id="profile-1",
            as_of_date=date(2026, 8, 1),
            knowledge_cutoff_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
            policy=POLICY,
        )
    assert exc_info.value.code is RecurrenceServiceErrorCode.INVALID_KNOWLEDGE_CUTOFF
    with pytest.raises(RecurrenceServiceError) as exc_info:
        detect_recurring_payments(
            factory,
            user_profile_id="profile-1",
            as_of_date=date(2026, 8, 2),
            knowledge_cutoff_at=datetime(2026, 8, 1, tzinfo=UTC),
            policy=POLICY,
        )
    assert exc_info.value.code is RecurrenceServiceErrorCode.INVALID_KNOWLEDGE_CUTOFF
    assert (
        detect_recurring_payments(
            factory,
            user_profile_id="profile-2",
            as_of_date=date(2026, 8, 1),
            knowledge_cutoff_at=datetime(2026, 8, 2, tzinfo=UTC),
            policy=POLICY,
        )
        == ()
    )

    _series(factory, "Review Clock", (date(2026, 7, 1), date(2026, 8, 1)))
    candidate = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 2),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )[0]
    with pytest.raises(RecurrenceServiceError) as exc_info:
        review_recurring_payment(
            factory,
            review=RecurrenceReview(
                user_profile_id="profile-1",
                candidate_id=candidate.candidate_id,
                action=RecurrenceReviewAction.CONFIRM,
                reviewed_at=datetime(2027, 1, 1, tzinfo=UTC),
            ),
        )
    assert exc_info.value.code is RecurrenceServiceErrorCode.INVALID_REVIEW_TIMESTAMP

    monkeypatch.setattr(
        recurrence_service,
        "utc_now",
        lambda: datetime(2026, 12, 30, tzinfo=UTC),
    )
    with pytest.raises(RecurrenceServiceError) as exc_info:
        review_recurring_payment(
            factory,
            review=RecurrenceReview(
                user_profile_id="profile-1",
                candidate_id=candidate.candidate_id,
                action=RecurrenceReviewAction.CONFIRM,
                reviewed_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
    assert exc_info.value.code is RecurrenceServiceErrorCode.INVALID_REVIEW_TIMESTAMP

    monkeypatch.setattr(recurrence_service, "utc_now", lambda: datetime(2026, 1, 1))
    with pytest.raises(RecurrenceServiceError) as exc_info:
        review_recurring_payment(
            factory,
            review=RecurrenceReview(
                user_profile_id="profile-1",
                candidate_id=candidate.candidate_id,
                action=RecurrenceReviewAction.CONFIRM,
                reviewed_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
    assert exc_info.value.code is RecurrenceServiceErrorCode.INVALID_REVIEW_TIMESTAMP


def test_older_cutoff_projects_history_without_overwriting_newer_evidence(
    factory: sessionmaker[Session],
) -> None:
    earlier = datetime(2026, 10, 1, tzinfo=UTC)
    _series(factory, "Monotonic", (date(2026, 7, 1), date(2026, 8, 1)))
    with session_scope(factory) as session:
        for index in range(2):
            transaction = session.get(VerifiedTransactionRecord, f"monotonic-{index}")
            batch = session.get(ImportBatchRecord, f"batch-monotonic-{index}")
            audit = session.get(FinancialRoleAuditRecord, f"role-monotonic-{index}")
            assert transaction is not None
            assert batch is not None
            assert audit is not None
            transaction.verified_at = earlier
            batch.imported_at = earlier
            audit.changed_at = earlier
    detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 2),
        knowledge_cutoff_at=NOW,
        policy=POLICY,
    )

    historical = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 2),
        knowledge_cutoff_at=earlier,
        policy=POLICY,
    )
    assert historical[0].knowledge_cutoff_at == earlier
    with session_scope(factory) as session:
        stored = session.get(
            RecurringPaymentCandidateRecord, historical[0].candidate_id
        )
        assert stored is not None
        assert stored.knowledge_cutoff_at == NOW


def test_legacy_availability_marker_hides_stored_review_state_from_history(
    factory: sessionmaker[Session],
) -> None:
    evidence_time = datetime(2026, 9, 1, tzinfo=UTC)
    historical_cutoff = datetime(2026, 10, 1, tzinfo=UTC)
    migration_marker = datetime(2026, 11, 1, tzinfo=UTC)
    _series(factory, "Legacy Marker", (date(2026, 7, 1), date(2026, 8, 1)))
    with session_scope(factory) as session:
        for index in range(2):
            transaction = session.get(
                VerifiedTransactionRecord, f"legacy-marker-{index}"
            )
            batch = session.get(ImportBatchRecord, f"batch-legacy-marker-{index}")
            audit = session.get(FinancialRoleAuditRecord, f"role-legacy-marker-{index}")
            assert transaction is not None
            assert batch is not None
            assert audit is not None
            transaction.verified_at = evidence_time
            batch.imported_at = evidence_time
            audit.changed_at = evidence_time
    candidate = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 2),
        knowledge_cutoff_at=migration_marker,
        policy=POLICY,
    )[0]
    with session_scope(factory) as session:
        stored = session.get(RecurringPaymentCandidateRecord, candidate.candidate_id)
        assert stored is not None
        stored.status = "cancelled"
        stored.reviewed_at = evidence_time
        stored.knowledge_cutoff_at = migration_marker

    historical = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 2),
        knowledge_cutoff_at=historical_cutoff,
        policy=POLICY,
    )

    assert len(historical) == 1
    assert historical[0].status is RecurrenceStatus.PENDING
    assert historical[0].knowledge_cutoff_at == historical_cutoff
    with session_scope(factory) as session:
        stored = session.get(RecurringPaymentCandidateRecord, candidate.candidate_id)
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.knowledge_cutoff_at == migration_marker


def test_review_after_historical_cutoff_is_projected_as_pending(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_cutoff = datetime(2026, 11, 1, tzinfo=UTC)
    historical_cutoff = datetime(2026, 12, 1, tzinfo=UTC)
    received_at = datetime(2026, 12, 15, tzinfo=UTC)
    evidence_time = datetime(2026, 10, 1, tzinfo=UTC)
    _series(factory, "Future Review", (date(2026, 7, 1), date(2026, 8, 1)))
    with session_scope(factory) as session:
        for index in range(2):
            transaction = session.get(
                VerifiedTransactionRecord, f"future-review-{index}"
            )
            batch = session.get(ImportBatchRecord, f"batch-future-review-{index}")
            audit = session.get(FinancialRoleAuditRecord, f"role-future-review-{index}")
            assert transaction is not None
            assert batch is not None
            assert audit is not None
            transaction.verified_at = evidence_time
            batch.imported_at = evidence_time
            audit.changed_at = evidence_time
    candidate = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 2),
        knowledge_cutoff_at=first_cutoff,
        policy=POLICY,
    )[0]
    monkeypatch.setattr(recurrence_service, "utc_now", lambda: received_at)
    result = review_recurring_payment(
        factory,
        review=RecurrenceReview(
            user_profile_id="profile-1",
            candidate_id=candidate.candidate_id,
            action=RecurrenceReviewAction.CONFIRM,
            reviewed_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )

    historical = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 2),
        knowledge_cutoff_at=historical_cutoff,
        policy=POLICY,
    )

    assert historical[0].status is RecurrenceStatus.PENDING
    with session_scope(factory) as session:
        stored = session.get(RecurringPaymentCandidateRecord, candidate.candidate_id)
        assert stored is not None
        assert stored.status == "confirmed"
        assert stored.reviewed_at == received_at
        series = session.get(RecurringSeriesRecord, result.recurring_series_id)
        assert series is not None
        assert series.created_at == received_at
