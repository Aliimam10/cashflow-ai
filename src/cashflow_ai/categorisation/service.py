"""Deterministic category assignment with explicit, explainable precedence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    CategoryCorrectionRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.persistence.repositories import (
    CategorisationRepository,
    UserProfileRepository,
)
from cashflow_ai.schemas.categorisation import (
    CategorisationPlan,
    CategoryDecision,
    CategoryDecisionSource,
    CategoryExplanation,
    CategoryExplanationCode,
    CategoryMatchField,
    CategoryRuleSet,
    KeywordCategoryRule,
    MerchantCategoryMapping,
    ScopedCategoryRule,
    normalise_rule_text,
)

_NEEDS_REVIEW = "needs_review"
_FIELD_ORDER = {field: index for index, field in enumerate(CategoryMatchField)}


class CategorisationServiceErrorCode(StrEnum):
    """Stable privacy-safe categorisation failures."""

    PROFILE_NOT_FOUND = "profile_not_found"
    TRANSACTION_SCOPE_NOT_FOUND = "transaction_scope_not_found"
    CATEGORY_NOT_FOUND = "category_not_found"
    CATEGORY_INACTIVE = "category_inactive"
    TAXONOMY_VERSION_MISMATCH = "taxonomy_version_mismatch"


class CategorisationServiceError(ValueError):
    """Controlled category-service failure without private transaction values."""

    def __init__(self, code: CategorisationServiceErrorCode, message: str) -> None:
        """Store a stable public code beside a privacy-safe message."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _ProposedCategory:
    category_id: str
    explanation: CategoryExplanation


@dataclass(frozen=True)
class _PersonalMatch:
    rule: ScopedCategoryRule
    fields: tuple[CategoryMatchField, ...]

    @property
    def rank(self) -> tuple[int, int]:
        return (self.rule.priority, len(self.fields))


@dataclass(frozen=True)
class _KeywordMatch:
    rule: KeywordCategoryRule
    fields: tuple[CategoryMatchField, ...]
    normalised_phrase: str

    @property
    def rank(self) -> tuple[int, int, int, int]:
        return (
            self.rule.priority,
            int(self.rule.direction is not None),
            len(self.normalised_phrase.split()),
            len(self.normalised_phrase),
        )


def _ordered_fields(
    fields: set[CategoryMatchField],
) -> tuple[CategoryMatchField, ...]:
    return tuple(sorted(fields, key=_FIELD_ORDER.__getitem__))


def _contains_phrase(value: str, phrase: str) -> bool:
    value_tokens = normalise_rule_text(value).split()
    phrase_tokens = normalise_rule_text(phrase).split()
    width = len(phrase_tokens)
    return any(
        value_tokens[index : index + width] == phrase_tokens
        for index in range(len(value_tokens) - width + 1)
    )


def _personal_match(
    transaction: VerifiedTransactionRecord,
    rule: ScopedCategoryRule,
) -> _PersonalMatch | None:
    if not rule.is_active or transaction.merchant is None:
        return None
    if normalise_rule_text(transaction.merchant) != normalise_rule_text(rule.merchant):
        return None
    fields = [CategoryMatchField.MERCHANT]
    if rule.direction is not None:
        if transaction.direction != rule.direction.value:
            return None
        fields.append(CategoryMatchField.DIRECTION)
    if rule.account_id is not None:
        if transaction.account_id != rule.account_id:
            return None
        fields.append(CategoryMatchField.ACCOUNT)
    if rule.description_contains is not None:
        if not _contains_phrase(transaction.description, rule.description_contains):
            return None
        fields.append(CategoryMatchField.DESCRIPTION)
    magnitude = abs(transaction.amount)
    if rule.minimum_amount is not None or rule.maximum_amount is not None:
        if rule.minimum_amount is not None and magnitude < rule.minimum_amount:
            return None
        if rule.maximum_amount is not None and magnitude > rule.maximum_amount:
            return None
        fields.append(CategoryMatchField.AMOUNT)
    return _PersonalMatch(rule=rule, fields=tuple(fields))


