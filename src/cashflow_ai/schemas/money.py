"""Fixed-precision money types and validation."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, cast

from pydantic import BeforeValidator, Field, PlainSerializer

MONEY_QUANTUM = Decimal("0.01")
MAX_MONEY_DIGITS = 18


def _parse_money(value: object) -> Decimal:
    if isinstance(value, float | bool):
        msg = "money must be supplied as a Decimal, integer, or decimal string"
        raise ValueError(msg)
    try:
        if isinstance(value, Decimal):
            parsed = value
        elif isinstance(value, int | str):
            parsed = Decimal(value)
        else:
            msg = "invalid decimal money value"
            raise ValueError(msg)
    except (InvalidOperation, TypeError, ValueError) as exc:
        msg = "invalid decimal money value"
        raise ValueError(msg) from exc
    if not parsed.is_finite():
        msg = "money must be finite"
        raise ValueError(msg)
    exponent = cast(int, parsed.as_tuple().exponent)
    if exponent < -2:
        msg = "money must have at most two fractional digits"
        raise ValueError(msg)
    return parsed.quantize(MONEY_QUANTUM)


type Money = Annotated[
    Decimal,
    BeforeValidator(_parse_money),
    Field(max_digits=MAX_MONEY_DIGITS, decimal_places=2),
    PlainSerializer(
        lambda value: format(value, ".2f"), return_type=str, when_used="json"
    ),
]
