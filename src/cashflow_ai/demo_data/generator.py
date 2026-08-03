"""Deterministic synthetic transaction history generation."""

from __future__ import annotations

import calendar
import random
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from cashflow_ai.demo_data.models import (
    SyntheticDataset,
    SyntheticProfile,
    SyntheticTransaction,
)

PENNY: Final = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class _Merchant:
    name: str
    description: str
    category: str
    minimum_pence: int
    maximum_pence: int


@dataclass(frozen=True, slots=True)
class _ProfileDefinition:
    opening_balance: Decimal
    daily_purchase_probability: float
    merchants: tuple[_Merchant, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    sequence: int
    transaction_date: date
    description: str
    merchant: str
    amount: Decimal
    transaction_type: str
    category: str
    is_recurring: bool = False
    recurring_series: str | None = None
    anomaly_type: str | None = None
    duplicate_source_sequence: int | None = None
    is_exact_duplicate: bool = False


PROFILE_DEFINITIONS: Final = {
    SyntheticProfile.STUDENT: _ProfileDefinition(
        opening_balance=Decimal("850.00"),
        daily_purchase_probability=0.48,
        merchants=(
            _Merchant("Green Basket", "GREEN BASKET", "Groceries", 450, 4800),
            _Merchant("Campus Cafe", "CAMPUS CAFE", "Eating Out", 250, 1900),
            _Merchant("City Transit", "CITY TRANSIT", "Transport", 200, 1200),
            _Merchant("Study Supply", "STUDY SUPPLY", "Education", 300, 4500),
            _Merchant("Film House", "FILM HOUSE", "Entertainment", 700, 2200),
        ),
    ),
    SyntheticProfile.SALARIED_WORKER: _ProfileDefinition(
        opening_balance=Decimal("2400.00"),
        daily_purchase_probability=0.58,
        merchants=(
            _Merchant("Market Square", "MARKET SQUARE", "Groceries", 700, 6500),
            _Merchant("Lunch Room", "LUNCH ROOM", "Eating Out", 600, 2800),
            _Merchant("Metro Rail", "METRO RAIL", "Transport", 300, 2200),
            _Merchant("Home Store", "HOME STORE", "Shopping", 900, 11000),
            _Merchant("Weekend Arts", "WEEKEND ARTS", "Entertainment", 900, 5000),
        ),
    ),
    SyntheticProfile.IRREGULAR_INCOME_WORKER: _ProfileDefinition(
        opening_balance=Decimal("1600.00"),
        daily_purchase_probability=0.43,
        merchants=(
            _Merchant("Corner Market", "CORNER MARKET", "Groceries", 500, 5200),
            _Merchant("Co-work Cafe", "CO WORK CAFE", "Eating Out", 350, 2400),
            _Merchant("Local Bus", "LOCAL BUS", "Transport", 180, 1000),
            _Merchant("Creative Supply", "CREATIVE SUPPLY", "Shopping", 500, 8000),
            _Merchant("Community Venue", "COMMUNITY VENUE", "Entertainment", 600, 3800),
        ),
    ),
}


def _money(value: Decimal) -> Decimal:
    return value.quantize(PENNY, rounding=ROUND_HALF_UP)


def _end_date(start_date: date, years: int) -> date:
    if years not in {1, 2, 3}:
        msg = "years must be between 1 and 3"
        raise ValueError(msg)
    try:
        anniversary = start_date.replace(year=start_date.year + years)
    except ValueError:
        anniversary = start_date.replace(
            year=start_date.year + years,
            day=28,
        )
    return anniversary - timedelta(days=1)


def _month_dates(start_date: date, end_date: date, day: int) -> list[date]:
    current_year = start_date.year
    current_month = start_date.month
    dates: list[date] = []

    while (current_year, current_month) <= (end_date.year, end_date.month):
        last_day = calendar.monthrange(current_year, current_month)[1]
        occurrence = date(current_year, current_month, min(day, last_day))
        if start_date <= occurrence <= end_date:
            dates.append(occurrence)
        if current_month == 12:
            current_year += 1
            current_month = 1
        else:
            current_month += 1
    return dates


def _drifted_amount(
    base_amount: Decimal,
    occurrence: date,
    start_date: date,
    annual_rate: Decimal,
) -> Decimal:
    years_elapsed = max(0, occurrence.year - start_date.year)
    multiplier = Decimal("1") + annual_rate * years_elapsed
    return _money(base_amount * multiplier)


def _append_monthly(
    candidates: list[_Candidate],
    *,
    start_date: date,
    end_date: date,
    day: int,
    merchant: str,
    description: str,
    amount: Decimal,
    transaction_type: str,
    category: str,
    series: str,
    annual_rate: Decimal = Decimal("0"),
) -> None:
    for occurrence in _month_dates(start_date, end_date, day):
        candidates.append(
            _Candidate(
                sequence=len(candidates),
                transaction_date=occurrence,
                description=description,
                merchant=merchant,
                amount=_drifted_amount(
                    amount,
                    occurrence,
                    start_date,
                    annual_rate,
                ),
                transaction_type=transaction_type,
                category=category,
                is_recurring=True,
                recurring_series=series,
            )
        )


def _append_weekly(
    candidates: list[_Candidate],
    *,
    start_date: date,
    end_date: date,
    weekday: int,
    merchant: str,
    description: str,
    amount: Decimal,
    category: str,
    series: str,
) -> None:
    occurrence = start_date + timedelta(days=(weekday - start_date.weekday()) % 7)
    while occurrence <= end_date:
        candidates.append(
            _Candidate(
                sequence=len(candidates),
                transaction_date=occurrence,
                description=description,
                merchant=merchant,
                amount=amount,
                transaction_type="card_payment",
                category=category,
                is_recurring=True,
                recurring_series=series,
            )
        )
        occurrence += timedelta(days=7)


def _append_profile_recurring(
    candidates: list[_Candidate],
    profile: SyntheticProfile,
    start_date: date,
    end_date: date,
) -> None:
    if profile is SyntheticProfile.STUDENT:
        _append_monthly(
            candidates,
            start_date=start_date,
            end_date=end_date,
            day=1,
            merchant="Student Funding",
            description="STUDENT FUNDING",
            amount=Decimal("1150.00"),
            transaction_type="credit",
            category="Income",
            series="student_funding",
        )
        _append_monthly(
            candidates,
            start_date=start_date,
            end_date=end_date,
            day=2,
            merchant="Campus Housing",
            description="CAMPUS HOUSING RENT",
            amount=Decimal("-675.00"),
            transaction_type="direct_debit",
            category="Housing",
            series="student_rent",
            annual_rate=Decimal("0.04"),
        )
        _append_weekly(
            candidates,
            start_date=start_date,
            end_date=end_date,
            weekday=0,
            merchant="City Transit",
            description="CITY TRANSIT WEEKLY",
            amount=Decimal("-12.50"),
            category="Transport",
            series="student_transit",
        )
        subscription_amount = Decimal("-8.99")
    elif profile is SyntheticProfile.SALARIED_WORKER:
        _append_monthly(
            candidates,
            start_date=start_date,
            end_date=end_date,
            day=25,
            merchant="Example Employer",
            description="EXAMPLE EMPLOYER PAYROLL",
            amount=Decimal("2850.00"),
            transaction_type="credit",
            category="Income",
            series="salary",
            annual_rate=Decimal("0.03"),
        )
        _append_monthly(
            candidates,
            start_date=start_date,
            end_date=end_date,
            day=1,
            merchant="Home Lettings",
            description="HOME LETTINGS RENT",
            amount=Decimal("-1125.00"),
            transaction_type="standing_order",
            category="Housing",
            series="worker_rent",
            annual_rate=Decimal("0.04"),
        )
        _append_monthly(
            candidates,
            start_date=start_date,
            end_date=end_date,
            day=26,
            merchant="Savings Account",
            description="SAVINGS TRANSFER",
            amount=Decimal("-350.00"),
            transaction_type="transfer",
            category="Savings",
            series="monthly_savings",
        )
        subscription_amount = Decimal("-12.99")
    else:
        _append_monthly(
            candidates,
            start_date=start_date,
            end_date=end_date,
            day=1,
            merchant="City Homes",
            description="CITY HOMES RENT",
            amount=Decimal("-925.00"),
            transaction_type="standing_order",
            category="Housing",
            series="freelancer_rent",
            annual_rate=Decimal("0.04"),
        )
        _append_monthly(
            candidates,
            start_date=start_date,
            end_date=end_date,
            day=14,
            merchant="Workspace Tools",
            description="WORKSPACE TOOLS SUBSCRIPTION",
            amount=Decimal("-24.00"),
            transaction_type="card_payment",
            category="Subscriptions",
            series="workspace_tools",
            annual_rate=Decimal("0.06"),
        )
        subscription_amount = Decimal("-10.99")

    _append_monthly(
        candidates,
        start_date=start_date,
        end_date=end_date,
        day=10,
        merchant="Example Streaming",
        description="EXAMPLE STREAMING",
        amount=subscription_amount,
        transaction_type="card_payment",
        category="Subscriptions",
        series="streaming_subscription",
        annual_rate=Decimal("0.08"),
    )
    _append_monthly(
        candidates,
        start_date=start_date,
        end_date=end_date,
        day=18,
        merchant="Example Mobile",
        description="EXAMPLE MOBILE BILL",
        amount=Decimal("-22.00"),
        transaction_type="direct_debit",
        category="Utilities",
        series="mobile_bill",
    )


def _append_irregular_income(
    candidates: list[_Candidate],
    rng: random.Random,
    start_date: date,
    end_date: date,
) -> None:
    clients = ("Fictional Client A", "Fictional Client B", "Fictional Client C")
    current_year = start_date.year
    current_month = start_date.month
    while (current_year, current_month) <= (end_date.year, end_date.month):
        last_day = calendar.monthrange(current_year, current_month)[1]
        for _ in range(rng.randint(1, 4)):
            occurrence = date(
                current_year,
                current_month,
                rng.randint(1, last_day),
            )
            if start_date <= occurrence <= end_date:
                client = rng.choice(clients)
                candidates.append(
                    _Candidate(
                        sequence=len(candidates),
                        transaction_date=occurrence,
                        description=f"{client.upper()} INVOICE",
                        merchant=client,
                        amount=Decimal(rng.randint(25000, 125000)) / 100,
                        transaction_type="credit",
                        category="Income",
                    )
                )
        if current_month == 12:
            current_year += 1
            current_month = 1
        else:
            current_month += 1


def _append_discretionary(
    candidates: list[_Candidate],
    rng: random.Random,
    definition: _ProfileDefinition,
    start_date: date,
    end_date: date,
) -> None:
    current = start_date
    while current <= end_date:
        probability = definition.daily_purchase_probability
        if current.weekday() >= 5:
            probability *= 1.2
        if rng.random() < probability:
            merchant = rng.choice(definition.merchants)
            amount = (
                Decimal(-rng.randint(merchant.minimum_pence, merchant.maximum_pence))
                / 100
            )
            candidates.append(
                _Candidate(
                    sequence=len(candidates),
                    transaction_date=current,
                    description=f"{merchant.description} {rng.randint(1000, 9999)}",
                    merchant=merchant.name,
                    amount=amount,
                    transaction_type="card_payment",
                    category=merchant.category,
                )
            )
        current += timedelta(days=1)


def _append_anomaly_examples(
    candidates: list[_Candidate],
    profile: SyntheticProfile,
    start_date: date,
    end_date: date,
) -> None:
    midpoint = start_date + (end_date - start_date) // 2
    large_amount = {
        SyntheticProfile.STUDENT: Decimal("-480.00"),
        SyntheticProfile.SALARIED_WORKER: Decimal("-1250.00"),
        SyntheticProfile.IRREGULAR_INCOME_WORKER: Decimal("-780.00"),
    }[profile]
    candidates.append(
        _Candidate(
            sequence=len(candidates),
            transaction_date=midpoint,
            description="EXAMPLE LARGE TRAVEL PURCHASE",
            merchant="Example Travel",
            amount=large_amount,
            transaction_type="card_payment",
            category="Travel",
            anomaly_type="unusually_large",
        )
    )

    source = next(
        candidate
        for candidate in candidates
        if not candidate.is_recurring
        and candidate.amount < 0
        and candidate.anomaly_type is None
    )
    candidates.append(
        replace(
            source,
            sequence=len(candidates),
            anomaly_type="exact_duplicate",
            duplicate_source_sequence=source.sequence,
            is_exact_duplicate=True,
        )
    )
    candidates.append(
        replace(
            source,
            sequence=len(candidates),
            transaction_date=min(source.transaction_date + timedelta(days=1), end_date),
            description=f"{source.description} REPEAT",
            anomaly_type="probable_duplicate",
            duplicate_source_sequence=source.sequence,
        )
    )


def _posting_date(transaction_date: date) -> date:
    posting_date = transaction_date + timedelta(days=1)
    while posting_date.weekday() >= 5:
        posting_date += timedelta(days=1)
    return posting_date


def _external_id(profile: SyntheticProfile, sequence: int) -> str:
    return f"{profile.value[:3].upper()}-{sequence:07d}"


def _finalise(
    candidates: list[_Candidate],
    profile: SyntheticProfile,
    opening_balance: Decimal,
) -> tuple[tuple[SyntheticTransaction, ...], Decimal]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.transaction_date,
            item.is_exact_duplicate,
            item.sequence,
        ),
    )
    running_balance = opening_balance
    balances_by_sequence: dict[int, Decimal] = {}
    transactions: list[SyntheticTransaction] = []

    for candidate in ordered:
        if candidate.is_exact_duplicate:
            source_sequence = candidate.duplicate_source_sequence
            if source_sequence is None:
                msg = "exact duplicate is missing its source sequence"
                raise ValueError(msg)
            balance_after = balances_by_sequence[source_sequence]
            external_id = _external_id(profile, source_sequence)
        else:
            running_balance = _money(running_balance + candidate.amount)
            balance_after = running_balance
            balances_by_sequence[candidate.sequence] = balance_after
            external_id = _external_id(profile, candidate.sequence)

        duplicate_of = (
            _external_id(profile, candidate.duplicate_source_sequence)
            if candidate.duplicate_source_sequence is not None
            else None
        )
        transactions.append(
            SyntheticTransaction(
                external_id=external_id,
                transaction_date=candidate.transaction_date,
                posting_date=_posting_date(candidate.transaction_date),
                description=candidate.description,
                merchant=candidate.merchant,
                amount=_money(candidate.amount),
                balance_after=balance_after,
                currency="GBP",
                account=f"{profile.value}_current",
                transaction_type=candidate.transaction_type,
                category=candidate.category,
                is_recurring=candidate.is_recurring,
                recurring_series=candidate.recurring_series,
                anomaly_type=candidate.anomaly_type,
                duplicate_of=duplicate_of,
                is_exact_duplicate=candidate.is_exact_duplicate,
            )
        )

    return tuple(transactions), running_balance


