"""Focused SQLAlchemy repositories for current persistence boundaries."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement

from cashflow_ai.persistence.models import (
    AccountRecord,
    BalanceSnapshotRecord,
    CategoryCorrectionRecord,
    CategoryRecord,
    FinancialRoleAuditRecord,
    FinancialRoleSuggestionRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    StatementCoverageRecord,
    UserFlagRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)

type AnalyticsTransactionRow = tuple[
    VerifiedTransactionRecord,
    CategoryRecord | None,
]
type AnalyticsCoverageRow = tuple[str, StatementCoverageRecord]
type ConfirmedTransferRow = tuple[
    FinancialRoleSuggestionRecord,
    VerifiedTransactionRecord,
    VerifiedTransactionRecord,
]


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

    def latest_verified_date(
        self,
        account_id: str,
        *,
        as_of_date: date,
    ) -> date | None:
        """Return the latest trusted transaction date up to an inclusive cutoff."""
        statement = select(func.max(VerifiedTransactionRecord.transaction_date)).where(
            VerifiedTransactionRecord.account_id == account_id,
            VerifiedTransactionRecord.transaction_date <= as_of_date,
        )
        return self._session.scalar(statement)

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
        return BalanceSnapshotRepository(self._session).add(balance)

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

    def list_verified_coverages_for_account(
        self,
        account_id: str,
        *,
        as_of_date: date,
    ) -> tuple[StatementCoverageRecord, ...]:
        """Return fully elapsed coverage belonging to verified import batches."""
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
            .where(
                ImportBatchRecord.account_id == account_id,
                ImportBatchRecord.verification_status == "verified",
                StatementCoverageRecord.statement_end_date <= as_of_date,
            )
            .order_by(
                StatementCoverageRecord.statement_start_date,
                StatementCoverageRecord.statement_end_date,
                StatementCoverageRecord.id,
            )
        )
        return tuple(self._session.scalars(statement))


class BalanceSnapshotRepository:
    """Store and select balance evidence without creating transactions."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to one transaction-scoped session."""
        self._session = session

    def add(self, balance: BalanceSnapshotRecord) -> BalanceSnapshotRecord:
        """Stage and flush one balance observation."""
        self._session.add(balance)
        self._session.flush()
        return balance

    def latest_verified_for_account(
        self,
        account_id: str,
        *,
        as_of_date: date,
    ) -> BalanceSnapshotRecord | None:
        """Select the latest eligible balance using a deterministic source policy."""
        source_priority = case(
            (BalanceSnapshotRecord.source == "manual", 4),
            (BalanceSnapshotRecord.source == "statement_closing", 3),
            (BalanceSnapshotRecord.source == "running_balance", 2),
            (BalanceSnapshotRecord.source == "statement_opening", 1),
            else_=0,
        )
        statement = (
            select(BalanceSnapshotRecord)
            .where(
                BalanceSnapshotRecord.account_id == account_id,
                BalanceSnapshotRecord.verification_status == "verified",
                BalanceSnapshotRecord.as_of_date <= as_of_date,
            )
            .order_by(
                desc(BalanceSnapshotRecord.as_of_date),
                desc(source_priority),
                desc(BalanceSnapshotRecord.recorded_at),
                desc(BalanceSnapshotRecord.id),
            )
            .limit(1)
        )
        return self._session.scalar(statement)


