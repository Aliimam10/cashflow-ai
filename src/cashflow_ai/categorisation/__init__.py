"""Public deterministic transaction categorisation boundary."""

from cashflow_ai.categorisation.service import (
    CategorisationServiceError,
    CategorisationServiceErrorCode,
    categorise_verified_transactions,
)

__all__ = [
    "CategorisationServiceError",
    "CategorisationServiceErrorCode",
    "categorise_verified_transactions",
]