def generate_dataset(
    profile: SyntheticProfile,
    *,
    seed: int = 42,
    start_date: date = date(2024, 1, 1),
    years: int = 2,
) -> SyntheticDataset:
    """Generate one deterministic synthetic transaction history."""
    end_date = _end_date(start_date, years)
    definition = PROFILE_DEFINITIONS[profile]
    rng = random.Random(seed)
    candidates: list[_Candidate] = []

    _append_profile_recurring(candidates, profile, start_date, end_date)
    if profile is SyntheticProfile.IRREGULAR_INCOME_WORKER:
        _append_irregular_income(candidates, rng, start_date, end_date)
    _append_discretionary(candidates, rng, definition, start_date, end_date)
    _append_anomaly_examples(candidates, profile, start_date, end_date)

    transactions, closing_balance = _finalise(
        candidates,
        profile,
        definition.opening_balance,
    )
    return SyntheticDataset(
        profile=profile,
        seed=seed,
        start_date=start_date,
        end_date=end_date,
        opening_balance=definition.opening_balance,
        closing_balance=closing_balance,
        transactions=transactions,
    )


def reconcile_balances(dataset: SyntheticDataset) -> bool:
    """Return whether non-duplicate rows reproduce every running balance."""
    running_balance = dataset.opening_balance
    for transaction in dataset.transactions:
        if transaction.is_exact_duplicate:
            continue
        running_balance = _money(running_balance + transaction.amount)
        if running_balance != transaction.balance_after:
            return False
    return running_balance == dataset.closing_balance