class AnalyticsRepository:
    """Read trusted persisted evidence for coverage-aware analytics."""

    def __init__(self, session: Session) -> None:
        """Bind repository reads to one transaction-scoped session."""
        self._session = session

    def list_owned_accounts(
        self,
        user_profile_id: str,
        account_ids: tuple[str, ...],
    ) -> tuple[AccountRecord, ...]:
        """Return selected accounts owned by the local profile in stable order."""
        statement = (
            select(AccountRecord)
            .where(
                AccountRecord.user_profile_id == user_profile_id,
                AccountRecord.id.in_(account_ids),
            )
            .order_by(AccountRecord.id)
        )
        return tuple(self._session.scalars(statement))

    def list_transactions(
        self,
        *,
        user_profile_id: str,
        account_ids: tuple[str, ...],
        start_date: date,
        end_date: date,
    ) -> tuple[AnalyticsTransactionRow, ...]:
        """Return accepted transactions and optional categories in the range."""
        statement = (
            select(VerifiedTransactionRecord, CategoryRecord)
            .join(
                AccountRecord,
                AccountRecord.id == VerifiedTransactionRecord.account_id,
            )
            .outerjoin(
                CategoryRecord,
                CategoryRecord.id == VerifiedTransactionRecord.category_id,
            )
            .where(
                AccountRecord.user_profile_id == user_profile_id,
                VerifiedTransactionRecord.account_id.in_(account_ids),
                VerifiedTransactionRecord.transaction_date.between(
                    start_date,
                    end_date,
                ),
            )
            .order_by(
                VerifiedTransactionRecord.transaction_date,
                VerifiedTransactionRecord.account_id,
                VerifiedTransactionRecord.id,
            )
        )
        return tuple(self._session.execute(statement).tuples())

    def list_verified_coverages(
        self,
        *,
        user_profile_id: str,
        account_ids: tuple[str, ...],
        start_date: date,
        end_date: date,
    ) -> tuple[AnalyticsCoverageRow, ...]:
        """Return verified statement coverage intersecting the requested range."""
        statement = (
            select(ImportBatchRecord.account_id, StatementCoverageRecord)
            .join(
                ImportContextRecord,
                ImportContextRecord.id == StatementCoverageRecord.import_context_id,
            )
            .join(
                ImportBatchRecord,
                ImportBatchRecord.id == ImportContextRecord.import_batch_id,
            )
            .join(AccountRecord, AccountRecord.id == ImportBatchRecord.account_id)
            .where(
                AccountRecord.user_profile_id == user_profile_id,
                ImportBatchRecord.account_id.in_(account_ids),
                ImportBatchRecord.verification_status == "verified",
                StatementCoverageRecord.statement_end_date >= start_date,
                StatementCoverageRecord.statement_start_date <= end_date,
            )
            .order_by(
                ImportBatchRecord.account_id,
                StatementCoverageRecord.statement_start_date,
                StatementCoverageRecord.statement_end_date,
                StatementCoverageRecord.id,
            )
        )
        return tuple(self._session.execute(statement).tuples())

    def list_verified_balances(
        self,
        *,
        user_profile_id: str,
        account_ids: tuple[str, ...],
        start_date: date,
        end_date: date,
    ) -> tuple[BalanceSnapshotRecord, ...]:
        """Return verified balance evidence using deterministic same-day priority."""
        source_priority = case(
            (BalanceSnapshotRecord.source == "manual", 4),
            (BalanceSnapshotRecord.source == "statement_closing", 3),
            (BalanceSnapshotRecord.source == "running_balance", 2),
            (BalanceSnapshotRecord.source == "statement_opening", 1),
            else_=0,
        )
        statement = (
            select(BalanceSnapshotRecord)
            .join(AccountRecord, AccountRecord.id == BalanceSnapshotRecord.account_id)
            .where(
                AccountRecord.user_profile_id == user_profile_id,
                BalanceSnapshotRecord.account_id.in_(account_ids),
                BalanceSnapshotRecord.verification_status == "verified",
                BalanceSnapshotRecord.as_of_date.between(start_date, end_date),
            )
            .order_by(
                BalanceSnapshotRecord.account_id,
                BalanceSnapshotRecord.as_of_date,
                desc(source_priority),
                desc(BalanceSnapshotRecord.recorded_at),
                desc(BalanceSnapshotRecord.id),
            )
        )
        return tuple(self._session.scalars(statement))

    def list_confirmed_transfer_pairs(
        self,
        transaction_ids: tuple[str, ...],
    ) -> tuple[ConfirmedTransferRow, ...]:
        """Return confirmed paired suggestions touching the observed rows."""
        if not transaction_ids:
            return ()
        subject = aliased(VerifiedTransactionRecord, name="transfer_subject")
        counterpart = aliased(VerifiedTransactionRecord, name="transfer_counterpart")
        statement = (
            select(FinancialRoleSuggestionRecord, subject, counterpart)
            .join(
                subject,
                subject.id == FinancialRoleSuggestionRecord.verified_transaction_id,
            )
            .join(
                counterpart,
                counterpart.id
                == FinancialRoleSuggestionRecord.counterpart_transaction_id,
            )
            .where(
                FinancialRoleSuggestionRecord.kind == "transfer",
                FinancialRoleSuggestionRecord.status == "confirmed",
                FinancialRoleSuggestionRecord.counterpart_transaction_id.is_not(None),
                or_(
                    FinancialRoleSuggestionRecord.verified_transaction_id.in_(
                        transaction_ids
                    ),
                    FinancialRoleSuggestionRecord.counterpart_transaction_id.in_(
                        transaction_ids
                    ),
                ),
            )
            .order_by(FinancialRoleSuggestionRecord.id)
        )
        return tuple(self._session.execute(statement).tuples())


