"""Tests for SQLite sessions, repositories, constraints, and rollback."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence import (
    AccountRepository,
    Base,
    ImportBatchRepository,
    TransactionRepository,
    UserProfileRepository,
    UTCDateTime,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.base import new_id, utc_now
from cashflow_ai.persistence.models import (
    AccountRecord,
    FinancialRoleRecord,
    ImportBatchRecord,
    RawTransactionRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)

FILE_HASH = "a" * 64
SOURCE_FINGERPRINT = "b" * 64
CANONICAL_FINGERPRINT = "c" * 64


@pytest.fixture
def engine() -> Engine:
    database_engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    return database_engine


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def profile_record(*, profile_id: str = "profile-1") -> UserProfileRecord:
    return UserProfileRecord(
        id=profile_id,
        display_name="Local User",
        base_currency="GBP",
        timezone="Europe/London",
    )


def account_record(
    *,
    account_id: str = "account-1",
    profile_id: str = "profile-1",
    name: str = "Everyday",
) -> AccountRecord:
    return AccountRecord(
        id=account_id,
        user_profile_id=profile_id,
        name=name,
        account_type="current",
        currency="GBP",
        institution_label="Example Bank",
    )


def batch_record(
    *,
    batch_id: str = "batch-1",
    account_id: str = "account-1",
    file_hash: str = FILE_HASH,
) -> ImportBatchRecord:
    return ImportBatchRecord(
        id=batch_id,
        account_id=account_id,
        source_type="csv",
        source_filename="statement.csv",
        file_hash=file_hash,
        mime_type="text/csv",
        byte_size=100,
        verification_status="verified",
    )


def raw_record(
    *,
    raw_id: str = "raw-1",
    batch_id: str = "batch-1",
    source_fingerprint: str = SOURCE_FINGERPRINT,
) -> RawTransactionRecord:
    return RawTransactionRecord(
        id=raw_id,
        import_batch_id=batch_id,
        source_type="csv",
        source_row_number=2,
        page_number=None,
        page_record_number=None,
        raw_payload={"Date": "2026-07-04", "Amount": "-12.50"},
        original_date_text="2026-07-04",
        original_description="Example Shop",
        original_amount_text="-12.50",
        parser_name="cashflow_transaction_normaliser",
        parser_version="1.0.0",
        source_fingerprint=source_fingerprint,
        canonical_fingerprint=CANONICAL_FINGERPRINT,
        review_status="confirmed",
    )


def verified_record(
    *,
    verified_id: str = "verified-1",
    raw_id: str = "raw-1",
    account_id: str = "account-1",
    amount: Decimal = Decimal("-12.50"),
    direction: str = "outflow",
) -> VerifiedTransactionRecord:
    return VerifiedTransactionRecord(
        id=verified_id,
        raw_transaction_id=raw_id,
        account_id=account_id,
        transaction_date=date(2026, 7, 4),
        posting_date=None,
        description="Example Shop",
        merchant="Example Shop",
        amount=amount,
        balance_after=Decimal("987.50"),
        currency="GBP",
        external_id="bank-transaction-1",
        transaction_type="card",
        direction=direction,
        category_id=None,
        financial_role_id="unknown",
    )


def seed_transaction_parents(session: Session) -> None:
    UserProfileRepository(session).add(profile_record())
    AccountRepository(session).add(account_record())
    session.add(FinancialRoleRecord(id="unknown", name="Unknown"))
    session.flush()
    ImportBatchRepository(session).add(batch_record())


def test_repositories_store_and_read_profiles_accounts_and_batches(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        profile_repository = UserProfileRepository(session)
        account_repository = AccountRepository(session)
        batch_repository = ImportBatchRepository(session)
        profile = profile_repository.add(profile_record())
        savings = account_repository.add(
            account_record(account_id="account-2", name="Savings")
        )
        everyday = account_repository.add(account_record())
        batch = batch_repository.add(batch_record())

        assert profile_repository.get(profile.id) is profile
        assert profile_repository.get("missing") is None
        assert account_repository.get(everyday.id) is everyday
        assert account_repository.get("missing") is None
        assert account_repository.list_for_user(profile.id) == (everyday, savings)
        assert batch_repository.get(batch.id) is batch
        assert batch_repository.get("missing") is None
        assert batch_repository.get_by_file_hash(everyday.id, FILE_HASH) is batch
        assert batch_repository.get_by_file_hash(everyday.id, "d" * 64) is None

    with session_scope(factory) as session:
        stored = AccountRepository(session).get("account-1")
        assert stored is not None
        assert stored.created_at.tzinfo is UTC


def test_raw_and_verified_transactions_remain_separate_and_decimal_safe(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        seed_transaction_parents(session)
        repository = TransactionRepository(session)
        raw = repository.add_raw(raw_record())
        verified = repository.add_verified(verified_record())

        assert repository.get_raw_by_source_fingerprint(SOURCE_FINGERPRINT) is raw
        assert repository.get_raw_by_source_fingerprint("d" * 64) is None
        assert repository.list_verified_for_account("missing") == ()
        assert repository.list_verified_for_account("account-1") == (verified,)

    with session_scope(factory) as session:
        stored = TransactionRepository(session).list_verified_for_account("account-1")
        assert stored[0].amount == Decimal("-12.50")
        assert stored[0].balance_after == Decimal("987.50")
        assert stored[0].verified_at.tzinfo is UTC


def test_duplicate_constraint_rolls_back_the_entire_unit_of_work(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        UserProfileRepository(session).add(profile_record())

    def insert_duplicate_accounts() -> None:
        with session_scope(factory) as session:
            repository = AccountRepository(session)
            repository.add(account_record(account_id="account-1"))
            repository.add(account_record(account_id="account-2"))

    with pytest.raises(IntegrityError):
        insert_duplicate_accounts()

    with session_scope(factory) as session:
        assert AccountRepository(session).list_for_user("profile-1") == ()


def test_import_file_hash_and_source_fingerprint_are_unique(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        UserProfileRepository(session).add(profile_record())
        AccountRepository(session).add(account_record())
        ImportBatchRepository(session).add(batch_record())

    with pytest.raises(IntegrityError), session_scope(factory) as session:
        ImportBatchRepository(session).add(batch_record(batch_id="batch-2"))

    with session_scope(factory) as session:
        TransactionRepository(session).add_raw(raw_record())

    with pytest.raises(IntegrityError), session_scope(factory) as session:
        TransactionRepository(session).add_raw(
            raw_record(raw_id="raw-2", source_fingerprint=SOURCE_FINGERPRINT)
        )


def test_foreign_keys_are_enforced_and_parent_delete_cascades(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(IntegrityError), session_scope(factory) as session:
        AccountRepository(session).add(account_record(profile_id="missing"))

    with session_scope(factory) as session:
        UserProfileRepository(session).add(profile_record())
        AccountRepository(session).add(account_record())

    with session_scope(factory) as session:
        profile = UserProfileRepository(session).get("profile-1")
        assert profile is not None
        session.delete(profile)

    with session_scope(factory) as session:
        assert AccountRepository(session).get("account-1") is None


def test_signed_direction_database_constraint_rejects_invalid_money(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        seed_transaction_parents(session)
        TransactionRepository(session).add_raw(raw_record())

    with pytest.raises(IntegrityError), session_scope(factory) as session:
        TransactionRepository(session).add_verified(
            verified_record(amount=Decimal("12.50"), direction="outflow")
        )

    with session_scope(factory) as session:
        count = session.scalar(
            select(func.count()).select_from(VerifiedTransactionRecord)
        )
        assert count == 0


def test_utc_datetime_rejects_naive_values_and_handles_nullable_values(
    engine: Engine,
) -> None:
    column_type = UTCDateTime()
    aware = datetime(2026, 7, 4, 13, 0, tzinfo=UTC) + timedelta(hours=1)

    stored = column_type.process_bind_param(aware, engine.dialect)
    restored = column_type.process_result_value(stored, engine.dialect)

    assert stored == datetime(2026, 7, 4, 14, 0)
    assert restored == datetime(2026, 7, 4, 14, 0, tzinfo=UTC)
    assert column_type.process_bind_param(None, engine.dialect) is None
    assert column_type.process_result_value(None, engine.dialect) is None
    with pytest.raises(ValueError, match="timezone-aware"):
        column_type.process_bind_param(datetime(2026, 7, 4), engine.dialect)


def test_engine_rejects_non_sqlite_urls() -> None:
    with pytest.raises(ValueError, match="SQLite"):
        create_sqlite_engine("postgresql://localhost/cashflow")


def test_database_identifiers_and_default_time_are_utc() -> None:
    assert UUID(new_id()).version == 4
    assert utc_now().tzinfo is UTC


def test_delete_statement_can_clear_seed_rows(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        session.add(FinancialRoleRecord(id="unknown", name="Unknown"))

    with session_scope(factory) as session:
        session.execute(delete(FinancialRoleRecord))

    with session_scope(factory) as session:
        assert session.get(FinancialRoleRecord, "unknown") is None