def _personal_proposal(
    transaction: VerifiedTransactionRecord,
    rules: tuple[ScopedCategoryRule, ...],
) -> _ProposedCategory | None:
    matches = tuple(
        match
        for rule in rules
        if (match := _personal_match(transaction, rule)) is not None
    )
    if not matches:
        return None
    top_rank = max(match.rank for match in matches)
    finalists = tuple(match for match in matches if match.rank == top_rank)
    fields = _ordered_fields({field for match in finalists for field in match.fields})
    if len({match.rule.category_id for match in finalists}) > 1:
        return _ProposedCategory(
            category_id=_NEEDS_REVIEW,
            explanation=CategoryExplanation(
                source=CategoryDecisionSource.NEEDS_REVIEW,
                code=CategoryExplanationCode.AMBIGUOUS_PERSONAL_RULES,
                message="Equally ranked personal rules disagree; review is required.",
                matched_fields=fields,
            ),
        )
    selected = min(finalists, key=lambda match: match.rule.rule_id)
    return _ProposedCategory(
        category_id=selected.rule.category_id,
        explanation=CategoryExplanation(
            source=CategoryDecisionSource.PERSONAL_RULE,
            code=CategoryExplanationCode.PERSONAL_RULE_MATCH,
            message="Matched an active scoped personal category rule.",
            rule_id=selected.rule.rule_id,
            matched_fields=selected.fields,
        ),
    )


def _merchant_proposal(
    transaction: VerifiedTransactionRecord,
    mappings: tuple[MerchantCategoryMapping, ...],
) -> _ProposedCategory | None:
    if transaction.merchant is None:
        return None
    merchant = normalise_rule_text(transaction.merchant)
    for mapping in mappings:
        if merchant in {normalise_rule_text(alias) for alias in mapping.aliases}:
            return _ProposedCategory(
                category_id=mapping.category_id,
                explanation=CategoryExplanation(
                    source=CategoryDecisionSource.MERCHANT_MAPPING,
                    code=CategoryExplanationCode.MERCHANT_MAPPING_MATCH,
                    message="Matched an exact known-merchant mapping.",
                    rule_id=mapping.rule_id,
                    matched_fields=(CategoryMatchField.MERCHANT,),
                ),
            )
    return None


def _keyword_match(
    transaction: VerifiedTransactionRecord,
    rule: KeywordCategoryRule,
) -> _KeywordMatch | None:
    if rule.direction is not None and transaction.direction != rule.direction.value:
        return None
    if not _contains_phrase(transaction.description, rule.phrase):
        return None
    fields = [CategoryMatchField.DESCRIPTION]
    if rule.direction is not None:
        fields.append(CategoryMatchField.DIRECTION)
    return _KeywordMatch(
        rule=rule,
        fields=_ordered_fields(set(fields)),
        normalised_phrase=normalise_rule_text(rule.phrase),
    )


def _keyword_proposal(
    transaction: VerifiedTransactionRecord,
    rules: tuple[KeywordCategoryRule, ...],
) -> _ProposedCategory | None:
    matches = tuple(
        match
        for rule in rules
        if (match := _keyword_match(transaction, rule)) is not None
    )
    if not matches:
        return None
    top_rank = max(match.rank for match in matches)
    finalists = tuple(match for match in matches if match.rank == top_rank)
    fields = _ordered_fields({field for match in finalists for field in match.fields})
    if len({match.rule.category_id for match in finalists}) > 1:
        return _ProposedCategory(
            category_id=_NEEDS_REVIEW,
            explanation=CategoryExplanation(
                source=CategoryDecisionSource.NEEDS_REVIEW,
                code=CategoryExplanationCode.AMBIGUOUS_KEYWORD_RULES,
                message="Equally ranked keyword rules disagree; review is required.",
                matched_fields=fields,
            ),
        )
    selected = min(finalists, key=lambda match: match.rule.rule_id)
    return _ProposedCategory(
        category_id=selected.rule.category_id,
        explanation=CategoryExplanation(
            source=CategoryDecisionSource.KEYWORD_RULE,
            code=CategoryExplanationCode.KEYWORD_RULE_MATCH,
            message="Matched a controlled whole-phrase keyword rule.",
            rule_id=selected.rule.rule_id,
            matched_fields=selected.fields,
        ),
    )


def _transaction_decision(
    correction: CategoryCorrectionRecord,
) -> _ProposedCategory:
    return _ProposedCategory(
        category_id=correction.new_category_id,
        explanation=CategoryExplanation(
            source=CategoryDecisionSource.TRANSACTION_DECISION,
            code=CategoryExplanationCode.TRANSACTION_DECISION_PRESERVED,
            message="Preserved the latest explicit transaction category decision.",
            rule_id=correction.id,
        ),
    )


