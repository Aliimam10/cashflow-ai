"""Focused SQLAlchemy repositories for current persistence boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import case, desc, exists, func, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement

from cashflow_ai.persistence.models import (
    AccountRecord,
    BalanceSnapshotRecord,
    BudgetRecord,
    CategoryCorrectionRecord,
    CategoryDecisionRecord,
    CategoryRecord,
    FinancialRoleAuditRecord,
    FinancialRoleSuggestionRecord,
    ImportBatchRecord,
    ImportContextRecord,
    ModelMetadataRecord,
    PersonalCategoryRuleRecord,
    RawTransactionRecord,
    RecurringPaymentCandidateRecord,
    RecurringPaymentMemberRecord,
    SavingsGoalRecord,
    StatementCoverageRecord,
    UserFlagRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)

type AnalyticsTransactionRow = tuple[
    VerifiedTransactionRecord,
    CategoryRecord | None,
    bool,
]
type AnalyticsCoverageRow = tuple[str, StatementCoverageRecord]
type ConfirmedTransferRow = tuple[
    FinancialRoleSuggestionRecord,
    VerifiedTransactionRecord,
    VerifiedTransactionRecord,
]


@dataclass(frozen=True, slots=True)
class MLTrainingCandidateRow:
    """Narrow training projection that deliberately omits raw private payloads."""

    transaction_id: str
    transaction_date: date
    verified_at: datetime
    merchant: str | None
    description: str
    account_lineage_matches: bool
    verified_source_type: str
    raw_review_status: str
    issue_codes: tuple[str, ...]
    batch_source_type: str
    batch_verification_status: str


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
            select(
                VerifiedTransactionRecord,
                CategoryRecord,
                exists()
                .where(
                    RecurringPaymentMemberRecord.verified_transaction_id
                    == VerifiedTransactionRecord.id,
                    RecurringPaymentMemberRecord.candidate_id
                    == RecurringPaymentCandidateRecord.id,
                    RecurringPaymentCandidateRecord.status == "confirmed",
                )
                .correlate(VerifiedTransactionRecord)
                .label("is_confirmed_recurring"),
            )
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

    def list_personal_rules(
        self, user_profile_id: str
    ) -> tuple[PersonalCategoryRuleRecord, ...]:
        """Return active persisted personal rules in stable identity order."""
        statement = (
            select(PersonalCategoryRuleRecord)
            .where(
                PersonalCategoryRuleRecord.user_profile_id == user_profile_id,
                PersonalCategoryRuleRecord.is_active.is_(True),
            )
            .order_by(PersonalCategoryRuleRecord.id)
        )
        return tuple(self._session.scalars(statement))

    def add_personal_rule(
        self, rule: PersonalCategoryRuleRecord
    ) -> PersonalCategoryRuleRecord:
        """Persist an explicitly supplied local rule."""
        self._session.add(rule)
        self._session.flush()
        return rule

    def get_personal_rule(self, rule_id: str) -> PersonalCategoryRuleRecord | None:
        """Return one personal rule by stable ID."""
        return self._session.get(PersonalCategoryRuleRecord, rule_id)

    def add_correction(
        self, correction: CategoryCorrectionRecord
    ) -> CategoryCorrectionRecord:
        """Append one explicit user category correction."""
        self._session.add(correction)
        self._session.flush()
        return correction

    def add_decision(self, decision: CategoryDecisionRecord) -> CategoryDecisionRecord:
        """Append privacy-safe categorisation provenance."""
        self._session.add(decision)
        self._session.flush()
        return decision

    def latest_decision(self, transaction_id: str) -> CategoryDecisionRecord | None:
        """Return the newest decision for idempotent hybrid runs."""
        statement = (
            select(CategoryDecisionRecord)
            .where(CategoryDecisionRecord.verified_transaction_id == transaction_id)
            .order_by(
                CategoryDecisionRecord.created_at.desc(),
                CategoryDecisionRecord.id.desc(),
            )
            .limit(1)
        )
        return self._session.scalar(statement)

    def list_pending_decisions(
        self, user_profile_id: str
    ) -> tuple[CategoryDecisionRecord, ...]:
        """Return the owned low-confidence queue without raw bank text."""
        statement = (
            select(CategoryDecisionRecord)
            .join(
                VerifiedTransactionRecord,
                VerifiedTransactionRecord.id
                == CategoryDecisionRecord.verified_transaction_id,
            )
            .join(
                AccountRecord, AccountRecord.id == VerifiedTransactionRecord.account_id
            )
            .where(
                AccountRecord.user_profile_id == user_profile_id,
                CategoryDecisionRecord.source == "ml_model",
                CategoryDecisionRecord.status == "pending_review",
            )
            .order_by(
                CategoryDecisionRecord.created_at,
                CategoryDecisionRecord.id,
            )
        )
        return tuple(self._session.scalars(statement))

    def supersede_pending_decisions(
        self, transaction_id: str, *, reviewed_at: datetime
    ) -> int:
        """Mark pending predictions reviewed after explicit feedback."""
        statement = select(CategoryDecisionRecord).where(
            CategoryDecisionRecord.verified_transaction_id == transaction_id,
            CategoryDecisionRecord.status == "pending_review",
        )
        decisions = tuple(self._session.scalars(statement))
        for decision in decisions:
            decision.status = "superseded"
            decision.reviewed_at = reviewed_at
        return len(decisions)


class MLCategorisationRepository:
    """Read cutoff-safe classifier evidence without loading raw bank payloads."""

    def __init__(self, session: Session) -> None:
        """Bind repository reads to one transaction-scoped session."""
        self._session = session

    def list_training_candidates(
        self,
        user_profile_id: str,
    ) -> tuple[MLTrainingCandidateRow, ...]:
        """Return a narrow, owned projection in deterministic historical order."""
        statement = (
            select(
                VerifiedTransactionRecord.id,
                VerifiedTransactionRecord.transaction_date,
                VerifiedTransactionRecord.verified_at,
                VerifiedTransactionRecord.merchant,
                VerifiedTransactionRecord.description,
                VerifiedTransactionRecord.account_id == ImportBatchRecord.account_id,
                RawTransactionRecord.source_type,
                RawTransactionRecord.review_status,
                RawTransactionRecord.issues_json,
                ImportBatchRecord.source_type,
                ImportBatchRecord.verification_status,
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
            .where(AccountRecord.user_profile_id == user_profile_id)
            .order_by(
                VerifiedTransactionRecord.transaction_date,
                VerifiedTransactionRecord.verified_at,
                VerifiedTransactionRecord.id,
            )
        )
        candidates: list[MLTrainingCandidateRow] = []
        for row in self._session.execute(statement):
            issues = row[8] if isinstance(row[8], list) else []
            issue_codes = tuple(
                str(issue["code"])
                for issue in issues
                if isinstance(issue, dict) and isinstance(issue.get("code"), str)
            )
            candidates.append(
                MLTrainingCandidateRow(
                    transaction_id=row[0],
                    transaction_date=row[1],
                    verified_at=row[2],
                    merchant=row[3],
                    description=row[4],
                    account_lineage_matches=row[5],
                    verified_source_type=row[6],
                    raw_review_status=row[7],
                    issue_codes=issue_codes,
                    batch_source_type=row[9],
                    batch_verification_status=row[10],
                )
            )
        return tuple(candidates)

    def latest_category_corrections_as_of(
        self,
        transaction_ids: tuple[str, ...],
        *,
        knowledge_cutoff_at: datetime,
    ) -> dict[str, CategoryCorrectionRecord]:
        """Return each latest explicit label known at the supplied cutoff."""
        if not transaction_ids:
            return {}
        statement = (
            select(CategoryCorrectionRecord)
            .where(
                CategoryCorrectionRecord.verified_transaction_id.in_(transaction_ids),
                CategoryCorrectionRecord.corrected_at <= knowledge_cutoff_at,
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

    def latest_financial_role_audits_as_of(
        self,
        transaction_ids: tuple[str, ...],
        *,
        knowledge_cutoff_at: datetime,
    ) -> dict[str, FinancialRoleAuditRecord]:
        """Reconstruct the latest user-confirmed role known at the cutoff."""
        if not transaction_ids:
            return {}
        statement = (
            select(FinancialRoleAuditRecord)
            .where(
                FinancialRoleAuditRecord.verified_transaction_id.in_(transaction_ids),
                FinancialRoleAuditRecord.changed_at <= knowledge_cutoff_at,
            )
            .order_by(
                FinancialRoleAuditRecord.changed_at,
                FinancialRoleAuditRecord.id,
            )
        )
        latest: dict[str, FinancialRoleAuditRecord] = {}
        for audit in self._session.scalars(statement):
            latest[audit.verified_transaction_id] = audit
        return latest

    def list_needs_review_transaction_ids_as_of(
        self,
        transaction_ids: tuple[str, ...],
        *,
        knowledge_cutoff_at: datetime,
    ) -> frozenset[str]:
        """Return rows carrying a structured review flag by the cutoff."""
        if not transaction_ids:
            return frozenset()
        statement = select(UserFlagRecord.verified_transaction_id).where(
            UserFlagRecord.verified_transaction_id.in_(transaction_ids),
            UserFlagRecord.flag == "needs_review",
            UserFlagRecord.created_at <= knowledge_cutoff_at,
        )
        return frozenset(self._session.scalars(statement))

    def list_unresolved_transfer_transaction_ids_as_of(
        self,
        transaction_ids: tuple[str, ...],
        *,
        knowledge_cutoff_at: datetime,
    ) -> frozenset[str]:
        """Return both legs of transfer suggestions unresolved at the cutoff."""
        if not transaction_ids:
            return frozenset()
        statement = select(
            FinancialRoleSuggestionRecord.verified_transaction_id,
            FinancialRoleSuggestionRecord.counterpart_transaction_id,
        ).where(
            FinancialRoleSuggestionRecord.kind == "transfer",
            FinancialRoleSuggestionRecord.created_at <= knowledge_cutoff_at,
            or_(
                FinancialRoleSuggestionRecord.reviewed_at.is_(None),
                FinancialRoleSuggestionRecord.reviewed_at > knowledge_cutoff_at,
            ),
            or_(
                FinancialRoleSuggestionRecord.verified_transaction_id.in_(
                    transaction_ids
                ),
                FinancialRoleSuggestionRecord.counterpart_transaction_id.in_(
                    transaction_ids
                ),
            ),
        )
        unresolved: set[str] = set()
        selected = set(transaction_ids)
        for subject_id, counterpart_id in self._session.execute(statement).tuples():
            if subject_id in selected:
                unresolved.add(subject_id)
            if counterpart_id in selected:
                unresolved.add(counterpart_id)
        return frozenset(unresolved)

    def list_categories(
        self,
        category_ids: tuple[str, ...],
    ) -> tuple[CategoryRecord, ...]:
        """Return requested category metadata in stable identifier order."""
        if not category_ids:
            return ()
        statement = (
            select(CategoryRecord)
            .where(CategoryRecord.id.in_(category_ids))
            .order_by(CategoryRecord.id)
        )
        return tuple(self._session.scalars(statement))


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


class ModelMetadataRepository:
    """Persist and select data-minimised local model metadata."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to one transaction-scoped session."""
        self._session = session

    def add(self, metadata: ModelMetadataRecord) -> ModelMetadataRecord:
        """Stage and flush one immutable model version."""
        self._session.add(metadata)
        self._session.flush()
        return metadata

    def get(self, model_id: str) -> ModelMetadataRecord | None:
        """Return one registered model version by identifier."""
        return self._session.get(ModelMetadataRecord, model_id)

    def get_version(
        self,
        *,
        model_name: str,
        model_version: str,
    ) -> ModelMetadataRecord | None:
        """Find a version before attempting its unique insert."""
        statement = select(ModelMetadataRecord).where(
            ModelMetadataRecord.model_name == model_name,
            ModelMetadataRecord.model_version == model_version,
        )
        return self._session.scalar(statement)

    def list(self, *, task: str | None = None) -> tuple[ModelMetadataRecord, ...]:
        """Return versions in stable task/name/version order."""
        statement = select(ModelMetadataRecord)
        if task is not None:
            statement = statement.where(ModelMetadataRecord.task == task)
        statement = statement.order_by(
            ModelMetadataRecord.task,
            ModelMetadataRecord.model_name,
            ModelMetadataRecord.created_at,
            ModelMetadataRecord.model_version,
            ModelMetadataRecord.id,
        )
        return tuple(self._session.scalars(statement))

    def get_active(self, *, task: str) -> ModelMetadataRecord | None:
        """Return the single explicitly active version for a modelling task."""
        statement = select(ModelMetadataRecord).where(
            ModelMetadataRecord.task == task,
            ModelMetadataRecord.is_active.is_(True),
        )
        return self._session.scalar(statement)

    def deactivate_for_task(self, *, task: str) -> ModelMetadataRecord | None:
        """Deactivate and return the previously active version, when present."""
        active = self.get_active(task=task)
        if active is not None:
            active.is_active = False
        return active


