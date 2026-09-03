"""Tests for rule-based alerts and coverage-gated Isolation Forest detection."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import cashflow_ai.anomalies.demo as anomaly_demo
import cashflow_ai.anomalies.service as anomaly_service
from cashflow_ai.anomalies import (
    AnomalyDetectionError,
    AnomalyDetectionErrorCode,
    detect_unusual_transactions,
    record_anomaly_feedback,
)
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    AnomalyAlertRecord,
    CategoryDecisionRecord,
    CategoryRecord,
    FinancialRoleAuditRecord,
    FinancialRoleRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    RecurringPaymentCandidateRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas import (
    AnomalyDetectionMode,
    AnomalyDetectionPlan,
    AnomalyDetectionPolicy,
    AnomalyDetectionResult,
    AnomalyExclusionReason,
    AnomalyFeedbackAction,
    AnomalyFeedbackRequest,
    AnomalyReviewStatus,
    AnomalySignal,
    AnomalySignalCode,
    AnomalyUserLabel,
    AnomalyWarningCode,
    IsolationForestRunMetadata,
    TransactionAnomalyAlert,
)
from cashflow_ai.schemas.transactions import FinancialRole

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
AS_OF = date(2026, 9, 30)


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine: Engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with session_scope(result) as session:
        session.add_all(
            [
                UserProfileRecord(
                    id="profile-1",
                    display_name="Synthetic User",
                    base_currency="GBP",
                    timezone="Europe/London",
                    created_at=NOW - timedelta(days=365),
                    updated_at=NOW - timedelta(days=365),
                ),
                UserProfileRecord(
                    id="profile-2",
                    display_name="Other Synthetic User",
                    base_currency="GBP",
                    timezone="Europe/London",
                    created_at=NOW - timedelta(days=365),
                    updated_at=NOW - timedelta(days=365),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AccountRecord(
                    id="account-1",
                    user_profile_id="profile-1",
                    name="Synthetic Current",
                    account_type="current",
                    currency="GBP",
                    created_at=NOW - timedelta(days=365),
                ),
                AccountRecord(
                    id="account-2",
                    user_profile_id="profile-1",
                    name="Synthetic Savings",
                    account_type="savings",
                    currency="GBP",
                    created_at=NOW - timedelta(days=365),
                ),
                AccountRecord(
                    id="foreign-account",
                    user_profile_id="profile-2",
                    name="Other Synthetic Current",
                    account_type="current",
                    currency="GBP",
                    created_at=NOW - timedelta(days=365),
                ),
            ]
        )
        session.add_all(
            FinancialRoleRecord(
                id=role.value,
                name=role.value.replace("_", " ").title(),
            )
            for role in FinancialRole
        )
        session.add_all(
            [
                CategoryRecord(
                    id="groceries",
                    name="Groceries",
                    taxonomy_version="1.0",
                    is_active=True,
                ),
                CategoryRecord(
                    id="housing",
                    name="Housing",
                    taxonomy_version="1.0",
                    is_active=True,
                ),
            ]
        )
    return result


def _policy(**overrides: Any) -> AnomalyDetectionPolicy:
    values: dict[str, Any] = {
        "history_lookback_days": 120,
        "detection_window_days": 10,
        "minimum_covered_days": 60,
        "minimum_coverage_ratio": 0.5,
        "minimum_history_transactions": 12,
        "minimum_large_amount": "100.00",
        "minimum_new_merchant_amount": "50.00",
        "minimum_high_daily_spending": "100.00",
        "isolation_estimators": 50,
        "isolation_contamination": 0.1,
        "random_seed": 7,
    }
    values.update(overrides)
    return AnomalyDetectionPolicy(**values)


def _plan(
    *,
    account_ids: tuple[str, ...] = ("account-1",),
    profile_id: str = "profile-1",
    policy: AnomalyDetectionPolicy | None = None,
) -> AnomalyDetectionPlan:
    return AnomalyDetectionPlan(
        user_profile_id=profile_id,
        account_ids=account_ids,
        as_of_date=AS_OF,
        knowledge_cutoff_at=NOW,
        policy=policy or _policy(),
    )


def _add_coverage(
    factory: sessionmaker[Session],
    *,
    account_id: str = "account-1",
    start: date = date(2026, 6, 3),
    end: date = AS_OF,
    status: str = "complete",
    missing: list[dict[str, str]] | None = None,
    source_type: str = "csv",
    verification_status: str = "verified",
    imported_at: datetime = NOW - timedelta(days=1),
) -> None:
    identifier = f"coverage-{account_id}-{source_type}-{start}-{end}-{status}"
    with session_scope(factory) as session:
        batch = ImportBatchRecord(
            id=f"batch-{identifier}",
            account_id=account_id,
            source_type=source_type,
            source_filename=f"{identifier}.statement",
            file_hash=_digest(identifier),
            mime_type="text/csv" if source_type == "csv" else "application/pdf",
            byte_size=100,
            verification_status=verification_status,
            imported_at=imported_at,
        )
        session.add(batch)
        context = ImportContextRecord(
            id=f"context-{identifier}",
            import_batch_id=batch.id,
            flags_json=[],
            note=None,
            created_at=imported_at,
        )
        session.add(context)
        session.flush()
        session.add(
            StatementCoverageRecord(
                id=identifier,
                import_context_id=context.id,
                statement_start_date=start,
                statement_end_date=end,
                coverage_status=status,
                missing_periods_json=missing or [],
            )
        )


def _add_transaction(
    factory: sessionmaker[Session],
    transaction_id: str,
    *,
    transaction_date: date,
    amount: str,
    merchant: str | None = "Synthetic Grocer",
    description: str = "Synthetic card purchase",
    account_id: str = "account-1",
    role: FinancialRole = FinancialRole.EXPENSE,
    category_id: str = "groceries",
    balance_after: str | None = "500.00",
    external_id: str | None = None,
    transaction_type: str | None = "card",
    issues: list[dict[str, Any]] | None = None,
    canonical: bool = True,
    source_type: str = "csv",
    batch_status: str = "verified",
    raw_status: str = "confirmed",
    evidence_at: datetime = NOW - timedelta(days=1),
    superseded_category_after_cutoff: bool = False,
) -> None:
    parsed_amount = Decimal(amount)
    with session_scope(factory) as session:
        batch = ImportBatchRecord(
            id=f"batch-{transaction_id}",
            account_id=account_id,
            source_type=source_type,
            source_filename=f"{transaction_id}.statement",
            file_hash=_digest(f"file-{transaction_id}"),
            mime_type="text/csv" if source_type == "csv" else "application/pdf",
            byte_size=100,
            verification_status=batch_status,
            imported_at=evidence_at,
        )
        session.add(batch)
        raw = RawTransactionRecord(
            id=f"raw-{transaction_id}",
            import_batch_id=batch.id,
            source_type=source_type,
            source_row_number=2 if source_type == "csv" else None,
            page_number=None if source_type == "csv" else 1,
            page_record_number=None if source_type == "csv" else 1,
            raw_payload={"synthetic": transaction_id},
            original_date_text=transaction_date.isoformat(),
            original_description=description,
            original_amount_text=amount,
            parser_name="synthetic_parser",
            parser_version="1.0",
            source_fingerprint=_digest(f"source-{transaction_id}"),
            canonical_fingerprint=(
                _digest(f"canonical-{transaction_id}") if canonical else None
            ),
            issues_json=issues or [],
            review_status=raw_status,
            created_at=evidence_at,
        )
        session.add(raw)
        transaction = VerifiedTransactionRecord(
            id=transaction_id,
            raw_transaction_id=raw.id,
            account_id=account_id,
            transaction_date=transaction_date,
            posting_date=transaction_date,
            description=description,
            merchant=merchant,
            amount=parsed_amount,
            balance_after=(Decimal(balance_after) if balance_after else None),
            currency="GBP",
            external_id=external_id,
            transaction_type=transaction_type,
            direction="inflow" if parsed_amount > 0 else "outflow",
            category_id=category_id,
            financial_role_id=role.value,
            verified_at=evidence_at,
        )
        session.add(transaction)
        session.flush()
        if role is not FinancialRole.UNKNOWN:
            session.add(
                FinancialRoleAuditRecord(
                    id=f"role-{transaction_id}",
                    verified_transaction_id=transaction_id,
                    previous_role_id=FinancialRole.UNKNOWN.value,
                    new_role_id=role.value,
                    source="user_override",
                    changed_at=evidence_at,
                )
            )
        session.add(
            CategoryDecisionRecord(
                id=f"category-{transaction_id}",
                verified_transaction_id=transaction_id,
                category_id=category_id,
                source="merchant_mapping",
                status=(
                    "superseded" if superseded_category_after_cutoff else "applied"
                ),
                confidence=None,
                model_version=None,
                rule_id="synthetic-rule",
                taxonomy_version="1.0",
                rule_set_version="rules-1.0",
                reason_code="known_merchant",
                created_at=evidence_at,
                reviewed_at=(
                    NOW + timedelta(days=1)
                    if superseded_category_after_cutoff
                    else None
                ),
            )
        )


def _add_recurrence(
    factory: sessionmaker[Session],
    *,
    candidate_id: str,
    merchant: str,
    expected_amount: str,
    status: str,
    reviewed_at: datetime = NOW - timedelta(days=20),
    account_id: str = "account-1",
) -> None:
    with session_scope(factory) as session:
        session.add(
            RecurringPaymentCandidateRecord(
                id=candidate_id,
                account_id=account_id,
                recurring_series_id=None,
                merchant_group=merchant,
                currency="GBP",
                direction="outflow",
                financial_role_id=FinancialRole.EXPENSE.value,
                expected_amount=Decimal(expected_amount),
                frequency="monthly",
                interval_days=30,
                next_expected_date=AS_OF + timedelta(days=30),
                confidence=Decimal("0.95"),
                covered_missed_count=0,
                status=status,
                detected_at=reviewed_at - timedelta(days=1),
                evidence_as_of_date=reviewed_at.date() - timedelta(days=1),
                knowledge_cutoff_at=reviewed_at - timedelta(days=1),
                reviewed_at=reviewed_at,
            )
        )


def _seed_reference(factory: sessionmaker[Session], count: int = 24) -> None:
    for index in range(count):
        _add_transaction(
            factory,
            f"history-{index:02d}",
            transaction_date=date(2026, 6, 5) + timedelta(days=index * 4),
            amount=str(
                -(Decimal("10.00") + Decimal(index) / 7).quantize(Decimal("0.01"))
            ),
            merchant=("Synthetic Grocer" if index % 2 == 0 else "Synthetic Transit"),
            description=f"Fictional historical purchase {index}",
            category_id="groceries",
        )


def _codes(
    result: AnomalyDetectionResult, transaction_id: str
) -> set[AnomalySignalCode]:
    alert = next(
        item for item in result.alerts if item.transaction_id == transaction_id
    )
    return {signal.code for signal in alert.signals}


def test_detects_all_rule_types_and_runs_reproducible_isolation_forest(
    factory: sessionmaker[Session],
) -> None:
    _add_coverage(factory)
    _seed_reference(factory)
    _add_recurrence(
        factory,
        candidate_id="rent-series",
        merchant="Synthetic Rent",
        expected_amount="-50.00",
        status="confirmed",
    )
    _add_recurrence(
        factory,
        candidate_id="cancelled-series",
        merchant="Synthetic Cancelled Service",
        expected_amount="-20.00",
        status="cancelled",
    )
    _add_transaction(
        factory,
        "probable-source",
        transaction_date=date(2026, 9, 20),
        amount="-25.13",
        merchant="Synthetic Duplicate Merchant",
        description="Fictional duplicate purchase",
    )
    _add_transaction(
        factory,
        "probable-copy",
        transaction_date=date(2026, 9, 21),
        amount="-25.13",
        merchant="Synthetic Duplicate Merchant",
        description="Fictional duplicate purchase",
    )
    _add_transaction(
        factory,
        "exact-issue",
        transaction_date=date(2026, 9, 22),
        amount="-17.13",
        issues=[{"details": [{"issue_code": "exact_duplicate"}]}],
    )
    _add_transaction(
        factory,
        "large",
        transaction_date=date(2026, 9, 23),
        amount="-500.00",
        merchant="Synthetic Existing Large",
    )
    _add_transaction(
        factory,
        "new-merchant",
        transaction_date=date(2026, 9, 24),
        amount="-75.00",
        merchant="Synthetic Brand New Merchant",
    )
    _add_transaction(
        factory,
        "rent-increase",
        transaction_date=date(2026, 9, 25),
        amount="-60.00",
        merchant="Synthetic Rent",
        category_id="housing",
    )
    _add_transaction(
        factory,
        "rent-normal",
        transaction_date=date(2026, 9, 26),
        amount="-50.00",
        merchant="Synthetic Rent",
        category_id="housing",
    )
    _add_transaction(
        factory,
        "after-cancel",
        transaction_date=date(2026, 9, 27),
        amount="-20.00",
        merchant="Synthetic Cancelled Service",
    )
    _add_transaction(
        factory,
        "daily-a",
        transaction_date=date(2026, 9, 28),
        amount="-70.01",
        merchant="Synthetic Grocer",
    )
    _add_transaction(
        factory,
        "daily-z",
        transaction_date=date(2026, 9, 28),
        amount="-70.02",
        merchant="Synthetic Transit",
    )
    _add_transaction(
        factory,
        "negative-balance",
        transaction_date=date(2026, 9, 29),
        amount="-30.03",
        merchant="Synthetic Grocer",
        balance_after="-12.34",
    )
    _add_transaction(
        factory,
        "normal",
        transaction_date=AS_OF,
        amount="-18.88",
        merchant="Synthetic Grocer",
    )

    first = detect_unusual_transactions(factory, plan=_plan())
    second = detect_unusual_transactions(factory, plan=_plan())

    assert first == second
    assert first.mode is AnomalyDetectionMode.RULES_AND_MODEL
    assert first.model_metadata is not None
    assert first.model_metadata.model_type == "IsolationForest"
    assert first.model_metadata.feature_names == (
        "log_absolute_amount",
        "merchant_frequency",
        "category",
        "weekday",
        "days_since_previous_merchant_transaction",
        "difference_from_merchant_median",
        "difference_from_category_median",
        "merchant_novelty",
    )
    assert first.model_metadata.random_seed == 7
    assert first.minimum_reference_covered_days == 110
    assert first.warnings == ()
    assert AnomalySignalCode.PROBABLE_DUPLICATE in _codes(first, "probable-copy")
    assert AnomalySignalCode.EXACT_DUPLICATE in _codes(first, "exact-issue")
    assert AnomalySignalCode.UNUSUALLY_LARGE_TRANSACTION in _codes(first, "large")
    assert AnomalySignalCode.NEW_MERCHANT_HIGH_SPENDING in _codes(first, "new-merchant")
    assert AnomalySignalCode.RECURRING_PRICE_INCREASE in _codes(first, "rent-increase")
    assert AnomalySignalCode.CHARGE_AFTER_CANCELLATION in _codes(first, "after-cancel")
    assert AnomalySignalCode.UNUSUALLY_HIGH_DAILY_SPENDING in _codes(first, "daily-z")
    assert AnomalySignalCode.NEGATIVE_BALANCE_EVENT in _codes(first, "negative-balance")
    assert "rent-normal" not in {item.transaction_id for item in first.alerts}
    duplicate = next(
        item for item in first.alerts if item.transaction_id == "exact-issue"
    )
    assert duplicate.label is AnomalyUserLabel.POSSIBLE_DUPLICATE
    assert duplicate.model_score is None
    assert all("fraud" not in item.label.value.casefold() for item in first.alerts)

    dismissed = record_anomaly_feedback(
        factory,
        request=AnomalyFeedbackRequest(
            plan=_plan(),
            transaction_id="exact-issue",
            action=AnomalyFeedbackAction.EXPECTED_ACTIVITY,
        ),
    )
    assert dismissed.status is AnomalyReviewStatus.DISMISSED
    with session_scope(factory) as session:
        saved = session.scalar(
            select(AnomalyAlertRecord).where(
                AnomalyAlertRecord.verified_transaction_id == "exact-issue"
            )
        )
        assert saved is not None
        assert saved.reason == "exact_duplicate"
        session.add(
            AnomalyAlertRecord(
                verified_transaction_id="large",
                score=Decimal("0.500000"),
                reason="unusually_large_transaction",
                status="open",
            )
        )

    reviewed_scan = detect_unusual_transactions(factory, plan=_plan())
    reviewed_alerts = {item.transaction_id: item for item in reviewed_scan.alerts}
    assert reviewed_alerts["exact-issue"].review_status is AnomalyReviewStatus.DISMISSED
    assert reviewed_alerts["large"].review_status is None

    confirmed = record_anomaly_feedback(
        factory,
        request=AnomalyFeedbackRequest(
            plan=_plan(),
            transaction_id="exact-issue",
            action=AnomalyFeedbackAction.CONFIRMED_UNUSUAL,
        ),
    )
    assert confirmed.status is AnomalyReviewStatus.REVIEWED
    with session_scope(factory) as session:
        updated = session.scalar(
            select(AnomalyAlertRecord).where(
                AnomalyAlertRecord.verified_transaction_id == "exact-issue"
            )
        )
        assert updated is not None
        assert updated.status == "reviewed"

    with pytest.raises(AnomalyDetectionError) as missing_alert:
        record_anomaly_feedback(
            factory,
            request=AnomalyFeedbackRequest(
                plan=_plan(),
                transaction_id="not-an-alert",
                action=AnomalyFeedbackAction.EXPECTED_ACTIVITY,
            ),
        )
    assert missing_alert.value.code is AnomalyDetectionErrorCode.ALERT_NOT_FOUND


def test_sparse_or_uncovered_data_returns_rules_only_with_explicit_warnings(
    factory: sessionmaker[Session],
) -> None:
    _add_coverage(
        factory,
        start=date(2026, 9, 21),
        status="gapped",
        missing=[{"start_date": "2026-09-22", "end_date": "2026-09-29"}],
    )
    _add_transaction(
        factory,
        "sparse-negative",
        transaction_date=AS_OF,
        amount="-20.00",
        balance_after="-1.00",
    )
    _add_transaction(
        factory,
        "uncovered",
        transaction_date=date(2026, 9, 25),
        amount="-200.00",
    )

    result = detect_unusual_transactions(factory, plan=_plan())

    assert result.mode is AnomalyDetectionMode.RULES_ONLY
    assert result.model_metadata is None
    assert result.scored_transaction_count == 0
    assert result.warnings == (
        AnomalyWarningCode.INSUFFICIENT_COVERAGE,
        AnomalyWarningCode.INSUFFICIENT_HISTORY,
    )
    assert _codes(result, "sparse-negative") == {
        AnomalySignalCode.NEGATIVE_BALANCE_EVENT
    }
    assert result.exclusions[0].reason is AnomalyExclusionReason.UNCOVERED_DATE


def test_model_excludes_pending_transfers_duplicates_and_unresolved_roles(
    factory: sessionmaker[Session],
) -> None:
    _add_coverage(factory)
    _seed_reference(factory)
    _add_transaction(
        factory,
        "reference-income",
        transaction_date=date(2026, 9, 10),
        amount="100.00",
        role=FinancialRole.INCOME,
    )
    _add_transaction(
        factory,
        "pending",
        transaction_date=date(2026, 9, 21),
        amount="-12.00",
        transaction_type="pending card",
        issues=[{"code": "exact_duplicate"}],
    )
    _add_transaction(
        factory,
        "transfer",
        transaction_date=date(2026, 9, 22),
        amount="-50.00",
        role=FinancialRole.TRANSFER_OUT,
    )
    _add_transaction(
        factory,
        "unresolved",
        transaction_date=date(2026, 9, 23),
        amount="-20.00",
        role=FinancialRole.UNKNOWN,
    )
    _add_transaction(
        factory,
        "probable-issue",
        transaction_date=date(2026, 9, 24),
        amount="-20.00",
        issues=[{"code": "probable_duplicate"}],
    )
    _add_transaction(
        factory,
        "current-income",
        transaction_date=date(2026, 9, 25),
        amount="101.00",
        role=FinancialRole.INCOME,
    )

    result = detect_unusual_transactions(factory, plan=_plan())
    exclusions = {item.reason: item.count for item in result.exclusions}

    assert exclusions[AnomalyExclusionReason.PENDING] == 1
    assert exclusions[AnomalyExclusionReason.TRANSFER] == 1
    assert exclusions[AnomalyExclusionReason.UNRESOLVED_ROLE] == 1
    assert exclusions[AnomalyExclusionReason.DUPLICATE] == 1
    assert _codes(result, "probable-issue") == {AnomalySignalCode.PROBABLE_DUPLICATE}
    assert "pending" not in {item.transaction_id for item in result.alerts}


def test_no_current_model_candidates_is_disclosed_and_future_evidence_is_ignored(
    factory: sessionmaker[Session],
) -> None:
    _add_coverage(factory)
    _seed_reference(factory)
    _add_transaction(
        factory,
        "future-evidence",
        transaction_date=AS_OF,
        amount="-999.00",
        evidence_at=NOW + timedelta(days=1),
    )
    _add_transaction(
        factory,
        "current-transfer",
        transaction_date=AS_OF,
        amount="-10.00",
        role=FinancialRole.TRANSFER_OUT,
    )

    result = detect_unusual_transactions(factory, plan=_plan())

    assert result.mode is AnomalyDetectionMode.RULES_ONLY
    assert result.warnings == (AnomalyWarningCode.NO_ELIGIBLE_DETECTION_TRANSACTIONS,)
    assert result.verified_transaction_count == 25
    assert result.alerts == ()


@pytest.mark.parametrize(
    ("plan", "code"),
    [
        (_plan(profile_id="missing"), AnomalyDetectionErrorCode.PROFILE_NOT_FOUND),
        (_plan(account_ids=("missing",)), AnomalyDetectionErrorCode.ACCOUNT_NOT_FOUND),
        (
            _plan(account_ids=("foreign-account",)),
            AnomalyDetectionErrorCode.ACCOUNT_NOT_OWNED,
        ),
    ],
)
def test_scope_errors_are_controlled(
    factory: sessionmaker[Session],
    plan: AnomalyDetectionPlan,
    code: AnomalyDetectionErrorCode,
) -> None:
    with pytest.raises(AnomalyDetectionError) as caught:
        detect_unusual_transactions(factory, plan=plan)
    assert caught.value.code is code


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"history_lookback_days": 14, "detection_window_days": 14},
            "detection window must be shorter",
        ),
        (
            {
                "history_lookback_days": 20,
                "detection_window_days": 10,
                "minimum_covered_days": 11,
            },
            "minimum covered days cannot exceed",
        ),
    ],
)
def test_policy_rejects_impossible_evidence_windows(
    values: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        AnomalyDetectionPolicy(**values)


def test_plan_rejects_duplicate_accounts_and_future_as_of_date() -> None:
    with pytest.raises(ValidationError, match="account IDs must be unique"):
        _plan(account_ids=("account-1", "account-1"))
    with pytest.raises(ValidationError, match="cutoff must follow the complete UTC"):
        AnomalyDetectionPlan(
            user_profile_id="profile-1",
            account_ids=("account-1",),
            as_of_date=date(2026, 10, 2),
            knowledge_cutoff_at=NOW,
            policy=_policy(),
        )
    with pytest.raises(ValidationError, match="cutoff must follow the complete UTC"):
        AnomalyDetectionPlan(
            user_profile_id="profile-1",
            account_ids=("account-1",),
            as_of_date=AS_OF,
            knowledge_cutoff_at=datetime(2026, 9, 30, 23, 59, tzinfo=UTC),
            policy=_policy(),
        )


def _signal(
    code: AnomalySignalCode = AnomalySignalCode.ISOLATION_FOREST,
    score: str = "0.700000",
) -> AnomalySignal:
    return AnomalySignal(code=code, score=Decimal(score))


def _alert(**overrides: Any) -> TransactionAnomalyAlert:
    values: dict[str, Any] = {
        "transaction_id": "synthetic-alert",
        "account_id": "account-1",
        "transaction_date": AS_OF,
        "label": AnomalyUserLabel.UNUSUAL,
        "score": Decimal("0.700000"),
        "signals": (_signal(),),
        "model_score": Decimal("0.700000"),
    }
    values.update(overrides)
    return TransactionAnomalyAlert(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"signals": (_signal(), _signal()), "score": "0.700000"},
            "signal codes must be unique",
        ),
        ({"label": AnomalyUserLabel.NEEDS_REVIEW}, "user label does not match"),
        ({"model_score": None}, "model score must appear exactly"),
        ({"model_score": Decimal("0.600000")}, "model score must match"),
        ({"score": Decimal("0.600000")}, "alert score must equal"),
    ],
)
def test_alert_contract_rejects_inconsistent_evidence(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _alert(**overrides)


def _metadata(**overrides: Any) -> IsolationForestRunMetadata:
    values: dict[str, Any] = {
        "model_type": "IsolationForest",
        "model_version": "test-1",
        "feature_schema_version": "features-1",
        "feature_names": tuple(f"feature-{index}" for index in range(8)),
        "training_start_date": date(2026, 1, 1),
        "training_end_date": date(2026, 2, 1),
        "training_transaction_count": 10,
        "scored_transaction_count": 1,
        "category_levels": ("groceries",),
        "estimators": 50,
        "contamination": 0.1,
        "random_seed": 1,
    }
    values.update(overrides)
    return IsolationForestRunMetadata(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"training_end_date": date(2025, 12, 31)},
            "training dates are reversed",
        ),
        (
            {"feature_names": ("same",) * 8},
            "feature names must be unique",
        ),
        (
            {"category_levels": ("same", "same")},
            "category levels must be unique",
        ),
    ],
)
def test_model_metadata_rejects_inconsistent_reproducibility_facts(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _metadata(**overrides)


def _result(**overrides: Any) -> AnomalyDetectionResult:
    values: dict[str, Any] = {
        "plan": _plan(),
        "mode": AnomalyDetectionMode.RULES_ONLY,
        "alerts": (),
        "verified_transaction_count": 0,
        "reference_transaction_count": 0,
        "scored_transaction_count": 0,
        "minimum_reference_covered_days": 0,
        "minimum_reference_coverage_ratio": 0,
        "exclusions": (),
        "warnings": (AnomalyWarningCode.INSUFFICIENT_HISTORY,),
        "model_metadata": None,
    }
    values.update(overrides)
    return AnomalyDetectionResult(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"mode": AnomalyDetectionMode.RULES_AND_MODEL},
            "mode and model metadata disagree",
        ),
        ({"scored_transaction_count": 1}, "rule-only results cannot claim"),
        (
            {
                "alerts": (
                    _alert(transaction_date=AS_OF),
                    _alert(
                        transaction_id="earlier",
                        transaction_date=AS_OF - timedelta(days=1),
                    ),
                )
            },
            "alerts must be chronologically ordered",
        ),
    ],
)
def test_result_contract_rejects_inconsistent_mode_or_order(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _result(**overrides)


def test_model_result_requires_scored_rows() -> None:
    with pytest.raises(ValidationError, match="must score at least one"):
        _result(
            mode=AnomalyDetectionMode.RULES_AND_MODEL,
            model_metadata=_metadata(),
            scored_transaction_count=0,
        )


def test_private_edge_helpers_keep_duplicates_and_features_conservative(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = anomaly_service._TransactionEvidence(
        transaction_id="base",
        account_id="account-1",
        transaction_date=date(2026, 9, 1),
        description="Synthetic edge case",
        merchant=None,
        merchant_key="synthetic edge case",
        amount=Decimal("-10.00"),
        balance_after=None,
        category_id=None,
        financial_role="expense",
        external_id="same-bank-id",
        transaction_type=None,
        source_fingerprint=_digest("base-source"),
        canonical_fingerprint=_digest("base-canonical"),
        issue_codes=frozenset(),
    )
    exact = anomaly_service._TransactionEvidence(
        **{
            **base.__dict__,
            "transaction_id": "exact",
            "transaction_date": date(2026, 9, 2),
            "source_fingerprint": _digest("exact-source"),
            "canonical_fingerprint": _digest("exact-canonical"),
        }
    )
    without_canonical = anomaly_service._TransactionEvidence(
        **{**base.__dict__, "transaction_id": "none", "canonical_fingerprint": None}
    )

    signal = anomaly_service._duplicate_signal(exact, (without_canonical, base))

    assert signal is not None
    assert signal.code is AnomalySignalCode.EXACT_DUPLICATE
    assert signal.related_transaction_id == "base"
    assert anomaly_service._duplicate_signal(without_canonical, (base,)) is None
    assert anomaly_service._duplicate_signal(exact, (base, exact)) is not None
    assert anomaly_service._quantile((Decimal("2"),), 0.9) == 2
    assert anomaly_service._issue_codes("not structured") == frozenset()
    assert anomaly_service._issue_codes({"code": 123, "message": "safe"}) == frozenset()
    assert anomaly_service._signal_strength(Decimal("1"), Decimal("0")) == 1
    assert anomaly_service._subtract_missing_days(
        date(2026, 1, 1),
        date(2026, 1, 2),
        [{"start_date": "2026-02-01", "end_date": "2026-02-02"}],
    ) == {date(2026, 1, 1), date(2026, 1, 2)}

    with session_scope(factory) as session:
        assert anomaly_service._latest_roles_as_of(session, (), NOW) == {}
        assert anomaly_service._latest_categories_as_of(session, (), NOW) == {}

    train, detection, levels = anomaly_service._model_feature_rows(
        (base, exact),
        (without_canonical,),
        maximum_gap_days=365,
    )
    assert levels == ("uncategorised",)
    assert train[0][1] == 0
    assert train[1][1] == 1
    assert detection[0][4] == 0
    empty_train, one_detection, empty_levels = anomaly_service._model_feature_rows(
        (), (base,), maximum_gap_days=365
    )
    assert empty_train == ()
    assert len(one_detection) == 1
    assert empty_levels == ("uncategorised",)

    class _AllNormalForest:
        def __init__(self, **parameters: Any) -> None:
            assert parameters["random_state"] == 7

        def fit(self, values: Any) -> _AllNormalForest:
            assert values
            return self

        def decision_function(self, values: Any) -> list[float]:
            return [0.1 for _item in values]

        def predict(self, values: Any) -> list[int]:
            return [1 for _item in values]

    monkeypatch.setattr(
        "cashflow_ai.anomalies.service.ensemble.IsolationForest", _AllNormalForest
    )
    scores, metadata = anomaly_service._isolation_scores(
        (base, exact), (base,), _plan()
    )
    assert scores == {}
    assert metadata.scored_transaction_count == 1


def test_digital_pdf_requires_verified_batch_and_category_as_of_is_retained(
    factory: sessionmaker[Session],
) -> None:
    _add_coverage(
        factory,
        source_type="digital_pdf",
        verification_status="verified",
    )
    _seed_reference(factory)
    _add_transaction(
        factory,
        "verified-pdf",
        transaction_date=AS_OF,
        amount="-30.00",
        source_type="digital_pdf",
        batch_status="verified",
        superseded_category_after_cutoff=True,
    )
    _add_transaction(
        factory,
        "unverified-pdf",
        transaction_date=AS_OF,
        amount="-500.00",
        source_type="digital_pdf",
        batch_status="needs_review",
    )

    result = detect_unusual_transactions(factory, plan=_plan())

    assert result.verified_transaction_count == 25
    assert result.model_metadata is not None
    assert result.model_metadata.category_levels == ("groceries",)


def test_partial_coverage_and_same_day_cancellation_do_not_create_false_alerts(
    factory: sessionmaker[Session],
) -> None:
    _add_coverage(factory, status="partial")
    _add_coverage(
        factory,
        start=date(2026, 9, 20),
        status="complete",
    )
    _add_recurrence(
        factory,
        candidate_id="same-day-cancel",
        merchant="Synthetic Same Day",
        expected_amount="-20.00",
        status="cancelled",
        reviewed_at=datetime(2026, 9, 30, 8, tzinfo=UTC),
    )
    _add_transaction(
        factory,
        "same-day-charge",
        transaction_date=AS_OF,
        amount="-20.00",
        merchant="Synthetic Same Day",
    )

    result = detect_unusual_transactions(factory, plan=_plan())

    assert result.alerts == ()
    assert AnomalyWarningCode.INSUFFICIENT_COVERAGE in result.warnings


@pytest.mark.parametrize(
    ("arguments", "expected_mode", "expected_warning"),
    [
        (("anomaly-demo",), "rules_and_model", "warnings: none"),
        (
            ("anomaly-demo", "--sparse"),
            "rules_only",
            "warnings: insufficient_coverage, insufficient_history",
        ),
    ],
)
def test_manual_demo_is_reproducible_and_carefully_worded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    expected_mode: str,
    expected_warning: str,
) -> None:
    monkeypatch.setattr("sys.argv", list(arguments))

    anomaly_demo.main()

    output = capsys.readouterr().out
    assert f"detection mode: {expected_mode}" in output
    assert expected_warning in output
    assert "Possible duplicate: fictional-duplicate" in output
    assert "known recurring rent protected: yes" in output
    assert "not confirmed fraud" in output


def test_manual_demo_rejects_unsafe_history_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.argv", ["anomaly-demo", "--history-transactions", "5"])
    with pytest.raises(SystemExit, match="2"):
        anomaly_demo.main()