class CategorisationRepository:
    """Read categorisation inputs and stage category-only transaction updates."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to one transaction-scoped session."""
        self._session = session

    def list_transactions_for_profile(
        self,
        user_profile_id: str,
        *,
        transaction_ids: tuple[str, ...] | None = None,
    ) -> tuple[VerifiedTransactionRecord, ...]:
        """Return owned verified transactions in deterministic order."""
        statement = (
            select(VerifiedTransactionRecord)
            .join(
                AccountRecord, AccountRecord.id == VerifiedTransactionRecord.account_id
            )
            .where(AccountRecord.user_profile_id == user_profile_id)
        )
        if transaction_ids is not None:
            statement = statement.where(
                VerifiedTransactionRecord.id.in_(transaction_ids)
            )
        statement = statement.order_by(
            VerifiedTransactionRecord.transaction_date,
            VerifiedTransactionRecord.account_id,
            VerifiedTransactionRecord.id,
        )
        return tuple(self._session.scalars(statement))

    def latest_category_corrections(
        self,
        transaction_ids: tuple[str, ...],
    ) -> dict[str, CategoryCorrectionRecord]:
        """Return the latest explicit category decision for each transaction."""
        if not transaction_ids:
            return {}
        statement = (
            select(CategoryCorrectionRecord)
            .where(
                CategoryCorrectionRecord.verified_transaction_id.in_(transaction_ids)
            )
            .order_by(
                CategoryCorrectionRecord.corrected_at,
                CategoryCorrectionRecord.id,
            )
        )
        latest: dict[str, CategoryCorrectionRecord] = {}
        for correction in self._session.scalars(statement):
            latest[correction.verified_transaction_id] = correction
        return latest

    def list_categories(
        self,
        category_ids: tuple[str, ...],
    ) -> tuple[CategoryRecord, ...]:
        """Return category targets in stable ID order."""
        if not category_ids:
            return ()
        statement = (
            select(CategoryRecord)
            .where(CategoryRecord.id.in_(category_ids))
            .order_by(CategoryRecord.id)
        )
        return tuple(self._session.scalars(statement))

    def assign_category(
        self,
        transaction: VerifiedTransactionRecord,
        category_id: str,
    ) -> None:
        """Stage only the selected category on one verified transaction."""
        transaction.category_id = category_id


