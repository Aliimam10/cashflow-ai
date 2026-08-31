"""Human-readable synthetic demonstration for Commit 25."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.anomalies import detect_unusual_transactions
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
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
from cashflow_ai.schemas import AnomalyDetectionPlan, AnomalyDetectionPolicy
from cashflow_ai.schemas.transactions import FinancialRole

_AS_OF = date(2026, 9, 30)
_NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _database() -> sessionmaker[Session]:
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        session.add(
            UserProfileRecord(
                id="synthetic-profile",
                display_name="Fictional User",
                base_currency="GBP",
                timezone="Europe/London",
                created_at=_NOW - timedelta(days=365),
                updated_at=_NOW - timedelta(days=365),
            )
        )
        session.flush()
        session.add(
            AccountRecord(
                id="synthetic-account",
                user_profile_id="synthetic-profile",
                name="Fictional Current Account",
                account_type="current",
                currency="GBP",
                created_at=_NOW - timedelta(days=365),
            )
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
    return factory


def _coverage(factory: sessionmaker[Session], *, sparse: bool) -> None:
    start = date(2026, 9, 20) if sparse else date(2026, 6, 3)
    with session_scope(factory) as session:
        batch = ImportBatchRecord(
            id="synthetic-coverage-batch",
            account_id="synthetic-account",
            source_type="csv",
            source_filename="fictional-coverage.csv",
            file_hash=_digest("fictional-coverage"),
            mime_type="text/csv",
            byte_size=100,
            verification_status="verified",
            imported_at=_NOW - timedelta(days=1),
        )
        session.add(batch)
        context = ImportContextRecord(
            id="synthetic-coverage-context",
            import_batch_id=batch.id,
            flags_json=[],
            note=None,
            created_at=_NOW - timedelta(days=1),
        )
        session.add(context)
        session.flush()
        session.add(
            StatementCoverageRecord(
                id="synthetic-coverage",
                import_context_id=context.id,
                statement_start_date=start,
                statement_end_date=_AS_OF,
                coverage_status="complete",
                missing_periods_json=[],
            )
        )


def _transaction(
    factory: sessionmaker[Session],
    identifier: str,
    *,
    transaction_date: date,
    amount: Decimal,
    merchant: str,
    category: str = "groceries",
    balance_after: Decimal = Decimal("500.00"),
    issues: list[dict[str, str]] | None = None,
) -> None:
    evidence_at = _NOW - timedelta(days=1)
    with session_scope(factory) as session:
        batch = ImportBatchRecord(
            id=f"batch-{identifier}",
            account_id="synthetic-account",
            source_type="csv",
            source_filename=f"fictional-{identifier}.csv",
            file_hash=_digest(f"file-{identifier}"),
            mime_type="text/csv",
            byte_size=100,
            verification_status="verified",
            imported_at=evidence_at,
        )
        session.add(batch)
        raw = RawTransactionRecord(
            id=f"raw-{identifier}",
            import_batch_id=batch.id,
            source_type="csv",
            source_row_number=2,
            page_number=None,
            page_record_number=None,
            raw_payload={"fictional_id": identifier},
            original_date_text=transaction_date.isoformat(),
            original_description=f"Fictional {merchant} purchase",
            original_amount_text=str(amount),
            parser_name="synthetic_demo",
            parser_version="1.0",
            source_fingerprint=_digest(f"source-{identifier}"),
            canonical_fingerprint=_digest(f"canonical-{identifier}"),
            issues_json=issues or [],
            review_status="confirmed",
            created_at=evidence_at,
        )
        session.add(raw)
        session.add(
            VerifiedTransactionRecord(
                id=identifier,
                raw_transaction_id=raw.id,
                account_id="synthetic-account",
                transaction_date=transaction_date,
                posting_date=transaction_date,
                description=f"Fictional {merchant} purchase",
                merchant=merchant,
                amount=amount,
                balance_after=balance_after,
                currency="GBP",
                external_id=None,
                transaction_type="card",
                direction="outflow",
                category_id=category,
                financial_role_id=FinancialRole.EXPENSE.value,
                verified_at=evidence_at,
            )
        )
        session.flush()
        session.add(
            FinancialRoleAuditRecord(
                id=f"role-{identifier}",
                verified_transaction_id=identifier,
                previous_role_id=FinancialRole.UNKNOWN.value,
                new_role_id=FinancialRole.EXPENSE.value,
                source="user_override",
                changed_at=evidence_at,
            )
        )
        session.add(
            CategoryDecisionRecord(
                id=f"category-{identifier}",
                verified_transaction_id=identifier,
                category_id=category,
                source="merchant_mapping",
                status="applied",
                confidence=None,
                model_version=None,
                rule_id="fictional-rule",
                taxonomy_version="1.0",
                rule_set_version="fictional-rules-1",
                reason_code="known_merchant",
                created_at=evidence_at,
                reviewed_at=None,
            )
        )


def _seed(factory: sessionmaker[Session], history_count: int, *, sparse: bool) -> None:
    _coverage(factory, sparse=sparse)
    reference_start = date(2026, 6, 5)
    reference_span = 105
    for index in range(history_count):
        offset = min(reference_span - 1, index * reference_span // history_count)
        _transaction(
            factory,
            f"history-{index:03d}",
            transaction_date=reference_start + timedelta(days=offset),
            amount=-(Decimal("10.00") + Decimal(index) / Decimal("9")),
            merchant=("Fictional Grocer" if index % 2 == 0 else "Fictional Transit"),
        )
    reviewed_at = _NOW - timedelta(days=20)
    with session_scope(factory) as session:
        session.add(
            RecurringPaymentCandidateRecord(
                id="fictional-rent-series",
                account_id="synthetic-account",
                recurring_series_id=None,
                merchant_group="Fictional Rent",
                currency="GBP",
                direction="outflow",
                financial_role_id=FinancialRole.EXPENSE.value,
                expected_amount=Decimal("-50.00"),
                frequency="monthly",
                interval_days=30,
                next_expected_date=_AS_OF + timedelta(days=30),
                confidence=Decimal("0.95"),
                covered_missed_count=0,
                status="confirmed",
                detected_at=reviewed_at - timedelta(days=1),
                evidence_as_of_date=reviewed_at.date() - timedelta(days=1),
                knowledge_cutoff_at=reviewed_at - timedelta(days=1),
                reviewed_at=reviewed_at,
            )
        )
    _transaction(
        factory,
        "fictional-large",
        transaction_date=date(2026, 9, 24),
        amount=Decimal("-450.00"),
        merchant="Fictional New Electronics",
    )
    _transaction(
        factory,
        "fictional-rent",
        transaction_date=date(2026, 9, 25),
        amount=Decimal("-50.00"),
        merchant="Fictional Rent",
        category="housing",
    )
    _transaction(
        factory,
        "fictional-negative-balance",
        transaction_date=date(2026, 9, 26),
        amount=Decimal("-30.00"),
        merchant="Fictional Grocer",
        balance_after=Decimal("-8.00"),
    )
    _transaction(
        factory,
        "fictional-duplicate",
        transaction_date=date(2026, 9, 27),
        amount=Decimal("-18.00"),
        merchant="Fictional Grocer",
        issues=[{"code": "probable_duplicate"}],
    )


def main() -> None:
    """Print careful anomaly review results from local fictional records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-transactions", type=int, default=30)
    parser.add_argument(
        "--sparse",
        action="store_true",
        help="show safe rules-only behaviour when coverage is inadequate",
    )
    args = parser.parse_args()
    if not 20 <= args.history_transactions <= 200:
        parser.error("--history-transactions must be from 20 through 200")

    factory = _database()
    _seed(factory, args.history_transactions, sparse=args.sparse)
    plan = AnomalyDetectionPlan(
        user_profile_id="synthetic-profile",
        account_ids=("synthetic-account",),
        as_of_date=_AS_OF,
        knowledge_cutoff_at=_NOW,
        policy=AnomalyDetectionPolicy(
            history_lookback_days=120,
            detection_window_days=10,
            minimum_covered_days=60,
            minimum_coverage_ratio=0.5,
            minimum_history_transactions=20,
            isolation_estimators=50,
            isolation_contamination=0.1,
            random_seed=7,
        ),
    )
    result = detect_unusual_transactions(factory, plan=plan)
    print("CashFlow AI fictional anomaly review")
    print(f"detection mode: {result.mode.value}")
    print(f"verified records in window: {result.verified_transaction_count}")
    print(f"model reference records: {result.reference_transaction_count}")
    print(f"model-scored current records: {result.scored_transaction_count}")
    print(f"review items: {len(result.alerts)}")
    for alert in result.alerts:
        reasons = ", ".join(signal.code.value for signal in alert.signals)
        print(f"- {alert.label.value}: {alert.transaction_id} ({reasons})")
    warnings = ", ".join(item.value for item in result.warnings) or "none"
    print(f"warnings: {warnings}")
    protected = all(item.transaction_id != "fictional-rent" for item in result.alerts)
    print(f"known recurring rent protected: {'yes' if protected else 'no'}")
    print("These review signals are not confirmed fraud.")


if __name__ == "__main__":  # pragma: no cover - console entry point
    main()
