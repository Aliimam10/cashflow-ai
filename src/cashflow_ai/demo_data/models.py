"""Typed records emitted by the synthetic data generator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class SyntheticProfile(StrEnum):
    """Supported fictional financial profiles."""

    STUDENT = "student"
    SALARIED_WORKER = "salaried_worker"
    IRREGULAR_INCOME_WORKER = "irregular_income_worker"


@dataclass(frozen=True, slots=True)
class SyntheticTransaction:
    """One fully labelled synthetic transaction row."""

    external_id: str
    transaction_date: date
    posting_date: date
    description: str
    merchant: str
    amount: Decimal
    balance_after: Decimal
    currency: str
    account: str
    transaction_type: str
    category: str
    is_recurring: bool
    recurring_series: str | None
    anomaly_type: str | None
    duplicate_of: str | None
    is_exact_duplicate: bool


@dataclass(frozen=True, slots=True)
class SyntheticDataset:
    """A reproducible transaction history and its generation metadata."""

    profile: SyntheticProfile
    seed: int
    start_date: date
    end_date: date
    opening_balance: Decimal
    closing_balance: Decimal
    transactions: tuple[SyntheticTransaction, ...]
