"""Tests for canonical and provisional transaction contracts."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cashflow_ai.schemas import (
    CanonicalTransaction,
    Currency,
    Direction,
    TransactionDraft,
)


def valid_transaction_payload() -> dict[str, object]:
    return {
        "transaction_date": "2026-07-31",
        "posting_date": "2026-08-01",
        "description": "  EXAMPLE GROCER  ",
        "merchant": "Example Grocer",
        "amount": "-24.50",
        "balance_after": Decimal("975.50"),
        "currency": "GBP",
        "account_id": "current-account",
        "external_id": "BANK-123",
        "transaction_type": "card_payment",
        "direction": "outflow",
        "category_id": "groceries",
    }


def test_canonical_transaction_validates_and_serializes_money_as_strings() -> None:
    transaction = CanonicalTransaction.model_validate(valid_transaction_payload())

    assert transaction.transaction_date == date(2026, 7, 31)
    assert transaction.description == "EXAMPLE GROCER"
    assert transaction.amount == Decimal("-24.50")
    assert transaction.balance_after == Decimal("975.50")
    assert transaction.currency is Currency.GBP
    assert transaction.direction is Direction.OUTFLOW
    assert '"amount":"-24.50"' in transaction.model_dump_json()
    assert '"balance_after":"975.50"' in transaction.model_dump_json()


def test_positive_inflow_accepts_integer_money() -> None:
    payload = valid_transaction_payload()
    payload.update(amount=100, direction="inflow")

    transaction = CanonicalTransaction.model_validate(payload)

    assert transaction.amount == Decimal("100.00")
    assert transaction.direction is Direction.INFLOW


@pytest.mark.parametrize("amount", [1.25, True])
def test_binary_float_and_boolean_money_are_rejected(amount: object) -> None:
    payload = valid_transaction_payload()
    payload["amount"] = amount

    with pytest.raises(ValidationError, match="Decimal, integer, or decimal string"):
        CanonicalTransaction.model_validate(payload)


@pytest.mark.parametrize("amount", [object(), "not-money"])
def test_invalid_decimal_money_is_rejected(amount: object) -> None:
    payload = valid_transaction_payload()
    payload["amount"] = amount

    with pytest.raises(ValidationError, match="invalid decimal money value"):
        CanonicalTransaction.model_validate(payload)


@pytest.mark.parametrize("amount", ["NaN", "Infinity"])
def test_non_finite_money_is_rejected(amount: str) -> None:
    payload = valid_transaction_payload()
    payload["amount"] = amount

    with pytest.raises(ValidationError, match="money must be finite"):
        CanonicalTransaction.model_validate(payload)


def test_money_with_more_than_two_fractional_digits_is_rejected() -> None:
    payload = valid_transaction_payload()
    payload["amount"] = "-1.001"

    with pytest.raises(ValidationError, match="at most two fractional digits"):
        CanonicalTransaction.model_validate(payload)


def test_money_exceeding_fixed_precision_is_rejected() -> None:
    payload = valid_transaction_payload()
    payload["amount"] = "12345678901234567.89"
    payload["direction"] = "inflow"

    with pytest.raises(ValidationError, match="decimal_max_digits"):
        CanonicalTransaction.model_validate(payload)


@pytest.mark.parametrize(
    ("amount", "direction", "message"),
    [
        ("0.00", "inflow", "cannot be zero"),
        ("5.00", "outflow", "positive amounts must use the inflow"),
        ("-5.00", "inflow", "negative amounts must use the outflow"),
    ],
)
def test_amount_direction_invariants(
    amount: str,
    direction: str,
    message: str,
) -> None:
    payload = valid_transaction_payload()
    payload.update(amount=amount, direction=direction)

    with pytest.raises(ValidationError, match=message):
        CanonicalTransaction.model_validate(payload)


def test_required_fields_and_unknown_fields_are_rejected() -> None:
    payload = valid_transaction_payload()
    del payload["description"]
    payload["unexpected"] = "value"

    with pytest.raises(ValidationError) as error:
        CanonicalTransaction.model_validate(payload)

    assert error.value.error_count() == 2


def test_version_one_currency_and_category_identifier_are_strict() -> None:
    payload = valid_transaction_payload()
    payload.update(currency="USD", category_id="Eating Out")

    with pytest.raises(ValidationError) as error:
        CanonicalTransaction.model_validate(payload)

    assert error.value.error_count() == 2


def test_transaction_draft_allows_incomplete_and_inconsistent_extraction() -> None:
    empty_draft = TransactionDraft()
    extracted_draft = TransactionDraft.model_validate(
        {
            "description": "  uncertain row ",
            "amount": "5.00",
            "direction": Direction.OUTFLOW,
        }
    )

    assert empty_draft.amount is None
    assert extracted_draft.description == "uncertain row"
    assert extracted_draft.amount == Decimal("5.00")
    assert extracted_draft.direction is Direction.OUTFLOW