class FinancialRoleRepository:
    """Persist advisory suggestions and explicit user role decisions."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to one transaction-scoped session."""
        self._session = session

    def list_unknown_candidates_for_user(
        self,
        user_profile_id: str,
    ) -> tuple[tuple[VerifiedTransactionRecord, AccountRecord], ...]:
        """Return unknown-role transactions owned by one local profile."""
        statement = (
            select(VerifiedTransactionRecord, AccountRecord)
            .join(
                AccountRecord,
                AccountRecord.id == VerifiedTransactionRecord.account_id,
            )
            .where(
                AccountRecord.user_profile_id == user_profile_id,
                VerifiedTransactionRecord.financial_role_id == "unknown",
            )
            .order_by(
                VerifiedTransactionRecord.transaction_date,
                VerifiedTransactionRecord.id,
            )
        )
        return tuple(self._session.execute(statement).tuples())

    def get_transaction(
        self,
        transaction_id: str,
    ) -> VerifiedTransactionRecord | None:
        """Return a verified transaction by ID."""
        return self._session.get(VerifiedTransactionRecord, transaction_id)

    def add_suggestion(
        self,
        suggestion: FinancialRoleSuggestionRecord,
    ) -> FinancialRoleSuggestionRecord:
        """Stage and flush one idempotently keyed system suggestion."""
        self._session.add(suggestion)
        self._session.flush()
        return suggestion

    def get_suggestion(
        self,
        suggestion_id: str,
    ) -> FinancialRoleSuggestionRecord | None:
        """Return a suggestion by ID."""
        return self._session.get(FinancialRoleSuggestionRecord, suggestion_id)

    def get_suggestion_by_key(
        self,
        suggestion_key: str,
    ) -> FinancialRoleSuggestionRecord | None:
        """Find an existing deterministic suggestion."""
        statement = select(FinancialRoleSuggestionRecord).where(
            FinancialRoleSuggestionRecord.suggestion_key == suggestion_key
        )
        return self._session.scalar(statement)

    def list_pending_for_user(
        self,
        user_profile_id: str,
    ) -> tuple[
        tuple[
            FinancialRoleSuggestionRecord,
            VerifiedTransactionRecord,
            ImportContextRecord | None,
        ],
        ...,
    ]:
        """Return pending suggestions with inert statement context for display."""
        statement = (
            select(
                FinancialRoleSuggestionRecord,
                VerifiedTransactionRecord,
                ImportContextRecord,
            )
            .join(
                VerifiedTransactionRecord,
                VerifiedTransactionRecord.id
                == FinancialRoleSuggestionRecord.verified_transaction_id,
            )
            .join(
                AccountRecord,
                AccountRecord.id == VerifiedTransactionRecord.account_id,
            )
            .join(
                RawTransactionRecord,
                RawTransactionRecord.id == VerifiedTransactionRecord.raw_transaction_id,
            )
            .join(
                ImportBatchRecord,
                ImportBatchRecord.id == RawTransactionRecord.import_batch_id,
            )
            .outerjoin(
                ImportContextRecord,
                ImportContextRecord.import_batch_id == ImportBatchRecord.id,
            )
            .where(
                AccountRecord.user_profile_id == user_profile_id,
                FinancialRoleSuggestionRecord.status == "pending",
            )
            .order_by(
                VerifiedTransactionRecord.transaction_date,
                FinancialRoleSuggestionRecord.id,
            )
        )
        return tuple(self._session.execute(statement).tuples())

    def reject_pending_for_transactions(
        self,
        transaction_ids: tuple[str, ...],
        *,
        reviewed_at: datetime,
        except_suggestion_id: str | None = None,
    ) -> None:
        """Reject competing pending suggestions after an explicit decision."""
        statement = select(FinancialRoleSuggestionRecord).where(
            FinancialRoleSuggestionRecord.status == "pending",
            or_(
                FinancialRoleSuggestionRecord.verified_transaction_id.in_(
                    transaction_ids
                ),
                FinancialRoleSuggestionRecord.counterpart_transaction_id.in_(
                    transaction_ids
                ),
            ),
        )
        if except_suggestion_id is not None:
            statement = statement.where(
                FinancialRoleSuggestionRecord.id != except_suggestion_id
            )
        for suggestion in self._session.scalars(statement):
            suggestion.status = "rejected"
            suggestion.reviewed_at = reviewed_at

    def add_audit(
        self,
        audit: FinancialRoleAuditRecord,
    ) -> FinancialRoleAuditRecord:
        """Stage and flush one immutable role-change audit entry."""
        self._session.add(audit)
        self._session.flush()
        return audit

    def list_audits_for_transaction(
        self,
        transaction_id: str,
    ) -> tuple[FinancialRoleAuditRecord, ...]:
        """Return a transaction's role history in deterministic order."""
        statement = (
            select(FinancialRoleAuditRecord)
            .where(FinancialRoleAuditRecord.verified_transaction_id == transaction_id)
            .order_by(
                FinancialRoleAuditRecord.changed_at,
                FinancialRoleAuditRecord.id,
            )
        )
        return tuple(self._session.scalars(statement))

    def add_flag_once(
        self,
        transaction_id: str,
        *,
        flag: str,
        created_at: datetime,
    ) -> bool:
        """Add one structured flag unless the transaction already has it."""
        statement = select(UserFlagRecord).where(
            UserFlagRecord.verified_transaction_id == transaction_id,
            UserFlagRecord.flag == flag,
        )
        if self._session.scalar(statement) is not None:
            return False
        self._session.add(
            UserFlagRecord(
                verified_transaction_id=transaction_id,
                flag=flag,
                note=None,
                created_at=created_at,
            )
        )
        self._session.flush()
        return True