def _fallback() -> _ProposedCategory:
    return _ProposedCategory(
        category_id=_NEEDS_REVIEW,
        explanation=CategoryExplanation(
            source=CategoryDecisionSource.NEEDS_REVIEW,
            code=CategoryExplanationCode.NO_DETERMINISTIC_MATCH,
            message="No deterministic category rule matched; review is required.",
        ),
    )


def _propose_category(
    transaction: VerifiedTransactionRecord,
    *,
    correction: CategoryCorrectionRecord | None,
    plan: CategorisationPlan,
    rule_set: CategoryRuleSet,
) -> _ProposedCategory:
    if correction is not None:
        return _transaction_decision(correction)
    personal = _personal_proposal(transaction, plan.personal_rules)
    if personal is not None:
        return personal
    merchant = _merchant_proposal(transaction, rule_set.merchant_mappings)
    if merchant is not None:
        return merchant
    keyword = _keyword_proposal(transaction, rule_set.keyword_rules)
    if keyword is not None:
        return keyword
    return _fallback()


def _validate_category_targets(
    repository: CategorisationRepository,
    *,
    rule_set: CategoryRuleSet,
    plan: CategorisationPlan,
    corrections: dict[str, CategoryCorrectionRecord],
) -> None:
    configured_ids = {
        _NEEDS_REVIEW,
        *(rule.category_id for rule in rule_set.merchant_mappings),
        *(rule.category_id for rule in rule_set.keyword_rules),
        *(rule.category_id for rule in plan.personal_rules if rule.is_active),
    }
    explicit_ids = {correction.new_category_id for correction in corrections.values()}
    requested_ids = configured_ids | explicit_ids
    categories = {
        category.id: category
        for category in repository.list_categories(tuple(sorted(requested_ids)))
    }
    missing = requested_ids - categories.keys()
    if missing:
        raise CategorisationServiceError(
            CategorisationServiceErrorCode.CATEGORY_NOT_FOUND,
            "one or more category targets do not exist",
        )
    if any(
        categories[category_id].taxonomy_version != rule_set.taxonomy_version
        for category_id in requested_ids
    ):
        raise CategorisationServiceError(
            CategorisationServiceErrorCode.TAXONOMY_VERSION_MISMATCH,
            "category targets do not match the configured taxonomy version",
        )
    if any(not categories[category_id].is_active for category_id in configured_ids):
        raise CategorisationServiceError(
            CategorisationServiceErrorCode.CATEGORY_INACTIVE,
            "automatic category rules require active category targets",
        )


def categorise_verified_transactions(
    factory: sessionmaker[Session],
    *,
    plan: CategorisationPlan,
    rule_set: CategoryRuleSet,
) -> tuple[CategoryDecision, ...]:
    """Assign explainable categories atomically without changing financial roles."""
    with session_scope(factory) as session:
        if UserProfileRepository(session).get(plan.user_profile_id) is None:
            raise CategorisationServiceError(
                CategorisationServiceErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        repository = CategorisationRepository(session)
        transactions = repository.list_transactions_for_profile(
            plan.user_profile_id,
            transaction_ids=plan.transaction_ids,
        )
        if plan.transaction_ids is not None and {
            transaction.id for transaction in transactions
        } != set(plan.transaction_ids):
            raise CategorisationServiceError(
                CategorisationServiceErrorCode.TRANSACTION_SCOPE_NOT_FOUND,
                "one or more selected transactions are unavailable to this profile",
            )
        corrections = repository.latest_category_corrections(
            tuple(transaction.id for transaction in transactions)
        )
        _validate_category_targets(
            repository,
            rule_set=rule_set,
            plan=plan,
            corrections=corrections,
        )

        decisions: list[CategoryDecision] = []
        for transaction in transactions:
            proposal = _propose_category(
                transaction,
                correction=corrections.get(transaction.id),
                plan=plan,
                rule_set=rule_set,
            )
            previous = transaction.category_id
            changed = previous != proposal.category_id
            if changed:
                repository.assign_category(transaction, proposal.category_id)
            decisions.append(
                CategoryDecision(
                    transaction_id=transaction.id,
                    previous_category_id=previous,
                    category_id=proposal.category_id,
                    taxonomy_version=rule_set.taxonomy_version,
                    rule_set_version=rule_set.version,
                    changed=changed,
                    explanation=proposal.explanation,
                )
            )
        session.flush()
        return tuple(decisions)
