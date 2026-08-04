"""Tests for the versioned category taxonomy."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from cashflow_ai.schemas import CategoryTaxonomy, load_taxonomy

EXPECTED_NAMES = {
    "Income",
    "Housing",
    "Utilities",
    "Groceries",
    "Eating Out",
    "Transport",
    "Shopping",
    "Health",
    "Education",
    "Entertainment",
    "Travel",
    "Subscriptions",
    "Fees and Charges",
    "Transfers",
    "Cash Withdrawal",
    "Savings",
    "Refund",
    "Other",
    "Needs Review",
}


def test_repository_taxonomy_has_stable_version_one_categories() -> None:
    taxonomy = load_taxonomy(Path("configs/categories.yaml"))

    assert taxonomy.version == "1.0"
    assert {category.name for category in taxonomy.categories} == EXPECTED_NAMES
    assert len(taxonomy.categories) == 19
    assert next(
        category for category in taxonomy.categories if category.id == "income"
    ).is_income
    assert next(
        category for category in taxonomy.categories if category.id == "transfers"
    ).is_transfer


def taxonomy_payload(*categories: dict[str, object]) -> dict[str, object]:
    return {"version": "test", "categories": categories}


def test_valid_parent_hierarchy_is_accepted() -> None:
    taxonomy = CategoryTaxonomy.model_validate(
        taxonomy_payload(
            {"id": "shopping", "name": "Shopping"},
            {
                "id": "electronics",
                "name": "Electronics",
                "parent_id": "shopping",
            },
        )
    )

    assert taxonomy.categories[1].parent_id == "shopping"


@pytest.mark.parametrize(
    ("categories", "message"),
    [
        (
            (
                {"id": "income", "name": "Income"},
                {"id": "income", "name": "Other Income"},
            ),
            "IDs must be unique",
        ),
        (
            (
                {"id": "income", "name": "Income"},
                {"id": "other_income", "name": "income"},
            ),
            "names must be unique",
        ),
        (
            ({"id": "shopping", "name": "Shopping", "parent_id": "shopping"},),
            "cannot be its own parent",
        ),
        (
            ({"id": "electronics", "name": "Electronics", "parent_id": "missing"},),
            "unknown parent category",
        ),
        (
            (
                {"id": "first", "name": "First", "parent_id": "second"},
                {"id": "second", "name": "Second", "parent_id": "first"},
            ),
            "cannot contain cycles",
        ),
    ],
)
def test_invalid_taxonomy_hierarchies_are_rejected(
    categories: tuple[dict[str, object], ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        CategoryTaxonomy.model_validate(taxonomy_payload(*categories))
