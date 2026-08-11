"""Tests for conservative financial-role suggestions and audited decisions."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import cashflow_ai.financial_roles.service as role_service
from cashflow_ai.financial_roles import (
    FinancialRoleServiceError,
    FinancialRoleServiceErrorCode,
    apply_transaction_review_action,
    confirm_financial_role_suggestion,
    generate_financial_role_suggestions,
    list_financial_role_audits,
    list_financial_role_review_queue,
    reject_financial_role_suggestion,
)
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    FinancialRoleAuditRecord,
    FinancialRoleRecord,
    FinancialRoleSuggestionRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    UserFlagRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.persistence.repositories import FinancialRoleRepository
from cashflow_ai.schemas import (
    FinancialRole,
    FinancialRoleSuggestion,
    RoleDecisionSource,
    RoleSuggestionKind,
    RoleSuggestionReason,
    RoleSuggestionStatus,
    TransactionReviewAction,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _required[T](value: T | None) -> T:
    assert value is not None
    return value


@pytest.fixture
def engine() -> Engine:
    database_engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    return database_engine


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _seed_profiles(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        session.add_all(
            [
                UserProfileRecord(
                    id="profile-1",
                    display_name="Synthetic User",
                    base_currency="GBP",
                    timezone="Europe/London",
                ),
                UserProfileRecord(
                    id="profile-2",
                    display_name="Other Synthetic User",
                    base_currency="GBP",
                    timezone="Europe/London",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AccountRecord(
                    id="current-1",
                    user_profile_id="profile-1",
                    name="Everyday Current",
                    account_type="current",
                    currency="GBP",
                ),
                AccountRecord(
                    id="savings-1",
                    user_profile_id="profile-1",
                    name="Rainy Day Savings",
                    account_type="savings",
                    currency="GBP",
                ),
                AccountRecord(
                    id="other-current",
                    user_profile_id="profile-2",
                    name="Other Current",
                    account_type="current",
                    currency="GBP",
                ),
            ]
        )
        session.flush()
        session.add_all(
            FinancialRoleRecord(
                id=role.value, name=role.value.replace("_", " ").title()
            )
            for role in FinancialRole
        )


def _add_transaction(
    session: Session,
    transaction_id: str,
    *,
    account_id: str,
    amount: str,
    description: str,
    transaction_date: date = date(2026, 8, 5),
    role: FinancialRole = FinancialRole.UNKNOWN,
    note: str | None = None,
    flags: list[str] | None = None,
    include_context: bool = True,
) -> VerifiedTransactionRecord:
    batch_id = f"batch-{transaction_id}"
    batch = ImportBatchRecord(
        id=batch_id,
        account_id=account_id,
        source_type="csv",
        source_filename=f"{transaction_id}.csv",
        file_hash=_hash(batch_id),
        mime_type="text/csv",
        byte_size=100,
        verification_status="verified",
        imported_at=NOW,
    )
    session.add(batch)
    if include_context:
        session.add(
            ImportContextRecord(
                id=f"context-{transaction_id}",
                import_batch_id=batch_id,
                flags_json=flags or [],
                note=note,
                created_at=NOW,
            )
        )
    raw = RawTransactionRecord(
        id=f"raw-{transaction_id}",
        import_batch_id=batch_id,
        source_type="csv",
        source_row_number=2,
        page_number=None,
        page_record_number=None,
        raw_payload={
            "Date": transaction_date.isoformat(),
            "Description": description,
            "Amount": amount,
        },
        original_date_text=transaction_date.isoformat(),
        original_description=description,
        original_amount_text=amount,
        parser_name="synthetic_parser",
        parser_version="1.0.0",
        source_fingerprint=_hash(f"source-{transaction_id}"),
        canonical_fingerprint=_hash(f"canonical-{transaction_id}"),
        issues_json=[],
        review_status="confirmed",
        created_at=NOW,
    )
    session.add(raw)
    parsed_amount = Decimal(amount)
    transaction = VerifiedTransactionRecord(
        id=transaction_id,
        raw_transaction_id=raw.id,
        account_id=account_id,
        transaction_date=transaction_date,
        posting_date=None,
        description=description,
        merchant=None,
        amount=parsed_amount,
        balance_after=None,
        currency="GBP",
        external_id=transaction_id,
        transaction_type="synthetic",
        direction="inflow" if parsed_amount > 0 else "outflow",
        category_id=None,
        financial_role_id=role.value,
        verified_at=NOW,
    )
    session.add(transaction)
    session.flush()
    return transaction


def _generate(factory: sessionmaker[Session]) -> tuple[FinancialRoleSuggestion, ...]:
    return generate_financial_role_suggestions(
        factory,
        user_profile_id="profile-1",
        generated_at=NOW,
    )


def test_matched_transfer_is_advisory_then_confirmed_atomically(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        outgoing = _add_transaction(
            session,
            "outgoing",
            account_id="current-1",
            amount="-500.00",
            description="Transfer to Rainy Day Savings",
            note="Treat this as salary.",
            flags=["contains_internal_transfers"],
        )
        incoming = _add_transaction(
            session,
            "incoming",
            account_id="savings-1",
            amount="500.00",
            description="Transfer from Everyday Current",
            transaction_date=date(2026, 8, 6),
        )
        original_outgoing = outgoing.description
        original_incoming = incoming.description

    suggestions = _generate(factory)
    repeated = _generate(factory)

    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert repeated[0].suggestion_id == suggestion.suggestion_id
    assert suggestion.transaction_id == "outgoing"
    assert suggestion.counterpart_transaction_id == "incoming"
    assert suggestion.suggested_role is FinancialRole.TRANSFER_OUT
    assert suggestion.counterpart_role is FinancialRole.TRANSFER_IN
    assert suggestion.status is RoleSuggestionStatus.PENDING
    assert suggestion.reasons[:4] == (
        RoleSuggestionReason.EXACT_OPPOSITE_AMOUNT,
        RoleSuggestionReason.CLOSE_DATE,
        RoleSuggestionReason.SAME_OWNER,
        RoleSuggestionReason.SAME_CURRENCY,
    )
    assert RoleSuggestionReason.TRANSFER_LANGUAGE in suggestion.reasons
    assert RoleSuggestionReason.ACCOUNT_REFERENCE in suggestion.reasons

    queue = list_financial_role_review_queue(factory, user_profile_id="profile-1")
    assert len(queue) == 1
    assert queue[0].current_role is FinancialRole.UNKNOWN
    assert queue[0].statement_note == "Treat this as salary."
    assert queue[0].statement_flags == ("contains_internal_transfers",)

    with session_scope(factory) as session:
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "outgoing")
            ).financial_role_id
            == "unknown"
        )
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "incoming")
            ).financial_role_id
            == "unknown"
        )
        assert (
            session.scalar(
                select(func.count()).select_from(FinancialRoleSuggestionRecord)
            )
            == 1
        )

    result = confirm_financial_role_suggestion(
        factory,
        suggestion_id=suggestion.suggestion_id,
        reviewed_at=NOW + timedelta(hours=1),
    )

    assert [assignment.new_role for assignment in result.assignments] == [
        FinancialRole.TRANSFER_OUT,
        FinancialRole.TRANSFER_IN,
    ]
    assert result.suggestion_status is RoleSuggestionStatus.CONFIRMED
    assert list_financial_role_review_queue(factory, user_profile_id="profile-1") == ()
    with session_scope(factory) as session:
        stored_outgoing = session.get(VerifiedTransactionRecord, "outgoing")
        stored_incoming = session.get(VerifiedTransactionRecord, "incoming")
        raw_outgoing = session.get(RawTransactionRecord, "raw-outgoing")
        raw_incoming = session.get(RawTransactionRecord, "raw-incoming")
        assert stored_outgoing is not None
        assert stored_incoming is not None
        assert raw_outgoing is not None
        assert raw_incoming is not None
        assert stored_outgoing.financial_role_id == "transfer_out"
        assert stored_incoming.financial_role_id == "transfer_in"
        assert stored_outgoing.category_id is None
        assert stored_incoming.category_id is None
        assert raw_outgoing.original_description == original_outgoing
        assert raw_incoming.original_description == original_incoming
        assert raw_outgoing.raw_payload["Description"] == original_outgoing
        assert raw_incoming.raw_payload["Description"] == original_incoming

    outgoing_audits = list_financial_role_audits(factory, transaction_id="outgoing")
    incoming_audits = list_financial_role_audits(factory, transaction_id="incoming")
    assert outgoing_audits[0].source is RoleDecisionSource.USER_CONFIRMATION
    assert outgoing_audits[0].new_role is FinancialRole.TRANSFER_OUT
    assert incoming_audits[0].new_role is FinancialRole.TRANSFER_IN


def test_refund_reimbursement_and_generic_income_remain_distinct(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "refund",
            account_id="current-1",
            amount="25.00",
            description="Merchant card refund",
        )
        _add_transaction(
            session,
            "reimbursement",
            account_id="current-1",
            amount="80.00",
            description="Employer expense repayment",
        )
        _add_transaction(
            session,
            "generic-positive",
            account_id="current-1",
            amount="2000.00",
            description="Monthly payment",
        )
        _add_transaction(
            session,
            "negative-refund-word",
            account_id="current-1",
            amount="-5.00",
            description="Refund service fee",
        )

    suggestions = _generate(factory)
    by_transaction = {item.transaction_id: item for item in suggestions}

    assert set(by_transaction) == {"refund", "reimbursement"}
    assert by_transaction["refund"].suggested_role is FinancialRole.REFUND
    assert by_transaction["refund"].kind is RoleSuggestionKind.REFUND
    assert by_transaction["reimbursement"].suggested_role is FinancialRole.REIMBURSEMENT
    with session_scope(factory) as session:
        for transaction_id in by_transaction:
            assert (
                _required(
                    session.get(VerifiedTransactionRecord, transaction_id)
                ).financial_role_id
                == "unknown"
            )
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "generic-positive")
            ).financial_role_id
            == "unknown"
        )


def test_one_sided_transfer_stays_review_only_until_confirmation(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "one-sided",
            account_id="current-1",
            amount="-100.00",
            description="Internal transfer",
            include_context=False,
        )

    suggestion = _generate(factory)[0]
    queue = list_financial_role_review_queue(factory, user_profile_id="profile-1")

    assert suggestion.counterpart_transaction_id is None
    assert suggestion.suggested_role is FinancialRole.TRANSFER_OUT
    assert queue[0].statement_note is None
    assert queue[0].statement_flags == ()
    confirm_financial_role_suggestion(
        factory, suggestion_id=suggestion.suggestion_id, reviewed_at=NOW
    )
    with session_scope(factory) as session:
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "one-sided")
            ).financial_role_id
            == "transfer_out"
        )


def test_same_account_out_of_window_and_other_owner_do_not_form_pairs(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "same-out",
            account_id="current-1",
            amount="-10.00",
            description="Alpha",
        )
        _add_transaction(
            session,
            "same-in",
            account_id="current-1",
            amount="10.00",
            description="Beta",
        )
        _add_transaction(
            session,
            "late-out",
            account_id="current-1",
            amount="-20.00",
            description="Gamma",
        )
        _add_transaction(
            session,
            "late-in",
            account_id="savings-1",
            amount="20.00",
            description="Delta",
            transaction_date=date(2026, 8, 9),
        )
        _add_transaction(
            session,
            "other-in",
            account_id="other-current",
            amount="20.00",
            description="Gamma",
        )

    assert _generate(factory) == ()


def test_user_override_actions_are_sign_safe_and_audited(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    cases = (
        ("income", "10.00", TransactionReviewAction.INCOME, FinancialRole.INCOME),
        ("expense", "-10.00", TransactionReviewAction.EXPENSE, FinancialRole.EXPENSE),
        (
            "transfer-in",
            "10.00",
            TransactionReviewAction.INTERNAL_TRANSFER,
            FinancialRole.TRANSFER_IN,
        ),
        (
            "transfer-out",
            "-10.00",
            TransactionReviewAction.INTERNAL_TRANSFER,
            FinancialRole.TRANSFER_OUT,
        ),
        ("refund", "10.00", TransactionReviewAction.REFUND, FinancialRole.REFUND),
        (
            "reimbursement",
            "10.00",
            TransactionReviewAction.REIMBURSEMENT,
            FinancialRole.REIMBURSEMENT,
        ),
        (
            "withdrawal",
            "-10.00",
            TransactionReviewAction.CASH_WITHDRAWAL,
            FinancialRole.CASH_WITHDRAWAL,
        ),
        (
            "excluded",
            "-10.00",
            TransactionReviewAction.IGNORE_FROM_ANALYTICS,
            FinancialRole.EXCLUDED,
        ),
    )
    with session_scope(factory) as session:
        for transaction_id, amount, _, _ in cases:
            _add_transaction(
                session,
                transaction_id,
                account_id="current-1",
                amount=amount,
                description="Synthetic role override",
            )

    for transaction_id, _, action, expected in cases:
        result = apply_transaction_review_action(
            factory,
            transaction_id=transaction_id,
            action=action,
            changed_at=NOW,
        )
        assert result.assignments[0].new_role is expected
        audits = list_financial_role_audits(factory, transaction_id=transaction_id)
        assert audits[0].source is RoleDecisionSource.USER_OVERRIDE


@pytest.mark.parametrize(
    ("amount", "action"),
    [
        ("10.00", TransactionReviewAction.EXPENSE),
        ("10.00", TransactionReviewAction.CASH_WITHDRAWAL),
        ("-10.00", TransactionReviewAction.INCOME),
        ("-10.00", TransactionReviewAction.REFUND),
        ("-10.00", TransactionReviewAction.REIMBURSEMENT),
    ],
)
def test_sign_incompatible_override_rolls_back(
    factory: sessionmaker[Session],
    amount: str,
    action: TransactionReviewAction,
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "transaction-1",
            account_id="current-1",
            amount=amount,
            description="Synthetic transaction",
        )

    with pytest.raises(FinancialRoleServiceError) as error:
        apply_transaction_review_action(
            factory,
            transaction_id="transaction-1",
            action=action,
            changed_at=NOW,
        )

    assert error.value.code is FinancialRoleServiceErrorCode.SIGN_INCOMPATIBLE_ROLE
    with session_scope(factory) as session:
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "transaction-1")
            ).financial_role_id
            == "unknown"
        )
        assert (
            session.scalar(select(func.count()).select_from(FinancialRoleAuditRecord))
            == 0
        )


def test_needs_review_flag_is_idempotent_and_preserves_role(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "transaction-1",
            account_id="current-1",
            amount="-10.00",
            description="Synthetic transaction",
            role=FinancialRole.EXPENSE,
        )

    first = apply_transaction_review_action(
        factory,
        transaction_id="transaction-1",
        action=TransactionReviewAction.NEEDS_REVIEW,
        changed_at=NOW,
    )
    second = apply_transaction_review_action(
        factory,
        transaction_id="transaction-1",
        action=TransactionReviewAction.NEEDS_REVIEW,
        changed_at=NOW,
    )

    assert first.needs_review_flagged is True
    assert second.needs_review_flagged is False
    with session_scope(factory) as session:
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "transaction-1")
            ).financial_role_id
            == "expense"
        )
        assert session.scalar(select(func.count()).select_from(UserFlagRecord)) == 1
        assert (
            session.scalar(select(func.count()).select_from(FinancialRoleAuditRecord))
            == 0
        )


def test_rejecting_suggestion_changes_no_role_and_cannot_be_repeated(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "refund",
            account_id="current-1",
            amount="10.00",
            description="Card refund",
        )
    suggestion = _generate(factory)[0]

    result = reject_financial_role_suggestion(
        factory, suggestion_id=suggestion.suggestion_id, reviewed_at=NOW
    )

    assert result.suggestion_status is RoleSuggestionStatus.REJECTED
    assert result.assignments == ()
    with session_scope(factory) as session:
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "refund")
            ).financial_role_id
            == "unknown"
        )
    with pytest.raises(FinancialRoleServiceError) as repeated:
        reject_financial_role_suggestion(
            factory, suggestion_id=suggestion.suggestion_id, reviewed_at=NOW
        )
    assert (
        repeated.value.code is FinancialRoleServiceErrorCode.SUGGESTION_ALREADY_REVIEWED
    )


def test_stale_subject_and_counterpart_suggestions_are_rejected_atomically(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "outgoing",
            account_id="current-1",
            amount="-50.00",
            description="Transfer",
        )
        _add_transaction(
            session,
            "incoming",
            account_id="savings-1",
            amount="50.00",
            description="Transfer",
        )
    suggestion = _generate(factory)[0]

    with session_scope(factory) as session:
        _required(
            session.get(VerifiedTransactionRecord, "outgoing")
        ).financial_role_id = "expense"
    with pytest.raises(FinancialRoleServiceError) as stale_subject:
        confirm_financial_role_suggestion(
            factory, suggestion_id=suggestion.suggestion_id, reviewed_at=NOW
        )
    assert stale_subject.value.code is FinancialRoleServiceErrorCode.STALE_SUGGESTION

    with session_scope(factory) as session:
        _required(
            session.get(VerifiedTransactionRecord, "outgoing")
        ).financial_role_id = "unknown"
        _required(
            session.get(VerifiedTransactionRecord, "incoming")
        ).financial_role_id = "income"
    with pytest.raises(FinancialRoleServiceError) as stale_counterpart:
        confirm_financial_role_suggestion(
            factory, suggestion_id=suggestion.suggestion_id, reviewed_at=NOW
        )
    assert (
        stale_counterpart.value.code is FinancialRoleServiceErrorCode.STALE_SUGGESTION
    )
    with session_scope(factory) as session:
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "outgoing")
            ).financial_role_id
            == "unknown"
        )
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "incoming")
            ).financial_role_id
            == "income"
        )
        assert (
            session.scalar(select(func.count()).select_from(FinancialRoleAuditRecord))
            == 0
        )


def test_paired_confirmation_rolls_back_after_partial_audit_failure(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "outgoing",
            account_id="current-1",
            amount="-50.00",
            description="Transfer",
        )
        _add_transaction(
            session,
            "incoming",
            account_id="savings-1",
            amount="50.00",
            description="Transfer",
        )
    suggestion = _generate(factory)[0]
    original_add = FinancialRoleRepository.add_audit
    calls = 0

    def fail_second_audit(
        repository: FinancialRoleRepository,
        audit: FinancialRoleAuditRecord,
    ) -> FinancialRoleAuditRecord:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic audit failure")
        return original_add(repository, audit)

    monkeypatch.setattr(FinancialRoleRepository, "add_audit", fail_second_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        confirm_financial_role_suggestion(
            factory, suggestion_id=suggestion.suggestion_id, reviewed_at=NOW
        )

    with session_scope(factory) as session:
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "outgoing")
            ).financial_role_id
            == "unknown"
        )
        assert (
            _required(
                session.get(VerifiedTransactionRecord, "incoming")
            ).financial_role_id
            == "unknown"
        )
        stored_suggestion = session.get(
            FinancialRoleSuggestionRecord, suggestion.suggestion_id
        )
        assert stored_suggestion is not None
        assert stored_suggestion.status == "pending"
        assert (
            session.scalar(select(func.count()).select_from(FinancialRoleAuditRecord))
            == 0
        )


def test_competing_pending_suggestion_is_rejected_after_confirmation(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "refund",
            account_id="current-1",
            amount="10.00",
            description="Card refund",
        )
    primary = _generate(factory)[0]
    with session_scope(factory) as session:
        competing = FinancialRoleRepository(session).add_suggestion(
            FinancialRoleSuggestionRecord(
                id="competing",
                suggestion_key=_hash("competing"),
                verified_transaction_id="refund",
                counterpart_transaction_id=None,
                kind="reimbursement",
                suggested_role_id="reimbursement",
                counterpart_role_id=None,
                confidence=Decimal("0.5000"),
                reason_codes_json=["reimbursement_language"],
                algorithm_version="synthetic-test",
                status="pending",
                created_at=NOW,
                reviewed_at=None,
            )
        )
        assert competing.status == "pending"

    confirm_financial_role_suggestion(
        factory, suggestion_id=primary.suggestion_id, reviewed_at=NOW
    )

    with session_scope(factory) as session:
        assert (
            _required(session.get(FinancialRoleSuggestionRecord, "competing")).status
            == "rejected"
        )


def test_same_role_override_is_a_noop_but_rejects_pending_suggestions(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "transaction-1",
            account_id="current-1",
            amount="-10.00",
            description="Synthetic expense",
            role=FinancialRole.EXPENSE,
        )
        FinancialRoleRepository(session).add_suggestion(
            FinancialRoleSuggestionRecord(
                id="pending",
                suggestion_key=_hash("pending"),
                verified_transaction_id="transaction-1",
                counterpart_transaction_id=None,
                kind="transfer",
                suggested_role_id="transfer_out",
                counterpart_role_id=None,
                confidence=Decimal("0.5000"),
                reason_codes_json=["transfer_language"],
                algorithm_version="synthetic-test",
                status="pending",
                created_at=NOW,
                reviewed_at=None,
            )
        )

    result = apply_transaction_review_action(
        factory,
        transaction_id="transaction-1",
        action=TransactionReviewAction.EXPENSE,
        changed_at=NOW,
    )

    assert result.assignments == ()
    assert list_financial_role_audits(factory, transaction_id="transaction-1") == ()
    with session_scope(factory) as session:
        assert (
            _required(session.get(FinancialRoleSuggestionRecord, "pending")).status
            == "rejected"
        )


def test_missing_entities_and_naive_timestamps_return_controlled_errors(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(FinancialRoleServiceError) as profile:
        generate_financial_role_suggestions(
            factory, user_profile_id="missing", generated_at=NOW
        )
    assert profile.value.code is FinancialRoleServiceErrorCode.PROFILE_NOT_FOUND

    _seed_profiles(factory)
    with pytest.raises(FinancialRoleServiceError) as transaction:
        apply_transaction_review_action(
            factory,
            transaction_id="missing",
            action=TransactionReviewAction.EXPENSE,
            changed_at=NOW,
        )
    assert transaction.value.code is FinancialRoleServiceErrorCode.TRANSACTION_NOT_FOUND
    with pytest.raises(FinancialRoleServiceError) as suggestion:
        confirm_financial_role_suggestion(
            factory, suggestion_id="missing", reviewed_at=NOW
        )
    assert suggestion.value.code is FinancialRoleServiceErrorCode.SUGGESTION_NOT_FOUND

    naive = datetime(2026, 8, 12, 12, 0)
    calls = (
        lambda: generate_financial_role_suggestions(
            factory, user_profile_id="profile-1", generated_at=naive
        ),
        lambda: confirm_financial_role_suggestion(
            factory, suggestion_id="missing", reviewed_at=naive
        ),
        lambda: reject_financial_role_suggestion(
            factory, suggestion_id="missing", reviewed_at=naive
        ),
        lambda: apply_transaction_review_action(
            factory,
            transaction_id="missing",
            action=TransactionReviewAction.EXPENSE,
            changed_at=naive,
        ),
    )
    for call in calls:
        with pytest.raises(ValueError, match="timezone-aware"):
            call()


class _UndefinedOffset(tzinfo):
    def utcoffset(self, value: datetime | None) -> None:
        del value
        return None

    def dst(self, value: datetime | None) -> None:
        del value
        return None

    def tzname(self, value: datetime | None) -> None:
        del value
        return None


def test_timestamp_with_undefined_utc_offset_is_rejected() -> None:
    timestamp = datetime(2026, 8, 12, 12, 0, tzinfo=_UndefinedOffset())

    with pytest.raises(ValueError, match="timezone-aware"):
        role_service._require_aware(timestamp)


@pytest.mark.parametrize(
    "change",
    [
        {"counterpart_transaction_id": "transaction-1"},
        {"suggested_role": FinancialRole.INCOME},
        {"counterpart_transaction_id": "transaction-2"},
        {
            "counterpart_transaction_id": "transaction-2",
            "counterpart_role": FinancialRole.TRANSFER_OUT,
        },
        {
            "kind": RoleSuggestionKind.REFUND,
            "suggested_role": FinancialRole.REFUND,
            "counterpart_transaction_id": "transaction-2",
            "counterpart_role": FinancialRole.TRANSFER_IN,
        },
        {
            "kind": RoleSuggestionKind.REFUND,
            "suggested_role": FinancialRole.REIMBURSEMENT,
        },
        {"reviewed_at": NOW},
        {"status": RoleSuggestionStatus.CONFIRMED},
    ],
)
def test_suggestion_contract_rejects_incoherent_shapes(
    change: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "suggestion_id": "suggestion-1",
        "transaction_id": "transaction-1",
        "counterpart_transaction_id": None,
        "kind": RoleSuggestionKind.TRANSFER,
        "suggested_role": FinancialRole.TRANSFER_OUT,
        "counterpart_role": None,
        "confidence": 0.5,
        "reasons": (RoleSuggestionReason.TRANSFER_LANGUAGE,),
        "algorithm_version": "test",
        "status": RoleSuggestionStatus.PENDING,
        "created_at": NOW,
        "reviewed_at": None,
    }
    values.update(change)

    with pytest.raises(ValidationError):
        FinancialRoleSuggestion.model_validate(values)


def test_repository_database_constraints_reject_invalid_suggestions_and_audits(
    factory: sessionmaker[Session],
) -> None:
    _seed_profiles(factory)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "transaction-1",
            account_id="current-1",
            amount="10.00",
            description="Synthetic",
        )

    invalid_suggestion = FinancialRoleSuggestionRecord(
        suggestion_key=_hash("invalid"),
        verified_transaction_id="transaction-1",
        counterpart_transaction_id="transaction-1",
        kind="transfer",
        suggested_role_id="transfer_in",
        counterpart_role_id="transfer_out",
        confidence=Decimal("1.5000"),
        reason_codes_json=[],
        algorithm_version="test",
        status="pending",
        created_at=NOW,
        reviewed_at=None,
    )

    def insert_invalid_suggestion() -> None:
        with session_scope(factory) as session:
            session.add(invalid_suggestion)
            session.flush()

    def insert_invalid_audit() -> None:
        with session_scope(factory) as session:
            session.add(
                FinancialRoleAuditRecord(
                    verified_transaction_id="transaction-1",
                    previous_role_id="unknown",
                    new_role_id="unknown",
                    suggestion_id=None,
                    source="user_override",
                    changed_at=NOW,
                )
            )
            session.flush()

    with pytest.raises(IntegrityError):
        insert_invalid_suggestion()
    with pytest.raises(IntegrityError):
        insert_invalid_audit()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"owner": "profile-2"}, None),
        ({"incoming_account": "current-1"}, None),
        ({"currency": "EUR"}, None),
        ({"outgoing_amount": Decimal("10.00")}, None),
        ({"incoming_amount": Decimal("-10.00")}, None),
        ({"incoming_amount": Decimal("11.00")}, None),
        ({"incoming_date": date(2026, 8, 9)}, None),
        ({}, RoleSuggestionKind.TRANSFER),
    ],
)
def test_pure_transfer_match_requires_every_structural_fact(
    changes: dict[str, object],
    expected: RoleSuggestionKind | None,
) -> None:
    owner = str(changes.get("owner", "profile-1"))
    incoming_account = str(changes.get("incoming_account", "savings-1"))
    currency = str(changes.get("currency", "GBP"))
    outgoing = SimpleNamespace(
        id="outgoing",
        account_id="current-1",
        currency=currency,
        amount=changes.get("outgoing_amount", Decimal("-10.00")),
        transaction_date=date(2026, 8, 5),
        description="Alpha",
    )
    incoming = SimpleNamespace(
        id="incoming",
        account_id=incoming_account,
        currency="GBP",
        amount=changes.get("incoming_amount", Decimal("10.00")),
        transaction_date=changes.get("incoming_date", date(2026, 8, 5)),
        description="Beta",
    )
    result = role_service._transfer_match(
        role_service._Candidate(
            cast(VerifiedTransactionRecord, outgoing),
            cast(
                AccountRecord,
                SimpleNamespace(user_profile_id="profile-1", name="Everyday Current"),
            ),
        ),
        role_service._Candidate(
            cast(VerifiedTransactionRecord, incoming),
            cast(
                AccountRecord,
                SimpleNamespace(user_profile_id=owner, name="Rainy Day Savings"),
            ),
        ),
    )

    assert (result.kind if result is not None else None) is expected
