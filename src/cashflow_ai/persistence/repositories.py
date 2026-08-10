"""Focused SQLAlchemy repositories for current persistence boundaries."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from cashflow_ai.persistence.models import (
    AccountRecord,
    BalanceSnapshotRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)


class UserProfileRepository:
    """Store and retrieve the local user profile."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to one transaction-scoped session."""
        self._session = session

    def add(self, profile: UserProfileRecord) -> UserProfileRecord:
        """Stage and flush a profile."""
        self._session.add(profile)
        self._session.flush()
        return profile

    def get(self, profile_id: str) -> UserProfileRecord | None:
        """Return a profile by ID when it exists."""
        return self._session.get(UserProfileRecord, profile_id)


class AccountRepository:
    """Store and query supported cash accounts."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to one transaction-scoped session."""
        self._session = session

    def add(self, account: AccountRecord) -> AccountRecord:
        """Stage and flush an account."""
        self._session.add(account)
        self._session.flush()
        return account

    def get(self, account_id: str) -> AccountRecord | None:
        """Return an account by ID when it exists."""
        return self._session.get(AccountRecord, account_id)

    def list_for_user(self, user_profile_id: str) -> tuple[AccountRecord, ...]:
        """Return a user's accounts in stable name order."""
        statement = (
            select(AccountRecord)
            .where(AccountRecord.user_profile_id == user_profile_id)
            .order_by(AccountRecord.name, AccountRecord.id)
        )
        return tuple(self._session.scalars(statement))


class ImportBatchRepository:
    """Store import batches and locate previously uploaded files."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to one transaction-scoped session."""
        self._session = session

    def add(self, batch: ImportBatchRecord) -> ImportBatchRecord:
        """Stage and flush an import batch."""
        self._session.add(batch)
        self._session.flush()
        return batch

    def get(self, batch_id: str) -> ImportBatchRecord | None:
        """Return an import batch by ID when it exists."""
        return self._session.get(ImportBatchRecord, batch_id)

    def get_by_file_hash(
        self,
        account_id: str,
        file_hash: str,
    ) -> ImportBatchRecord | None:
        """Return a prior upload with the same account and byte hash."""
        statement = select(ImportBatchRecord).where(
            ImportBatchRecord.account_id == account_id,
            ImportBatchRecord.file_hash == file_hash,
        )
        return self._session.scalar(statement)


class TransactionRepository:
    """Keep raw evidence separate from verified transaction records."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to one transaction-scoped session."""
        self._session = session

    def add_raw(self, transaction: RawTransactionRecord) -> RawTransactionRecord:
        """Stage and flush one auditable source record."""
        self._session.add(transaction)
        self._session.flush()
        return transaction

    def add_verified(
        self,
        transaction: VerifiedTransactionRecord,
    ) -> VerifiedTransactionRecord:
        """Stage and flush one reviewed transaction."""
        self._session.add(transaction)
        self._session.flush()
        return transaction

    def get_raw_by_source_fingerprint(
        self,
        source_fingerprint: str,
    ) -> RawTransactionRecord | None:
        """Find the exact source record used for deterministic deduplication."""
        statement = select(RawTransactionRecord).where(
            RawTransactionRecord.source_fingerprint == source_fingerprint
        )
        return self._session.scalar(statement)

    def list_verified_for_account(
        self,
        account_id: str,
    ) -> tuple[VerifiedTransactionRecord, ...]:
        """Return verified rows chronologically with deterministic tie-breaking."""
        statement = (
            select(VerifiedTransactionRecord)
            .where(VerifiedTransactionRecord.account_id == account_id)
            .order_by(
                VerifiedTransactionRecord.transaction_date,
                VerifiedTransactionRecord.id,
            )
        )
        return tuple(self._session.scalars(statement))

    def list_raw_for_batch(
        self,
        import_batch_id: str,
    ) -> tuple[RawTransactionRecord, ...]:
        """Return all preserved rows for one document in source order."""
        statement = (
            select(RawTransactionRecord)
            .where(RawTransactionRecord.import_batch_id == import_batch_id)
            .order_by(RawTransactionRecord.source_row_number, RawTransactionRecord.id)
        )
        return tuple(self._session.scalars(statement))

    def list_duplicate_candidates(
        self,
        *,
        account_id: str,
        transaction_date: date,
        external_id: str | None,
    ) -> tuple[tuple[VerifiedTransactionRecord, RawTransactionRecord], ...]:
        """Find nearby or same-ID verified rows suitable for duplicate scoring."""
        nearby = VerifiedTransactionRecord.transaction_date.between(
            transaction_date - timedelta(days=2),
            transaction_date + timedelta(days=2),
        )
        candidate_filter: ColumnElement[bool] = nearby
        if external_id is not None:
            candidate_filter = or_(
                nearby,
                func.lower(VerifiedTransactionRecord.external_id)
                == external_id.casefold(),
            )
        statement = (
            select(VerifiedTransactionRecord, RawTransactionRecord)
            .join(
                RawTransactionRecord,
                RawTransactionRecord.id == VerifiedTransactionRecord.raw_transaction_id,
            )
            .where(
                VerifiedTransactionRecord.account_id == account_id,
                candidate_filter,
            )
            .order_by(
                VerifiedTransactionRecord.transaction_date,
                VerifiedTransactionRecord.id,
            )
        )
        return tuple(self._session.execute(statement).tuples())


class StatementRepository:
    """Persist inert statement context, coverage, and balance evidence."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to one transaction-scoped session."""
        self._session = session

    def add_context(self, context: ImportContextRecord) -> ImportContextRecord:
        """Stage and flush one statement context."""
        self._session.add(context)
        self._session.flush()
        return context

    def add_coverage(
        self,
        coverage: StatementCoverageRecord,
    ) -> StatementCoverageRecord:
        """Stage and flush one statement coverage record."""
        self._session.add(coverage)
        self._session.flush()
        return coverage

    def add_balance(self, balance: BalanceSnapshotRecord) -> BalanceSnapshotRecord:
        """Stage and flush one statement balance snapshot."""
        self._session.add(balance)
        self._session.flush()
        return balance

    def list_coverages_for_account(
        self,
        account_id: str,
        *,
        exclude_batch_id: str | None = None,
    ) -> tuple[StatementCoverageRecord, ...]:
        """Return prior coverage for an account in chronological order."""
        statement = (
            select(StatementCoverageRecord)
            .join(
                ImportContextRecord,
                ImportContextRecord.id == StatementCoverageRecord.import_context_id,
            )
            .join(
                ImportBatchRecord,
                ImportBatchRecord.id == ImportContextRecord.import_batch_id,
            )
            .where(ImportBatchRecord.account_id == account_id)
        )
        if exclude_batch_id is not None:
            statement = statement.where(ImportBatchRecord.id != exclude_batch_id)
        statement = statement.order_by(
            StatementCoverageRecord.statement_start_date,
            StatementCoverageRecord.statement_end_date,
            StatementCoverageRecord.id,
        )
        return tuple(self._session.scalars(statement))

    def get_coverage_for_batch(
        self,
        import_batch_id: str,
    ) -> StatementCoverageRecord | None:
        """Return the statement coverage belonging to one import batch."""
        statement = (
            select(StatementCoverageRecord)
            .join(
                ImportContextRecord,
                ImportContextRecord.id == StatementCoverageRecord.import_context_id,
            )
            .where(ImportContextRecord.import_batch_id == import_batch_id)
        )
        return self._session.scalar(statement)
