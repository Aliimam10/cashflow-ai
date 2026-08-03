"""Tests for deterministic synthetic transaction generation."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from cashflow_ai.demo_data.generator import (
    _Candidate,
    _finalise,
    generate_dataset,
    reconcile_balances,
)
from cashflow_ai.demo_data.models import SyntheticProfile


@pytest.mark.parametrize("profile", list(SyntheticProfile))
def test_each_profile_has_required_labels_and_reconciled_balances(
    profile: SyntheticProfile,
) -> None:
    dataset = generate_dataset(profile, seed=17, years=1)

    assert dataset.profile is profile
    assert dataset.start_date == date(2024, 1, 1)
    assert dataset.end_date == date(2024, 12, 31)
    assert len(dataset.transactions) > 200
    assert reconcile_balances(dataset)
    assert all(transaction.currency == "GBP" for transaction in dataset.transactions)
    assert all(
        transaction.posting_date.weekday() < 5 for transaction in dataset.transactions
    )
    assert any(transaction.amount > 0 for transaction in dataset.transactions)
    assert any(transaction.amount < 0 for transaction in dataset.transactions)
    assert any(transaction.is_recurring for transaction in dataset.transactions)
    assert {
        transaction.anomaly_type
        for transaction in dataset.transactions
        if transaction.anomaly_type
    } >= {"exact_duplicate", "probable_duplicate", "unusually_large"}


def test_same_seed_produces_the_same_history() -> None:
    first = generate_dataset(SyntheticProfile.STUDENT, seed=123, years=1)
    second = generate_dataset(SyntheticProfile.STUDENT, seed=123, years=1)
    different = generate_dataset(SyntheticProfile.STUDENT, seed=124, years=1)

    assert first == second
    assert first.transactions != different.transactions


@pytest.mark.parametrize("years", [0, 4])
def test_generation_rejects_unsupported_history_length(years: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 3"):
        generate_dataset(SyntheticProfile.STUDENT, years=years)


def test_leap_day_start_produces_a_valid_anniversary() -> None:
    dataset = generate_dataset(
        SyntheticProfile.STUDENT,
        start_date=date(2024, 2, 29),
        years=1,
    )

    assert dataset.end_date == date(2025, 2, 27)
    assert all(
        dataset.start_date <= transaction.transaction_date <= dataset.end_date
        for transaction in dataset.transactions
    )


def test_irregular_income_respects_partial_boundary_months() -> None:
    dataset = generate_dataset(
        SyntheticProfile.IRREGULAR_INCOME_WORKER,
        seed=42,
        start_date=date(2023, 6, 15),
        years=1,
    )

    assert dataset.end_date == date(2024, 6, 14)
    assert all(
        dataset.start_date <= transaction.transaction_date <= dataset.end_date
        for transaction in dataset.transactions
    )


def test_recurring_prices_drift_across_years() -> None:
    dataset = generate_dataset(SyntheticProfile.STUDENT, seed=9, years=2)
    streaming_amounts = {
        transaction.amount
        for transaction in dataset.transactions
        if transaction.recurring_series == "streaming_subscription"
    }

    assert streaming_amounts == {Decimal("-8.99"), Decimal("-9.71")}


def test_exact_duplicate_repeats_source_without_affecting_balance() -> None:
    dataset = generate_dataset(SyntheticProfile.SALARIED_WORKER, years=1)
    exact_duplicate = next(
        transaction
        for transaction in dataset.transactions
        if transaction.is_exact_duplicate
    )
    source = next(
        transaction
        for transaction in dataset.transactions
        if transaction.external_id == exact_duplicate.duplicate_of
        and not transaction.is_exact_duplicate
    )

    assert exact_duplicate.external_id == source.external_id
    assert exact_duplicate.amount == source.amount
    assert exact_duplicate.balance_after == source.balance_after
    assert reconcile_balances(dataset)


def test_reconciliation_detects_row_and_closing_balance_errors() -> None:
    dataset = generate_dataset(SyntheticProfile.STUDENT, years=1)
    index = next(
        index
        for index, transaction in enumerate(dataset.transactions)
        if not transaction.is_exact_duplicate
    )
    transactions = list(dataset.transactions)
    transactions[index] = replace(
        transactions[index],
        balance_after=transactions[index].balance_after + Decimal("1.00"),
    )

    assert not reconcile_balances(replace(dataset, transactions=tuple(transactions)))
    assert not reconcile_balances(
        replace(dataset, closing_balance=dataset.closing_balance + Decimal("1.00"))
    )


def test_finalisation_rejects_source_less_exact_duplicate() -> None:
    invalid = _Candidate(
        sequence=0,
        transaction_date=date(2024, 1, 1),
        description="INVALID DUPLICATE",
        merchant="Invalid Example",
        amount=Decimal("-1.00"),
        transaction_type="card_payment",
        category="Other",
        is_exact_duplicate=True,
    )

    with pytest.raises(ValueError, match="missing its source"):
        _finalise([invalid], SyntheticProfile.STUDENT, Decimal("10.00"))
