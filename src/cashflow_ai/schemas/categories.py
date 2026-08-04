"""Validated category-taxonomy contracts and loading."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CategoryId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class CategoryDefinition(BaseModel):
    """One category or subcategory in a versioned taxonomy."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: CategoryId
    name: str = Field(min_length=1, max_length=100)
    parent_id: CategoryId | None = None
    is_income: bool = False
    is_transfer: bool = False
    is_active: bool = True


class CategoryTaxonomy(BaseModel):
    """A validated versioned collection of financial categories."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    version: str = Field(min_length=1, max_length=50)
    categories: tuple[CategoryDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_hierarchy(self) -> CategoryTaxonomy:
        """Require unique, resolvable, acyclic category relationships."""
        identifiers = [category.id for category in self.categories]
        names = [category.name.casefold() for category in self.categories]
        if len(identifiers) != len(set(identifiers)):
            msg = "category IDs must be unique"
            raise ValueError(msg)
        if len(names) != len(set(names)):
            msg = "category names must be unique"
            raise ValueError(msg)

        categories_by_id = {category.id: category for category in self.categories}
        for category in self.categories:
            if category.parent_id == category.id:
                msg = "a category cannot be its own parent"
                raise ValueError(msg)
            if (
                category.parent_id is not None
                and category.parent_id not in categories_by_id
            ):
                msg = f"unknown parent category: {category.parent_id}"
                raise ValueError(msg)

            visited = {category.id}
            parent_id = category.parent_id
            while parent_id is not None:
                if parent_id in visited:
                    msg = "category hierarchy cannot contain cycles"
                    raise ValueError(msg)
                visited.add(parent_id)
                parent_id = categories_by_id[parent_id].parent_id
        return self


def load_taxonomy(path: Path) -> CategoryTaxonomy:
    """Load and validate a YAML category taxonomy."""
    with path.open(encoding="utf-8") as taxonomy_file:
        payload = yaml.safe_load(taxonomy_file)
    return CategoryTaxonomy.model_validate(payload)
