"""Contracts for deterministic, explainable transaction categorisation."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.categories import CategoryId, CategoryTaxonomy
from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.transactions import Direction, Identifier

RuleId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)]
RuleText = Annotated[str, Field(min_length=1, max_length=500)]


def normalise_rule_text(value: str) -> str:
    """Return case-insensitive whole-token matching text without source mutation."""
    normalised = unicodedata.normalize("NFKC", value).casefold()
    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in normalised
    )
    return " ".join(re.sub(r"[\W_]+", " ", without_controls).split())


class _CategorisationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class CategoryDecisionSource(StrEnum):
    """Deterministic precedence tier that selected a category."""

    TRANSACTION_DECISION = "transaction_decision"
    PERSONAL_RULE = "personal_rule"
    MERCHANT_MAPPING = "merchant_mapping"
    KEYWORD_RULE = "keyword_rule"
    ML_MODEL = "ml_model"
    NEEDS_REVIEW = "needs_review"


class CategoryMatchField(StrEnum):
    """Controlled transaction facts used by one rule without exposing values."""

    MERCHANT = "merchant"
    DIRECTION = "direction"
    ACCOUNT = "account"
    DESCRIPTION = "description"
    AMOUNT = "amount"


class CategoryExplanationCode(StrEnum):
    """Stable privacy-safe explanation for a category decision."""

    TRANSACTION_DECISION_PRESERVED = "transaction_decision_preserved"
    PERSONAL_RULE_MATCH = "personal_rule_match"
    MERCHANT_MAPPING_MATCH = "merchant_mapping_match"
    KEYWORD_RULE_MATCH = "keyword_rule_match"
    AMBIGUOUS_PERSONAL_RULES = "ambiguous_personal_rules"
    AMBIGUOUS_KEYWORD_RULES = "ambiguous_keyword_rules"
    NO_DETERMINISTIC_MATCH = "no_deterministic_match"
    ML_CONFIDENCE_ACCEPTED = "ml_confidence_accepted"
    ML_CONFIDENCE_REVIEW = "ml_confidence_review"
    USER_CATEGORY_FEEDBACK = "user_category_feedback"


class ScopedCategoryRule(_CategorisationModel):
    """One local personal rule anchored to an exact normalised merchant."""

    rule_id: RuleId
    user_profile_id: Identifier
    category_id: CategoryId
    merchant: RuleText
    direction: Direction | None = None
    account_id: Identifier | None = None
    description_contains: RuleText | None = None
    minimum_amount: Money | None = None
    maximum_amount: Money | None = None
    priority: int = Field(default=0, ge=0, le=10_000)
    is_active: bool = True

    @model_validator(mode="after")
    def validate_match_scope(self) -> ScopedCategoryRule:
        """Require usable normalised text and an ordered absolute amount range."""
        if not normalise_rule_text(self.merchant):
            msg = "personal-rule merchant must contain searchable text"
            raise ValueError(msg)
        if self.description_contains is not None and not normalise_rule_text(
            self.description_contains
        ):
            msg = "personal-rule description phrase must contain searchable text"
            raise ValueError(msg)
        if self.minimum_amount is not None and self.minimum_amount < 0:
            msg = "personal-rule minimum amount must be non-negative"
            raise ValueError(msg)
        if self.maximum_amount is not None and self.maximum_amount < 0:
            msg = "personal-rule maximum amount must be non-negative"
            raise ValueError(msg)
        if (
            self.minimum_amount is not None
            and self.maximum_amount is not None
            and self.maximum_amount < self.minimum_amount
        ):
            msg = "personal-rule maximum amount must not be below its minimum"
            raise ValueError(msg)
        return self


class MerchantCategoryMapping(_CategorisationModel):
    """Versioned exact-merchant aliases mapped to one taxonomy category."""

    rule_id: RuleId
    aliases: tuple[RuleText, ...] = Field(min_length=1)
    category_id: CategoryId

    @model_validator(mode="after")
    def validate_aliases(self) -> MerchantCategoryMapping:
        """Require unique, searchable aliases within one merchant mapping."""
        normalised = tuple(normalise_rule_text(alias) for alias in self.aliases)
        if any(not alias for alias in normalised):
            msg = "merchant aliases must contain searchable text"
            raise ValueError(msg)
        if len(set(normalised)) != len(normalised):
            msg = "merchant aliases must be unique after normalisation"
            raise ValueError(msg)
        return self


class KeywordCategoryRule(_CategorisationModel):
    """Versioned whole-phrase description rule with optional direction scope."""

    rule_id: RuleId
    phrase: RuleText
    category_id: CategoryId
    direction: Direction | None = None
    priority: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_phrase(self) -> KeywordCategoryRule:
        """Require a keyword phrase that remains searchable after normalisation."""
        if not normalise_rule_text(self.phrase):
            msg = "keyword phrase must contain searchable text"
            raise ValueError(msg)
        return self


class CategoryRuleSet(_CategorisationModel):
    """Versioned deterministic merchant and keyword rule configuration."""

    version: str = Field(min_length=1, max_length=50)
    taxonomy_version: str = Field(min_length=1, max_length=50)
    merchant_mappings: tuple[MerchantCategoryMapping, ...] = ()
    keyword_rules: tuple[KeywordCategoryRule, ...] = ()

    @model_validator(mode="after")
    def validate_rule_identity(self) -> CategoryRuleSet:
        """Reject duplicate IDs, aliases, and keyword phrases after normalisation."""
        rule_ids = [item.rule_id for item in self.merchant_mappings]
        rule_ids.extend(item.rule_id for item in self.keyword_rules)
        if len(set(rule_ids)) != len(rule_ids):
            msg = "category rule IDs must be unique"
            raise ValueError(msg)

        aliases = [
            normalise_rule_text(alias)
            for mapping in self.merchant_mappings
            for alias in mapping.aliases
        ]
        if len(set(aliases)) != len(aliases):
            msg = "merchant aliases cannot appear in multiple mappings"
            raise ValueError(msg)

        phrases = [normalise_rule_text(rule.phrase) for rule in self.keyword_rules]
        if len(set(phrases)) != len(phrases):
            msg = "keyword phrases must be unique after normalisation"
            raise ValueError(msg)
        return self


class CategorisationPlan(_CategorisationModel):
    """Owned transaction selection and local personal rules for one rule run."""

    user_profile_id: Identifier
    transaction_ids: tuple[Identifier, ...] | None = None
    personal_rules: tuple[ScopedCategoryRule, ...] = ()

    @model_validator(mode="after")
    def validate_selection(self) -> CategorisationPlan:
        """Require unique transaction and personal-rule identities for the user."""
        if self.transaction_ids is not None:
            if not self.transaction_ids:
                msg = "an explicit transaction selection cannot be empty"
                raise ValueError(msg)
            if len(set(self.transaction_ids)) != len(self.transaction_ids):
                msg = "categorisation transaction IDs must be unique"
                raise ValueError(msg)
        rule_ids = [rule.rule_id for rule in self.personal_rules]
        if len(set(rule_ids)) != len(rule_ids):
            msg = "personal category rule IDs must be unique"
            raise ValueError(msg)
        if any(
            rule.user_profile_id != self.user_profile_id for rule in self.personal_rules
        ):
            msg = "personal category rules must belong to the selected profile"
            raise ValueError(msg)
        return self


class CategoryExplanation(_CategorisationModel):
    """Privacy-safe reason for one deterministic category selection."""

    source: CategoryDecisionSource
    code: CategoryExplanationCode
    message: str = Field(min_length=1, max_length=250)
    rule_id: Identifier | None = None
    matched_fields: tuple[CategoryMatchField, ...] = ()


class CategoryDecision(_CategorisationModel):
    """Category assignment result and explanation for one verified transaction."""

    transaction_id: Identifier
    previous_category_id: CategoryId | None
    category_id: CategoryId
    taxonomy_version: str = Field(min_length=1, max_length=50)
    rule_set_version: str = Field(min_length=1, max_length=50)
    changed: bool
    explanation: CategoryExplanation


def validate_rule_set_taxonomy(
    rule_set: CategoryRuleSet,
    taxonomy: CategoryTaxonomy,
) -> CategoryRuleSet:
    """Require every configured target to be active in the declared taxonomy."""
    if rule_set.taxonomy_version != taxonomy.version:
        msg = "category rule set and taxonomy versions must match"
        raise ValueError(msg)
    categories = {category.id: category for category in taxonomy.categories}
    target_ids = [rule.category_id for rule in rule_set.merchant_mappings]
    target_ids.extend(rule.category_id for rule in rule_set.keyword_rules)
    for category_id in target_ids:
        category = categories.get(category_id)
        if category is None:
            msg = f"unknown category rule target: {category_id}"
            raise ValueError(msg)
        if not category.is_active:
            msg = f"inactive category rule target: {category_id}"
            raise ValueError(msg)
    fallback = categories.get("needs_review")
    if fallback is None or not fallback.is_active:
        msg = "taxonomy requires an active needs_review fallback"
        raise ValueError(msg)
    return rule_set


def load_category_rule_set(
    path: Path,
    taxonomy: CategoryTaxonomy,
) -> CategoryRuleSet:
    """Load YAML rules and validate every target against a taxonomy."""
    with path.open(encoding="utf-8") as rule_file:
        payload = yaml.safe_load(rule_file)
    return validate_rule_set_taxonomy(
        CategoryRuleSet.model_validate(payload),
        taxonomy,
    )
