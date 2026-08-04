"""Canonical and provisional transaction contracts."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.money import Money

NonEmptyText = Annotated[str, Field(min_length=1, max_length=500)]
Identifier = Annotated[str, Field(min_length=1, max_length=255)]
CategoryId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class Currency(StrEnum):
    """Currencies accepted by the Version 1 canonical contract."""

    GBP = "GBP"


class Direction(StrEnum):
    """Signed transaction direction."""

    INFLOW = "inflow"
    OUTFLOW = "outflow"


class FinancialRole(StrEnum):
    """How a transaction contributes to financial calculations."""

    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    REFUND = "refund"
    REIMBURSEMENT = "reimbursement"
    CASH_WITHDRAWAL = "cash_withdrawal"
    EXCLUDED = "excluded"
    UNKNOWN = "unknown"


class _TransactionBase(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    transaction_date: date | None = None
    posting_date: date | None = None
    description: NonEmptyText | None = None
    merchant: NonEmptyText | None = None
    amount: Money | None = None
    balance_after: Money | None = None
    currency: Currency | None = None
    account_id: Identifier | None = None
    external_id: Identifier | None = None
    transaction_type: Identifier | None = None
    direction: Direction | None = None
    category_id: CategoryId | None = None
    financial_role: FinancialRole | None = None


class TransactionDraft(_TransactionBase):
    """Possibly incomplete values extracted before user confirmation."""


class CanonicalTransaction(_TransactionBase):
    """Validated transaction accepted by downstream application services."""

    transaction_date: date
    description: NonEmptyText
    amount: Money
    currency: Currency = Currency.GBP
    account_id: Identifier
    direction: Direction
    financial_role: FinancialRole = FinancialRole.UNKNOWN

    @model_validator(mode="after")
    def validate_signed_direction(self) -> CanonicalTransaction:
        """Require the canonical sign convention to match direction."""
        if self.amount == 0:
            msg = "canonical transaction amount cannot be zero"
            raise ValueError(msg)
        if self.amount > 0 and self.direction is not Direction.INFLOW:
            msg = "positive amounts must use the inflow direction"
            raise ValueError(msg)
        if self.amount < 0 and self.direction is not Direction.OUTFLOW:
            msg = "negative amounts must use the outflow direction"
            raise ValueError(msg)
        return self
