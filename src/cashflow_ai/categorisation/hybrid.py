"""Conservative hybrid categorisation and explicit user-feedback workflow."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.categorisation.ml import (
    LoadedTransactionCategoriser,
    build_training_dataset,
    predict_transaction_categories,
)
from cashflow_ai.categorisation.service import (
    CategorisationServiceError,
    CategorisationServiceErrorCode,
    _personal_match,
    _propose_category,
    _validate_category_targets,
)
from cashflow_ai.persistence.base import new_id, utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    CategoryCorrectionRecord,
    CategoryDecisionRecord,
    PersonalCategoryRuleRecord,
)
from cashflow_ai.persistence.repositories import (
    CategorisationRepository,
    UserProfileRepository,
)
from cashflow_ai.schemas.categorisation import (
    CategorisationPlan,
    CategoryDecisionSource,
    CategoryExplanation,
    CategoryExplanationCode,
    CategoryRuleSet,
    ScopedCategoryRule,
    normalise_rule_text,
)
from cashflow_ai.schemas.hybrid_categorisation import (
    CategoryFeedback,
    CategoryFeedbackAction,
    CategoryFeedbackResult,
    HybridCategorisationPlan,
    HybridCategoryDecision,
    HybridDecisionSource,
    HybridDecisionStatus,
    LowConfidenceReviewItem,
    ManualRetrainingDataset,
    deterministic_source,
)
from cashflow_ai.schemas.ml_categorisation import (
    MLCategorisationInput,
    TrainingCutoff,
)
from cashflow_ai.schemas.transactions import Direction


class HybridCategorisationErrorCode(StrEnum):
    """Stable failure codes for the hybrid and feedback boundary."""

    MODEL_NOT_SELECTED = "model_not_selected"
    MODEL_TAXONOMY_MISMATCH = "model_taxonomy_mismatch"
    FEEDBACK_RULE_MISMATCH = "feedback_rule_mismatch"
    PERSONAL_RULE_CONFLICT = "personal_rule_conflict"
    INVALID_FEEDBACK_TIMESTAMP = "invalid_feedback_timestamp"


class HybridCategorisationError(ValueError):
    """Controlled hybrid workflow failure without private transaction text."""

    def __init__(self, code: HybridCategorisationErrorCode, message: str) -> None:
        """Store the stable code beside a privacy-safe message."""
        super().__init__(message)
        self.code = code


def _stored_rules(
    records: tuple[PersonalCategoryRuleRecord, ...],
) -> tuple[ScopedCategoryRule, ...]:
    return tuple(
        ScopedCategoryRule(
            rule_id=record.id,
            user_profile_id=record.user_profile_id,
            category_id=record.category_id,
            merchant=record.merchant,
            direction=Direction(record.direction)
            if record.direction is not None
            else None,
            account_id=record.account_id,
            description_contains=record.description_contains,
            minimum_amount=record.minimum_amount,
            maximum_amount=record.maximum_amount,
            priority=record.priority,
            is_active=record.is_active,
        )
        for record in records
    )


def _same_decision(
    existing: CategoryDecisionRecord | None, candidate: CategoryDecisionRecord
) -> bool:
    return existing is not None and all(
        (
            existing.category_id == candidate.category_id,
            existing.source == candidate.source,
            existing.status == candidate.status,
            existing.confidence == candidate.confidence,
            existing.model_version == candidate.model_version,
            existing.rule_id == candidate.rule_id,
            existing.taxonomy_version == candidate.taxonomy_version,
            existing.rule_set_version == candidate.rule_set_version,
            existing.reason_code == candidate.reason_code,
        )
    )


def hybrid_categorise_verified_transactions(
    factory: sessionmaker[Session],
    *,
    plan: HybridCategorisationPlan,
    rule_set: CategoryRuleSet,
    model: LoadedTransactionCategoriser | None,
) -> tuple[HybridCategoryDecision, ...]:
    """Apply trusted rules first and use ML only for unmatched transactions."""
    if model is not None:
        if not model.metadata.evaluation.candidate_selected:
            raise HybridCategorisationError(
                HybridCategorisationErrorCode.MODEL_NOT_SELECTED,
                "the evaluated model candidate was not selected for hybrid use",
            )
        if model.metadata.manifest.taxonomy_version != rule_set.taxonomy_version:
            raise HybridCategorisationError(
                HybridCategorisationErrorCode.MODEL_TAXONOMY_MISMATCH,
                "model and category rule taxonomy versions do not match",
            )
    decision_time = utc_now()
    with session_scope(factory) as session:
        if UserProfileRepository(session).get(plan.user_profile_id) is None:
            raise CategorisationServiceError(
                CategorisationServiceErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        repository = CategorisationRepository(session)
        transactions = repository.list_transactions_for_profile(
            plan.user_profile_id, transaction_ids=plan.transaction_ids
        )
        if plan.transaction_ids is not None and {
            item.id for item in transactions
        } != set(plan.transaction_ids):
            raise CategorisationServiceError(
                CategorisationServiceErrorCode.TRANSACTION_SCOPE_NOT_FOUND,
                "one or more selected transactions are unavailable to this profile",
            )
        personal_rules = _stored_rules(
            repository.list_personal_rules(plan.user_profile_id)
        )
        deterministic_plan = CategorisationPlan(
            user_profile_id=plan.user_profile_id,
            transaction_ids=plan.transaction_ids,
            personal_rules=personal_rules,
        )
        corrections = repository.latest_category_corrections(
            tuple(item.id for item in transactions)
        )
        _validate_category_targets(
            repository,
            rule_set=rule_set,
            plan=deterministic_plan,
            corrections=corrections,
        )
        results: list[HybridCategoryDecision] = []
        for transaction in transactions:
            proposal = _propose_category(
                transaction,
                correction=corrections.get(transaction.id),
                plan=deterministic_plan,
                rule_set=rule_set,
            )
            source = deterministic_source(proposal.explanation.source)
            status = HybridDecisionStatus.APPLIED
            confidence: float | None = None
            model_version: str | None = None
            category_id = proposal.category_id
            if source is HybridDecisionSource.NEEDS_REVIEW and model is not None:
                prediction = predict_transaction_categories(
                    model,
                    (
                        MLCategorisationInput(
                            transaction_id=transaction.id,
                            merchant=transaction.merchant,
                            description=transaction.description,
                        ),
                    ),
                )[0]
                category_id = prediction.predicted_category_id
                confidence = next(
                    item.probability
                    for item in prediction.probabilities
                    if item.category_id == category_id
                )
                model_version = model.metadata.manifest.model_version
                source = HybridDecisionSource.ML_MODEL
                if confidence >= plan.confidence_threshold:
                    proposal = type(proposal)(
                        category_id=category_id,
                        explanation=CategoryExplanation(
                            source=CategoryDecisionSource.ML_MODEL,
                            code=CategoryExplanationCode.ML_CONFIDENCE_ACCEPTED,
                            message=(
                                "Selected model category met the explicit "
                                "confidence threshold."
                            ),
                        ),
                    )
                else:
                    status = HybridDecisionStatus.PENDING_REVIEW
                    proposal = type(proposal)(
                        category_id=category_id,
                        explanation=CategoryExplanation(
                            source=CategoryDecisionSource.ML_MODEL,
                            code=CategoryExplanationCode.ML_CONFIDENCE_REVIEW,
                            message=(
                                "Model confidence was below the explicit threshold; "
                                "review is required."
                            ),
                        ),
                    )
            categories = repository.list_categories((category_id,))
            if len(categories) != 1 or not categories[0].is_active:
                raise CategorisationServiceError(
                    CategorisationServiceErrorCode.CATEGORY_NOT_FOUND,
                    "proposed category is unavailable or inactive",
                )
            previous = transaction.category_id
            changed = status is HybridDecisionStatus.APPLIED and previous != category_id
            if changed:
                repository.assign_category(transaction, category_id)
            decision_record = CategoryDecisionRecord(
                verified_transaction_id=transaction.id,
                category_id=category_id,
                source=source.value,
                status=status.value,
                confidence=Decimal(str(confidence)) if confidence is not None else None,
                model_version=model_version,
                rule_id=proposal.explanation.rule_id,
                taxonomy_version=rule_set.taxonomy_version,
                rule_set_version=rule_set.version,
                reason_code=proposal.explanation.code.value,
                created_at=decision_time,
            )
            if (
                status is HybridDecisionStatus.APPLIED
                and source is not HybridDecisionSource.NEEDS_REVIEW
            ):
                repository.supersede_pending_decisions(
                    transaction.id, reviewed_at=decision_time
                )
            if not _same_decision(
                repository.latest_decision(transaction.id), decision_record
            ):
                repository.add_decision(decision_record)
            results.append(
                HybridCategoryDecision(
                    transaction_id=transaction.id,
                    previous_category_id=previous,
                    category_id=category_id,
                    source=source,
                    status=status,
                    confidence=confidence,
                    model_version=model_version,
                    changed=changed,
                    explanation=proposal.explanation,
                )
            )
        session.flush()
        return tuple(results)


def list_low_confidence_reviews(
    factory: sessionmaker[Session], *, user_profile_id: str
) -> tuple[LowConfidenceReviewItem, ...]:
    """Return unresolved ML predictions without descriptions or raw values."""
    with session_scope(factory) as session:
        records = CategorisationRepository(session).list_pending_decisions(
            user_profile_id
        )
        return tuple(
            LowConfidenceReviewItem(
                decision_id=item.id,
                transaction_id=item.verified_transaction_id,
                predicted_category_id=item.category_id,
                confidence=float(item.confidence),
                model_version=str(item.model_version),
                created_at=item.created_at,
            )
            for item in records
            if item.confidence is not None and item.model_version is not None
        )


def apply_category_feedback(
    factory: sessionmaker[Session], *, feedback: CategoryFeedback
) -> CategoryFeedbackResult:
    """Apply one explicit correction and optionally the exact rule the user chose."""
    received_at = utc_now()
    with session_scope(factory) as session:
        repository = CategorisationRepository(session)
        transactions = repository.list_transactions_for_profile(
            feedback.user_profile_id, transaction_ids=(feedback.transaction_id,)
        )
        if len(transactions) != 1:
            raise CategorisationServiceError(
                CategorisationServiceErrorCode.TRANSACTION_SCOPE_NOT_FOUND,
                "selected transaction is unavailable to this profile",
            )
        transaction = transactions[0]
        if not transaction.verified_at <= feedback.corrected_at <= received_at:
            raise HybridCategorisationError(
                HybridCategorisationErrorCode.INVALID_FEEDBACK_TIMESTAMP,
                "feedback time must follow verification and cannot be in the future",
            )
        categories = repository.list_categories((feedback.category_id,))
        if len(categories) != 1 or not categories[0].is_active:
            raise CategorisationServiceError(
                CategorisationServiceErrorCode.CATEGORY_NOT_FOUND,
                "feedback category is unavailable or inactive",
            )
        created_rule_id: str | None = None
        if feedback.action is CategoryFeedbackAction.CREATE_PERSONAL_RULE:
            rule = feedback.personal_rule
            assert rule is not None
            if (
                _personal_match(
                    transaction, rule.model_copy(update={"is_active": True})
                )
                is None
            ):
                raise HybridCategorisationError(
                    HybridCategorisationErrorCode.FEEDBACK_RULE_MISMATCH,
                    "personal rule must match the selected transaction's supplied "
                    "merchant, account, direction, description, and amount scopes",
                )
            existing = repository.get_personal_rule(rule.rule_id)
            if existing is not None:
                raise HybridCategorisationError(
                    HybridCategorisationErrorCode.PERSONAL_RULE_CONFLICT,
                    "personal rule identity already exists",
                )
            repository.add_personal_rule(
                PersonalCategoryRuleRecord(
                    id=rule.rule_id,
                    user_profile_id=rule.user_profile_id,
                    category_id=rule.category_id,
                    merchant=normalise_rule_text(rule.merchant),
                    direction=rule.direction.value
                    if rule.direction is not None
                    else None,
                    account_id=rule.account_id,
                    description_contains=(
                        normalise_rule_text(rule.description_contains)
                        if rule.description_contains is not None
                        else None
                    ),
                    minimum_amount=rule.minimum_amount,
                    maximum_amount=rule.maximum_amount,
                    priority=rule.priority,
                    is_active=True,
                    created_at=received_at,
                )
            )
            created_rule_id = rule.rule_id
        latest_correction = repository.latest_category_corrections(
            (transaction.id,)
        ).get(transaction.id)
        latest_decision = repository.latest_decision(transaction.id)
        if (
            latest_correction is not None
            and feedback.corrected_at <= latest_correction.corrected_at
        ) or (
            latest_decision is not None
            and feedback.corrected_at < latest_decision.created_at
        ):
            raise HybridCategorisationError(
                HybridCategorisationErrorCode.INVALID_FEEDBACK_TIMESTAMP,
                "feedback time must follow the transaction's existing decisions",
            )
        previous = transaction.category_id
        correction = repository.add_correction(
            CategoryCorrectionRecord(
                id=new_id(),
                verified_transaction_id=transaction.id,
                previous_category_id=previous,
                new_category_id=feedback.category_id,
                # The caller timestamp is validated for chronology, but the
                # server receipt time is the authoritative as-of boundary.
                corrected_at=received_at,
            )
        )
        repository.assign_category(transaction, feedback.category_id)
        superseded = repository.supersede_pending_decisions(
            transaction.id, reviewed_at=received_at
        )
        session.flush()
        return CategoryFeedbackResult(
            transaction_id=transaction.id,
            previous_category_id=previous,
            category_id=feedback.category_id,
            correction_id=correction.id,
            created_rule_id=created_rule_id,
            superseded_decision_count=superseded,
        )


def prepare_manual_retraining_dataset(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
    taxonomy_version: str,
    cutoff: TrainingCutoff,
) -> ManualRetrainingDataset:
    """Prepare corrected examples; fitting remains a separate manual action."""
    dataset = build_training_dataset(
        factory,
        user_profile_id=user_profile_id,
        taxonomy_version=taxonomy_version,
        cutoff=cutoff,
    )
    return ManualRetrainingDataset(
        user_profile_id=user_profile_id,
        cutoff=cutoff,
        dataset=dataset,
    )


__all__ = [
    "HybridCategorisationError",
    "HybridCategorisationErrorCode",
    "apply_category_feedback",
    "hybrid_categorise_verified_transactions",
    "list_low_confidence_reviews",
    "prepare_manual_retraining_dataset",
]