class PlanningRepository:
    """Persist and retrieve budgets and goals within an owned profile scope."""

    def __init__(self, session: Session) -> None:
        """Bind repository operations to one transaction-scoped session."""
        self._session = session

    def add_budget(self, budget: BudgetRecord) -> BudgetRecord:
        """Stage and flush one validated budget."""
        self._session.add(budget)
        self._session.flush()
        return budget

    def get_category(self, category_id: str) -> CategoryRecord | None:
        """Return one category used to validate a category-budget target."""
        return self._session.get(CategoryRecord, category_id)

    def list_budgets_on(
        self,
        *,
        user_profile_id: str,
        as_of_date: date,
    ) -> tuple[BudgetRecord, ...]:
        """Return budgets whose inclusive period contains the selected date."""
        statement = (
            select(BudgetRecord)
            .where(
                BudgetRecord.user_profile_id == user_profile_id,
                BudgetRecord.period_start <= as_of_date,
                BudgetRecord.period_end >= as_of_date,
            )
            .order_by(
                BudgetRecord.budget_type,
                BudgetRecord.category_id,
                BudgetRecord.period_start,
                BudgetRecord.id,
            )
        )
        return tuple(self._session.scalars(statement))

    def get_weekly_budget(
        self,
        *,
        user_profile_id: str,
        period_start: date,
        period_end: date,
    ) -> BudgetRecord | None:
        """Return the exact weekly discretionary budget for a forecast week."""
        statement = select(BudgetRecord).where(
            BudgetRecord.user_profile_id == user_profile_id,
            BudgetRecord.budget_type == "weekly_discretionary",
            BudgetRecord.period_start == period_start,
            BudgetRecord.period_end == period_end,
        )
        return self._session.scalar(statement)

    def add_goal(self, goal: SavingsGoalRecord) -> SavingsGoalRecord:
        """Stage and flush one validated savings or minimum-balance goal."""
        self._session.add(goal)
        self._session.flush()
        return goal

    def list_goals_for_accounts(
        self,
        account_ids: tuple[str, ...],
    ) -> tuple[tuple[SavingsGoalRecord, AccountRecord], ...]:
        """Return selected-account goals with ownership evidence."""
        statement = (
            select(SavingsGoalRecord, AccountRecord)
            .join(AccountRecord, AccountRecord.id == SavingsGoalRecord.account_id)
            .where(SavingsGoalRecord.account_id.in_(account_ids))
            .order_by(
                SavingsGoalRecord.goal_type,
                SavingsGoalRecord.account_id,
                SavingsGoalRecord.target_date,
                SavingsGoalRecord.name,
                SavingsGoalRecord.id,
            )
        )
        return tuple(self._session.execute(statement).tuples())
