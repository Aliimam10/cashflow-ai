"""Supported financial-account contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cashflow_ai.schemas.transactions import Currency, Identifier


class AccountType(StrEnum):
    """Cash account types supported in Version 1."""

    CURRENT = "current"
    CHECKING = "checking"
    SAVINGS = "savings"


class Account(BaseModel):
    """A current/checking or savings account owned by the local user."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    account_id: Identifier
    name: str = Field(min_length=1, max_length=100)
    account_type: AccountType
    currency: Currency = Currency.GBP
    institution_label: str | None = Field(default=None, max_length=100)
    is_active: bool = True
