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
    DateRange,
    RecurrenceDetectionPolicy,
    RecurrenceFrequency,
    RecurrenceReview,
    RecurrenceReviewAction,
    RecurrenceStatus,
    RecurringPaymentCandidate,
)

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
POLICY = RecurrenceDetectionPolicy(
    minimum_occurrences=2,
    maximum_amount_variation=Decimal("2.00"),
    maximum_interval_variation_days=3,
    minimum_confidence=0,
)


@pytest.fixture
def factory() -> sessionmaker[Session]:
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
            _add_transaction(
                session,
                f"{merchant.lower().replace(' ', '-')}-{index}",
                merchant=merchant,
                amount=str(Decimal("-20.00") + Decimal(index) / 10),
                transaction_date=transaction_date,
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
        policy=POLICY,
    )
    second = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 29),
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
        policy=POLICY,
    )
    assert result[0].covered_missed_count == 2
    assert result[0].confidence < 0.7


def test_noise_and_untrusted_groups_are_not_detected(
    factory: sessionmaker[Session],
) -> None:
    _series(
        factory, "Irregular", (date(2026, 1, 1), date(2026, 2, 20), date(2026, 8, 1))
    )
    _series(factory, "Variable", (date(2026, 7, 1), date(2026, 8, 1)))
    with session_scope(factory) as session:
        item = session.get(RecurringSeriesRecord, "does-not-exist")
        assert item is None
        variable = session.get(VerifiedTransactionRecord, "variable-1")
        assert variable is not None
        variable.amount = Decimal("-99.00")
        # A same-day duplicate merchant cannot prove an interval.
        _add_transaction(
            session, "same-1", merchant="Same Day", transaction_date=date(2026, 8, 1)
        )
        _add_transaction(
            session, "same-2", merchant="Same Day", transaction_date=date(2026, 8, 1)
        )
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
            expected_amount=Decimal("-10"),
            frequency=RecurrenceFrequency.MONTHLY,
            interval_days=30,
            occurrence_dates=(date(2026, 8, 1), date(2026, 7, 1)),
            next_expected_date=date(2026, 9, 1),
            confidence=0.8,
            covered_missed_count=0,
            status=RecurrenceStatus.PENDING,
        )
    with pytest.raises(ValidationError):
        RecurringPaymentCandidate(
            candidate_id="candidate",
            account_id="current-1",
            merchant_group="synthetic",
            expected_amount=Decimal("-10"),
            frequency=RecurrenceFrequency.MONTHLY,
            interval_days=30,
            occurrence_dates=(date(2026, 7, 1), date(2026, 8, 1)),
            next_expected_date=date(2026, 8, 1),
            confidence=0.8,
            covered_missed_count=0,
            status=RecurrenceStatus.PENDING,
        )
    with pytest.raises(RecurrenceServiceError) as exc_info:
        detect_recurring_payments(
            factory,
            user_profile_id="missing",
            as_of_date=date(2026, 8, 1),
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


def test_detector_edge_filters_and_coverage_subtraction(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        _add_transaction(
            session, "no-merchant", merchant=None, transaction_date=date(2026, 8, 1)
        )
        _add_transaction(
            session, "single", merchant="Single", transaction_date=date(2026, 8, 1)
        )
    assert (
        detect_recurring_payments(
            factory,
            user_profile_id="profile-1",
            as_of_date=date(2026, 8, 2),
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
            policy=POLICY.model_copy(update={"minimum_confidence": 1.0}),
        )
        == ()
    )
    candidate = detect_recurring_payments(
        factory,
        user_profile_id="profile-1",
        as_of_date=date(2026, 8, 15),
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
            policy=POLICY,
        )
        == ()
    )
    _coverage(factory, "low-confidence-0", status="partial")
    with session_scope(factory) as session:
        assert recurrence_service._known_ranges(session, "current-1") == ()

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
        recurrence_service._known_ranges(session, "current-1")
