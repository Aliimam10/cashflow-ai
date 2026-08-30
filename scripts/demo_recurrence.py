"""Run a privacy-safe recurring-payment lifecycle with fictional local data."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence import (
    Base,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import (
    AccountRecord,
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
from cashflow_ai.recurrence import detect_recurring_payments, review_recurring_payment
from cashflow_ai.schemas import (
    FinancialRole,
    RecurrenceDetectionPolicy,
    RecurrenceReview,
    RecurrenceReviewAction,
)

_PROFILE_ID = "synthetic-profile"
_ACCOUNT_ID = "synthetic-account"
_EVIDENCE_TIME = datetime(2025, 5, 2, tzinfo=UTC)


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _add_transaction(
    factory: sessionmaker[Session],
    *,
    transaction_id: str,
    transaction_date: date,
    amount: Decimal,
) -> None:
    with session_scope(factory) as session:
        batch_id = f"batch-{transaction_id}"
        session.add(
            ImportBatchRecord(
                id=batch_id,
                account_id=_ACCOUNT_ID,
                source_type="csv",
                source_filename=f"{transaction_id}.csv",
                file_hash=_digest(f"file-{transaction_id}"),
                mime_type="text/csv",
                byte_size=100,
                verification_status="verified",
                imported_at=_EVIDENCE_TIME,
            )
        )
        session.add(
            ImportContextRecord(
                id=f"context-{transaction_id}",
                import_batch_id=batch_id,
                flags_json=["synthetic_demo"],
                note=None,
                created_at=_EVIDENCE_TIME,
            )
        )
        raw_id = f"raw-{transaction_id}"
        session.add(
            RawTransactionRecord(
                id=raw_id,
                import_batch_id=batch_id,
                source_type="csv",
                source_row_number=2,
                page_number=None,
                page_record_number=None,
                raw_payload={"synthetic": True},
                original_date_text=transaction_date.isoformat(),
                original_description="Synthetic Utility",
                original_amount_text=str(amount),
                parser_name="synthetic_demo",
                parser_version="1.0",
                source_fingerprint=_digest(f"source-{transaction_id}"),
                canonical_fingerprint=_digest(f"canonical-{transaction_id}"),
                issues_json=[],
                review_status="confirmed",
                created_at=_EVIDENCE_TIME,
            )
        )
        session.add(
            VerifiedTransactionRecord(
                id=transaction_id,
                raw_transaction_id=raw_id,
                account_id=_ACCOUNT_ID,
                transaction_date=transaction_date,
                posting_date=transaction_date,
                description="Synthetic Utility",
                merchant="Synthetic Utility",
                amount=amount,
                balance_after=None,
                currency="GBP",
                external_id=f"external-{transaction_id}",
                transaction_type="synthetic",
                direction="outflow",
                category_id=None,
                financial_role_id=FinancialRole.EXPENSE.value,
                verified_at=_EVIDENCE_TIME,
            )
        )
        session.flush()
        session.add(
            FinancialRoleAuditRecord(
                id=f"role-{transaction_id}",
                verified_transaction_id=transaction_id,
                previous_role_id=FinancialRole.UNKNOWN.value,
                new_role_id=FinancialRole.EXPENSE.value,
                suggestion_id=None,
                source="user_override",
                changed_at=_EVIDENCE_TIME,
            )
        )


def main() -> None:
    """Print detection, confirmation, and refresh results from fictional data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amount", type=Decimal, default=Decimal("-24.00"))
    args = parser.parse_args()
    if args.amount >= 0:
        parser.error("--amount must be negative for this outgoing-payment demo")

    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        session.add(
            UserProfileRecord(
                id=_PROFILE_ID,
                display_name="Synthetic User",
                base_currency="GBP",
                timezone="UTC",
            )
        )
        session.flush()
        session.add(
            AccountRecord(
                id=_ACCOUNT_ID,
                user_profile_id=_PROFILE_ID,
                name="Synthetic Current",
                account_type="current",
                currency="GBP",
            )
        )
        session.add_all(
            (
                FinancialRoleRecord(id="unknown", name="Unknown"),
                FinancialRoleRecord(id="expense", name="Expense"),
            )
        )

    for index, transaction_date in enumerate(
        (date(2025, 1, 31), date(2025, 2, 28), date(2025, 3, 31))
    ):
        _add_transaction(
            factory,
            transaction_id=f"utility-{index}",
            transaction_date=transaction_date,
            amount=args.amount,
        )
    with session_scope(factory) as session:
        session.add(
            StatementCoverageRecord(
                id="synthetic-coverage",
                import_context_id="context-utility-0",
                statement_start_date=date(2025, 1, 1),
                statement_end_date=date(2025, 5, 31),
                coverage_status="complete",
                missing_periods_json=[],
            )
        )

    policy = RecurrenceDetectionPolicy(
        minimum_occurrences=3,
        maximum_amount_variation=Decimal("0.50"),
        maximum_interval_variation_days=2,
        maximum_skipped_occurrences=1,
        minimum_confidence=0.5,
    )
    cutoff = datetime.now(UTC)
    candidate = detect_recurring_payments(
        factory,
        user_profile_id=_PROFILE_ID,
        as_of_date=date(2025, 4, 2),
        knowledge_cutoff_at=cutoff,
        policy=policy,
    )[0]
    confirmation = review_recurring_payment(
        factory,
        review=RecurrenceReview(
            user_profile_id=_PROFILE_ID,
            candidate_id=candidate.candidate_id,
            action=RecurrenceReviewAction.CONFIRM,
            reviewed_at=cutoff,
        ),
    )
    with session_scope(factory) as session:
        stored_candidate = session.get(
            RecurringPaymentCandidateRecord, candidate.candidate_id
        )
        assert stored_candidate is not None
        assert stored_candidate.reviewed_at is not None
        refresh_cutoff = stored_candidate.reviewed_at
    _add_transaction(
        factory,
        transaction_id="utility-3",
        transaction_date=date(2025, 4, 30),
        amount=args.amount,
    )
    refreshed = detect_recurring_payments(
        factory,
        user_profile_id=_PROFILE_ID,
        as_of_date=date(2025, 5, 1),
        knowledge_cutoff_at=refresh_cutoff,
        policy=policy,
    )[0]

    print("CashFlow AI synthetic recurrence check")
    print(f"merchant group: {candidate.merchant_group}")
    print(f"frequency: {candidate.frequency.value}")
    print(f"first predicted date: {candidate.next_expected_date.isoformat()}")
    print(f"review status: {confirmation.status.value}")
    print(f"occurrences after refresh: {len(refreshed.occurrence_dates)}")
    print(f"refreshed predicted date: {refreshed.next_expected_date.isoformat()}")
    print(f"covered misses: {refreshed.covered_missed_count}")


if __name__ == "__main__":
    main()
