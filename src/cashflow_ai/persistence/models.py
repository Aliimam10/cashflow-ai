"""SQLAlchemy models for the local CashFlow AI relational schema."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from cashflow_ai.persistence.base import Base, UTCDateTime, new_id, utc_now

MONEY = Numeric(18, 2)
HASH_LENGTH = 64
ID_LENGTH = 36


class UserProfileRecord(Base):
    """Local single-user preferences and base currency."""

    __tablename__ = "user_profiles"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    display_name: Mapped[str | None] = mapped_column(String(100))
    base_currency: Mapped[str] = mapped_column(String(3), default="GBP")
    timezone: Mapped[str] = mapped_column(String(100), default="UTC")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        CheckConstraint("base_currency = 'GBP'", name="ck_user_profiles_currency"),
    )


class AccountRecord(Base):
    """Current/checking or savings account owned by the local profile."""

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    user_profile_id: Mapped[str] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    account_type: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    institution_label: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        UniqueConstraint("user_profile_id", "name", name="uq_accounts_user_name"),
        CheckConstraint(
            "account_type IN ('current', 'checking', 'savings')",
            name="ck_accounts_type",
        ),
        CheckConstraint("currency = 'GBP'", name="ck_accounts_currency"),
    )


class ImportBatchRecord(Base):
    """One uploaded document import attempt for an account."""

    __tablename__ = "import_batches"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(20))
    source_filename: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(HASH_LENGTH))
    mime_type: Mapped[str] = mapped_column(String(100))
    byte_size: Mapped[int] = mapped_column(Integer)
    verification_status: Mapped[str] = mapped_column(String(20), default="unverified")
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        UniqueConstraint("account_id", "file_hash", name="uq_import_batches_file"),
        CheckConstraint(
            "source_type IN ('csv', 'digital_pdf', 'ocr_pdf')",
            name="ck_import_batches_source_type",
        ),
        CheckConstraint("byte_size > 0", name="ck_import_batches_byte_size"),
        CheckConstraint(
            "verification_status IN "
            "('unverified', 'needs_review', 'verified', 'rejected')",
            name="ck_import_batches_verification",
        ),
    )


class ImportContextRecord(Base):
    """User-supplied statement flags and inert note for an import."""

    __tablename__ = "import_contexts"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    import_batch_id: Mapped[str] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), unique=True
    )
    flags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class StatementCoverageRecord(Base):
    """Known statement extent and explicit missing periods."""

    __tablename__ = "statement_coverages"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    import_context_id: Mapped[str] = mapped_column(
        ForeignKey("import_contexts.id", ondelete="CASCADE"), unique=True
    )
    statement_start_date: Mapped[date] = mapped_column(Date)
    statement_end_date: Mapped[date] = mapped_column(Date)
    coverage_status: Mapped[str] = mapped_column(String(20))
    missing_periods_json: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, default=list
    )

    __table_args__ = (
        CheckConstraint(
            "statement_end_date >= statement_start_date",
            name="ck_statement_coverages_dates",
        ),
        CheckConstraint(
            "coverage_status IN "
            "('complete', 'partial', 'gapped', 'overlapping', 'unknown')",
            name="ck_statement_coverages_status",
        ),
    )


class BalanceSnapshotRecord(Base):
    """Account balance evidence stored separately from transactions."""

    __tablename__ = "balance_snapshots"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    import_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE")
    )
    balance: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    as_of_date: Mapped[date] = mapped_column(Date)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    source: Mapped[str] = mapped_column(String(30))
    verification_status: Mapped[str] = mapped_column(String(20))

    __table_args__ = (
        CheckConstraint("currency = 'GBP'", name="ck_balance_snapshots_currency"),
        CheckConstraint(
            "source IN "
            "('statement_opening', 'statement_closing', 'running_balance', 'manual')",
            name="ck_balance_snapshots_source",
        ),
        CheckConstraint(
            "verification_status IN "
            "('unverified', 'needs_review', 'verified', 'rejected')",
            name="ck_balance_snapshots_verification",
        ),
        CheckConstraint(
            "(source = 'manual' AND import_batch_id IS NULL) OR "
            "(source != 'manual' AND import_batch_id IS NOT NULL)",
            name="ck_balance_snapshots_lineage",
        ),
    )


class CategoryRecord(Base):
    """Versioned category taxonomy entry."""

    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT")
    )
    taxonomy_version: Mapped[str] = mapped_column(String(50), default="1.0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("taxonomy_version", "name", name="uq_categories_version_name"),
        CheckConstraint(
            "parent_id IS NULL OR parent_id != id", name="ck_categories_parent"
        ),
    )


class FinancialRoleRecord(Base):
    """Financial calculation role kept independent of category."""

    __tablename__ = "financial_roles"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class RawTransactionRecord(Base):
    """Auditable source row and provisional values before acceptance."""

    __tablename__ = "raw_transactions"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    import_batch_id: Mapped[str] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(20))
    source_row_number: Mapped[int | None] = mapped_column(Integer)
    page_number: Mapped[int | None] = mapped_column(Integer)
    page_record_number: Mapped[int | None] = mapped_column(Integer)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    original_date_text: Mapped[str] = mapped_column(Text)
    original_description: Mapped[str] = mapped_column(Text)
    original_amount_text: Mapped[str | None] = mapped_column(Text)
    parser_name: Mapped[str] = mapped_column(String(100))
    parser_version: Mapped[str] = mapped_column(String(50))
    source_fingerprint: Mapped[str] = mapped_column(String(HASH_LENGTH), unique=True)
    canonical_fingerprint: Mapped[str | None] = mapped_column(
        String(HASH_LENGTH), index=True
    )
    issues_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('csv', 'digital_pdf', 'ocr_pdf')",
            name="ck_raw_transactions_source_type",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'needs_review', 'confirmed', 'rejected')",
            name="ck_raw_transactions_review",
        ),
        CheckConstraint(
            "(source_type = 'csv' AND source_row_number IS NOT NULL "
            "AND page_number IS NULL AND page_record_number IS NULL) OR "
            "(source_type != 'csv' AND source_row_number IS NULL "
            "AND page_number IS NOT NULL AND page_record_number IS NOT NULL)",
            name="ck_raw_transactions_location",
        ),
    )


class VerifiedTransactionRecord(Base):
    """Reviewed transaction eligible for downstream calculations."""

    __tablename__ = "verified_transactions"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    raw_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("raw_transactions.id", ondelete="CASCADE"), unique=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    posting_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    merchant: Mapped[str | None] = mapped_column(String(500), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    balance_after: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    external_id: Mapped[str | None] = mapped_column(String(255))
    transaction_type: Mapped[str | None] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(10))
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    financial_role_id: Mapped[str] = mapped_column(
        ForeignKey("financial_roles.id", ondelete="RESTRICT"), default="unknown"
    )
    verified_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        UniqueConstraint(
            "account_id", "external_id", name="uq_verified_transactions_external_id"
        ),
        CheckConstraint("amount != 0", name="ck_verified_transactions_amount"),
        CheckConstraint("currency = 'GBP'", name="ck_verified_transactions_currency"),
        CheckConstraint(
            "direction IN ('inflow', 'outflow')",
            name="ck_verified_transactions_direction",
        ),
        CheckConstraint(
            "(amount > 0 AND direction = 'inflow') OR "
            "(amount < 0 AND direction = 'outflow')",
            name="ck_verified_transactions_signed_direction",
        ),
    )


Index(
    "ix_verified_transactions_account_date_amount",
    VerifiedTransactionRecord.account_id,
    VerifiedTransactionRecord.transaction_date,
    VerifiedTransactionRecord.amount,
)


class UserFlagRecord(Base):
    """User-applied structured transaction flag."""

    __tablename__ = "user_flags"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    verified_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("verified_transactions.id", ondelete="CASCADE"), index=True
    )
    flag: Mapped[str] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        UniqueConstraint(
            "verified_transaction_id", "flag", name="uq_user_flags_transaction_flag"
        ),
    )


class FinancialRoleSuggestionRecord(Base):
    """Reviewable system suggestion that never changes a role by itself."""

    __tablename__ = "financial_role_suggestions"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    suggestion_key: Mapped[str] = mapped_column(String(HASH_LENGTH), unique=True)
    verified_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("verified_transactions.id", ondelete="CASCADE"), index=True
    )
    counterpart_transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("verified_transactions.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    suggested_role_id: Mapped[str] = mapped_column(
        ForeignKey("financial_roles.id", ondelete="RESTRICT")
    )
    counterpart_role_id: Mapped[str | None] = mapped_column(
        ForeignKey("financial_roles.id", ondelete="RESTRICT")
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    algorithm_version: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        CheckConstraint(
            "kind IN ('transfer', 'refund', 'reimbursement')",
            name="ck_financial_role_suggestions_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_financial_role_suggestions_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_financial_role_suggestions_confidence",
        ),
        CheckConstraint(
            "counterpart_transaction_id IS NULL OR "
            "counterpart_transaction_id != verified_transaction_id",
            name="ck_financial_role_suggestions_counterpart",
        ),
        CheckConstraint(
            "(status = 'pending' AND reviewed_at IS NULL) OR "
            "(status != 'pending' AND reviewed_at IS NOT NULL)",
            name="ck_financial_role_suggestions_reviewed_at",
        ),
        CheckConstraint(
            "(kind = 'transfer' AND suggested_role_id IN "
            "('transfer_in', 'transfer_out') AND "
            "((counterpart_transaction_id IS NULL AND counterpart_role_id IS NULL) "
            "OR (counterpart_transaction_id IS NOT NULL AND "
            "counterpart_role_id IN ('transfer_in', 'transfer_out') AND "
            "counterpart_role_id != suggested_role_id))) OR "
            "(kind = 'refund' AND suggested_role_id = 'refund' AND "
            "counterpart_transaction_id IS NULL AND counterpart_role_id IS NULL) OR "
            "(kind = 'reimbursement' AND suggested_role_id = 'reimbursement' AND "
            "counterpart_transaction_id IS NULL AND counterpart_role_id IS NULL)",
            name="ck_financial_role_suggestions_roles",
        ),
    )


class FinancialRoleAuditRecord(Base):
    """Immutable audit entry for a user-confirmed financial-role change."""

    __tablename__ = "financial_role_audits"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    verified_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("verified_transactions.id", ondelete="CASCADE"), index=True
    )
    previous_role_id: Mapped[str] = mapped_column(
        ForeignKey("financial_roles.id", ondelete="RESTRICT")
    )
    new_role_id: Mapped[str] = mapped_column(
        ForeignKey("financial_roles.id", ondelete="RESTRICT")
    )
    suggestion_id: Mapped[str | None] = mapped_column(
        ForeignKey("financial_role_suggestions.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(30))
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint(
            "source IN ('user_confirmation', 'user_override')",
            name="ck_financial_role_audits_source",
        ),
        CheckConstraint(
            "previous_role_id != new_role_id",
            name="ck_financial_role_audits_changed",
        ),
    )


class CategoryCorrectionRecord(Base):
    """Auditable user correction to a transaction category."""

    __tablename__ = "category_corrections"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    verified_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("verified_transactions.id", ondelete="CASCADE"), index=True
    )
    previous_category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    new_category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT")
    )
    corrected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class PersonalCategoryRuleRecord(Base):
    """An explicitly requested, narrowly scoped local categorisation rule."""

    __tablename__ = "personal_category_rules"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    user_profile_id: Mapped[str] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT")
    )
    merchant: Mapped[str] = mapped_column(String(500))
    direction: Mapped[str | None] = mapped_column(String(10))
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )
    description_contains: Mapped[str | None] = mapped_column(String(500))
    minimum_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    maximum_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint(
            "direction IS NULL OR direction IN ('inflow', 'outflow')",
            name="ck_personal_category_rules_direction",
        ),
        CheckConstraint("priority >= 0", name="ck_personal_category_rules_priority"),
        CheckConstraint(
            "minimum_amount IS NULL OR minimum_amount >= 0",
            name="ck_personal_category_rules_minimum",
        ),
        CheckConstraint(
            "maximum_amount IS NULL OR maximum_amount >= 0",
            name="ck_personal_category_rules_maximum",
        ),
        CheckConstraint(
            "minimum_amount IS NULL OR maximum_amount IS NULL "
            "OR maximum_amount >= minimum_amount",
            name="ck_personal_category_rules_range",
        ),
    )


class CategoryDecisionRecord(Base):
    """Privacy-safe audit of a rule, model, fallback, or user category decision."""

    __tablename__ = "category_decisions"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    verified_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("verified_transactions.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT")
    )
    source: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(7, 6))
    model_version: Mapped[str | None] = mapped_column(String(100))
    rule_id: Mapped[str | None] = mapped_column(String(100))
    taxonomy_version: Mapped[str] = mapped_column(String(50))
    rule_set_version: Mapped[str] = mapped_column(String(50))
    reason_code: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        CheckConstraint(
            "source IN ('transaction_decision', 'personal_rule', "
            "'merchant_mapping', 'keyword_rule', 'ml_model', 'needs_review')",
            name="ck_category_decisions_source",
        ),
        CheckConstraint(
            "status IN ('applied', 'pending_review', 'superseded')",
            name="ck_category_decisions_status",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_category_decisions_confidence",
        ),
        CheckConstraint(
            "(source = 'ml_model' AND confidence IS NOT NULL "
            "AND model_version IS NOT NULL) OR "
            "(source != 'ml_model' AND confidence IS NULL "
            "AND model_version IS NULL)",
            name="ck_category_decisions_model_evidence",
        ),
        CheckConstraint(
            "(status = 'superseded' AND reviewed_at IS NOT NULL) OR "
            "(status != 'superseded' AND reviewed_at IS NULL)",
            name="ck_category_decisions_reviewed",
        ),
    )


class RecurringSeriesRecord(Base):
    """Persisted recurring-flow series definition."""

    __tablename__ = "recurring_series"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    merchant_pattern: Mapped[str] = mapped_column(String(500))
    expected_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    interval_days: Mapped[int] = mapped_column(Integer)
    financial_role_id: Mapped[str] = mapped_column(
        ForeignKey("financial_roles.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint("interval_days > 0", name="ck_recurring_series_interval"),
    )


class RecurringPaymentCandidateRecord(Base):
    """Detected recurring pattern awaiting or reflecting explicit user review."""

    __tablename__ = "recurring_payment_candidates"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    recurring_series_id: Mapped[str | None] = mapped_column(
        ForeignKey("recurring_series.id", ondelete="SET NULL")
    )
    merchant_group: Mapped[str] = mapped_column(String(500))
    expected_amount: Mapped[Decimal] = mapped_column(MONEY)
    frequency: Mapped[str] = mapped_column(String(20))
    interval_days: Mapped[int] = mapped_column(Integer)
    next_expected_date: Mapped[date] = mapped_column(Date)
    confidence: Mapped[Decimal] = mapped_column(Numeric(7, 6))
    covered_missed_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        CheckConstraint(
            "frequency IN ('weekly', 'fortnightly', 'monthly', 'quarterly', 'annual')",
            name="ck_recurring_candidates_frequency",
        ),
        CheckConstraint("interval_days > 0", name="ck_recurring_candidates_interval"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_recurring_candidates_confidence",
        ),
        CheckConstraint(
            "covered_missed_count >= 0", name="ck_recurring_candidates_missed"
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled')",
            name="ck_recurring_candidates_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND reviewed_at IS NULL) OR "
            "(status != 'pending' AND reviewed_at IS NOT NULL)",
            name="ck_recurring_candidates_reviewed",
        ),
    )


class RecurringPaymentMemberRecord(Base):
    """Verified transaction evidence belonging to one detected candidate."""

    __tablename__ = "recurring_payment_members"

    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("recurring_payment_candidates.id", ondelete="CASCADE"),
        primary_key=True,
    )
    verified_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("verified_transactions.id", ondelete="CASCADE"),
        primary_key=True,
    )


class BudgetRecord(Base):
    """Category budget for an inclusive date period."""

    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    user_profile_id: Mapped[str] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT")
    )
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    amount_limit: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default="GBP")

    __table_args__ = (
        UniqueConstraint(
            "user_profile_id",
            "category_id",
            "period_start",
            "period_end",
            name="uq_budgets_period",
        ),
        CheckConstraint("period_end >= period_start", name="ck_budgets_dates"),
        CheckConstraint("amount_limit >= 0", name="ck_budgets_amount"),
        CheckConstraint("currency = 'GBP'", name="ck_budgets_currency"),
    )


class SavingsGoalRecord(Base):
    """Named balance target for an account."""

    __tablename__ = "savings_goals"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    target_amount: Mapped[Decimal] = mapped_column(MONEY)
    current_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"))
    target_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        UniqueConstraint("account_id", "name", name="uq_savings_goals_account_name"),
        CheckConstraint("target_amount > 0", name="ck_savings_goals_target"),
        CheckConstraint("current_amount >= 0", name="ck_savings_goals_current"),
    )


class ScenarioRecord(Base):
    """User-defined what-if scenario inputs."""

    __tablename__ = "scenarios"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    assumptions_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        UniqueConstraint("account_id", "name", name="uq_scenarios_account_name"),
    )


class ModelMetadataRecord(Base):
    """Reproducibility and evaluation metadata for one model version."""

    __tablename__ = "model_metadata"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    model_name: Mapped[str] = mapped_column(String(100))
    model_version: Mapped[str] = mapped_column(String(100))
    task: Mapped[str] = mapped_column(String(100))
    artifact_path: Mapped[str | None] = mapped_column(String(500))
    training_cutoff: Mapped[date | None] = mapped_column(Date)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        UniqueConstraint(
            "model_name", "model_version", name="uq_model_metadata_version"
        ),
    )


class ForecastRunRecord(Base):
    """One reproducible forecast output for an account and optional scenario."""

    __tablename__ = "forecast_runs"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("scenarios.id", ondelete="SET NULL")
    )
    model_metadata_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_metadata.id", ondelete="SET NULL")
    )
    forecast_start: Mapped[date] = mapped_column(Date)
    forecast_end: Mapped[date] = mapped_column(Date)
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint(
            "forecast_end >= forecast_start", name="ck_forecast_runs_dates"
        ),
    )


class AnomalyAlertRecord(Base):
    """Persisted anomaly score and review state for a verified transaction."""

    __tablename__ = "anomaly_alerts"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    verified_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("verified_transactions.id", ondelete="CASCADE"), unique=True
    )
    model_metadata_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_metadata.id", ondelete="SET NULL")
    )
    score: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 1", name="ck_anomaly_alerts_score"),
        CheckConstraint(
            "status IN ('open', 'reviewed', 'dismissed')",
            name="ck_anomaly_alerts_status",
        ),
    )
