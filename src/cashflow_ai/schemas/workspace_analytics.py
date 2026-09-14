"""Typed read-only contracts for finalized statement-workspace insights."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.analytics import (
    AccountBalanceHistory,
    CashFlowTotals,
    CategorySpending,
    DataCoverageIndicator,
    LargestTransaction,
    MonthlyCashFlow,
    SavingsRateResult,
    SpendingCadenceBreakdown,
)
from cashflow_ai.schemas.api import Pagination
from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.statements import DateRange
from cashflow_ai.schemas.transactions import (
    CategoryId,
    Currency,
    Direction,
    FinancialRole,
    Identifier,
)


class _WorkspaceAnalyticsContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class WorkspaceAnalyticsRequest(_WorkspaceAnalyticsContract):
    """Revision-bound request for one finalized workspace period."""

    expected_workspace_revision: int = Field(ge=1)
    period: DateRange | None = None
    largest_transaction_limit: int = Field(default=10, ge=1, le=100)


class WorkspaceAnalytics(_WorkspaceAnalyticsContract):
    """Coverage-aware insights calculated directly from approved workspace rows."""

    workspace_id: Identifier
    workspace_revision: int = Field(ge=1)
    account_name: str = Field(min_length=1, max_length=100, repr=False)
    period: DateRange
    currency: Currency
    coverage: DataCoverageIndicator
    totals: CashFlowTotals | None
    savings_rate: SavingsRateResult
    category_spending: tuple[CategorySpending, ...] | None
    spending_cadence: SpendingCadenceBreakdown | None
    largest_transactions: tuple[LargestTransaction, ...]
    balance_history: tuple[AccountBalanceHistory, ...]
    monthly_cash_flow: tuple[MonthlyCashFlow, ...]
    observed_transaction_count: int = Field(ge=0)
    unresolved_financial_role_count: int = Field(ge=0)


class WorkspaceTransactionSearchRequest(_WorkspaceAnalyticsContract):
    """Validated filters for approved rows in one workspace revision."""

    expected_workspace_revision: int = Field(ge=1)
    period: DateRange | None = None
    search_text: str | None = Field(default=None, min_length=1, max_length=100)
    category_ids: tuple[CategoryId, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=100,
    )
    financial_roles: tuple[FinancialRole, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
    )
    pagination: Pagination = Field(default_factory=Pagination)

    @model_validator(mode="after")
    def validate_unique_filters(self) -> WorkspaceTransactionSearchRequest:
        """Prevent repeated filters from making request meaning ambiguous."""
        for values in (self.category_ids, self.financial_roles):
            if values is not None and len(values) != len(set(values)):
                raise ValueError("workspace transaction filters must be unique")
        return self


class WorkspaceTransactionView(_WorkspaceAnalyticsContract):
    """One approved canonical row projected for the normal transaction screen."""

    row_id: Identifier
    transaction_date: date
    posting_date: date | None = None
    description: str = Field(min_length=1, max_length=500, repr=False)
    merchant: str | None = Field(default=None, max_length=500, repr=False)
    amount: Money
    balance_after: Money | None = Field(default=None, repr=False)
    currency: Currency
    direction: Direction
    category_id: CategoryId
    category_name: str = Field(min_length=1, max_length=100)
    financial_role: FinancialRole
    external_id: Identifier | None = Field(default=None, repr=False)
    transaction_type: Identifier | None = Field(default=None, repr=False)


class WorkspaceTransactionSearchResult(_WorkspaceAnalyticsContract):
    """Newest-first bounded transaction result tied to a workspace revision."""

    workspace_id: Identifier
    workspace_revision: int = Field(ge=1)
    account_name: str = Field(min_length=1, max_length=100, repr=False)
    currency: Currency
    items: tuple[WorkspaceTransactionView, ...]
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_page(self) -> WorkspaceTransactionSearchResult:
        """Keep the returned items inside the declared result window."""
        if len(self.items) > self.limit or len(self.items) > max(
            self.total - self.offset,
            0,
        ):
            raise ValueError("workspace transaction items exceed the result window")
        return self


__all__ = [
    "WorkspaceAnalytics",
    "WorkspaceAnalyticsRequest",
    "WorkspaceTransactionSearchRequest",
    "WorkspaceTransactionSearchResult",
    "WorkspaceTransactionView",
]
