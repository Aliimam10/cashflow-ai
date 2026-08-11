"""Contracts for conservative financial-role suggestions and user decisions."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.transactions import FinancialRole, Identifier

Confidence = Annotated[float, Field(ge=0, le=1)]


class _RoleModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class RoleSuggestionKind(StrEnum):
    """Supported system interpretations that always require review."""

    TRANSFER = "transfer"
    REFUND = "refund"
    REIMBURSEMENT = "reimbursement"


class RoleSuggestionStatus(StrEnum):
    """Review state of one persisted system suggestion."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class RoleSuggestionReason(StrEnum):
    """Controlled, non-sensitive facts supporting a suggestion."""

    EXACT_OPPOSITE_AMOUNT = "exact_opposite_amount"
    CLOSE_DATE = "close_date"
    SAME_OWNER = "same_owner"
    SAME_CURRENCY = "same_currency"
    DESCRIPTION_SIMILARITY = "description_similarity"
    ACCOUNT_REFERENCE = "account_reference"
    TRANSFER_LANGUAGE = "transfer_language"
    REFUND_LANGUAGE = "refund_language"
    REIMBURSEMENT_LANGUAGE = "reimbursement_language"


class RoleDecisionSource(StrEnum):
    """How an accepted financial-role change was made."""

    USER_CONFIRMATION = "user_confirmation"
    USER_OVERRIDE = "user_override"


class TransactionReviewAction(StrEnum):
    """Explicit actions available in the transaction review workflow."""

    INCOME = "income"
    EXPENSE = "expense"
    INTERNAL_TRANSFER = "internal_transfer"
    REFUND = "refund"
    REIMBURSEMENT = "reimbursement"
    CASH_WITHDRAWAL = "cash_withdrawal"
    IGNORE_FROM_ANALYTICS = "ignore_from_analytics"
    NEEDS_REVIEW = "needs_review"


class FinancialRoleSuggestion(_RoleModel):
    """Persisted advisory interpretation that has not silently changed a role."""

    suggestion_id: Identifier
    transaction_id: Identifier
    counterpart_transaction_id: Identifier | None = None
    kind: RoleSuggestionKind
    suggested_role: FinancialRole
    counterpart_role: FinancialRole | None = None
    confidence: Confidence
    reasons: tuple[RoleSuggestionReason, ...]
    algorithm_version: str = Field(min_length=1, max_length=50)
    status: RoleSuggestionStatus
    created_at: AwareDatetime
    reviewed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> FinancialRoleSuggestion:
        """Keep paired transfers and single-row suggestions coherent."""
        if self.counterpart_transaction_id == self.transaction_id:
            msg = "a transfer counterpart must be a different transaction"
            raise ValueError(msg)
        if self.kind is RoleSuggestionKind.TRANSFER:
            if self.suggested_role not in {
                FinancialRole.TRANSFER_IN,
                FinancialRole.TRANSFER_OUT,
            }:
                msg = "transfer suggestions require a transfer role"
                raise ValueError(msg)
            paired = self.counterpart_transaction_id is not None
            if paired != (self.counterpart_role is not None):
                msg = "paired transfer identifiers and roles must appear together"
                raise ValueError(msg)
            if paired and self.counterpart_role is self.suggested_role:
                msg = "paired transfers require opposite incoming and outgoing roles"
                raise ValueError(msg)
        elif self.counterpart_transaction_id is not None or self.counterpart_role:
            msg = "refund and reimbursement suggestions cannot have counterparts"
            raise ValueError(msg)
        elif (
            self.kind is RoleSuggestionKind.REFUND
            and self.suggested_role is not FinancialRole.REFUND
        ) or (
            self.kind is RoleSuggestionKind.REIMBURSEMENT
            and self.suggested_role is not FinancialRole.REIMBURSEMENT
        ):
            msg = "suggestion kind and financial role do not agree"
            raise ValueError(msg)

        pending = self.status is RoleSuggestionStatus.PENDING
        if pending == (self.reviewed_at is not None):
            msg = "only reviewed suggestions require a reviewed timestamp"
            raise ValueError(msg)
        return self


class FinancialRoleAudit(_RoleModel):
    """Immutable projection of one accepted financial-role change."""

    audit_id: Identifier
    transaction_id: Identifier
    previous_role: FinancialRole
    new_role: FinancialRole
    suggestion_id: Identifier | None = None
    source: RoleDecisionSource
    changed_at: AwareDatetime


class RoleReviewItem(_RoleModel):
    """Local review item with statement context shown only as reference."""

    suggestion: FinancialRoleSuggestion
    account_id: Identifier
    transaction_date: date
    description: str = Field(min_length=1, max_length=500)
    amount: Money
    current_role: FinancialRole
    statement_flags: tuple[str, ...] = ()
    statement_note: str | None = Field(default=None, max_length=2_000)


class RoleAssignment(_RoleModel):
    """One transaction role changed by an explicit user decision."""

    transaction_id: Identifier
    previous_role: FinancialRole
    new_role: FinancialRole


class RoleDecisionResult(_RoleModel):
    """Atomic outcome of confirming, rejecting, or overriding a role."""

    suggestion_id: Identifier | None = None
    suggestion_status: RoleSuggestionStatus | None = None
    assignments: tuple[RoleAssignment, ...] = ()
    needs_review_flagged: bool = False
