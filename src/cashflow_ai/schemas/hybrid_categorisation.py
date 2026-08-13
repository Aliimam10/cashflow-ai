"""Contracts for hybrid categorisation, review, and explicit feedback."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.categories import CategoryId
from cashflow_ai.schemas.categorisation import (
    CategoryDecisionSource,
    CategoryExplanation,
    ScopedCategoryRule,
)
from cashflow_ai.schemas.ml_categorisation import MLTrainingDataset, TrainingCutoff
from cashflow_ai.schemas.transactions import Identifier

Confidence = Annotated[float, Field(ge=0, le=1)]


class _HybridModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class HybridDecisionSource(StrEnum):
    """Audited origin of a hybrid category decision."""

    TRANSACTION_DECISION = "transaction_decision"
    PERSONAL_RULE = "personal_rule"
    MERCHANT_MAPPING = "merchant_mapping"
    KEYWORD_RULE = "keyword_rule"
    ML_MODEL = "ml_model"
    NEEDS_REVIEW = "needs_review"


class HybridDecisionStatus(StrEnum):
    """Lifecycle state of an audited category decision."""

    APPLIED = "applied"
    PENDING_REVIEW = "pending_review"
    SUPERSEDED = "superseded"


class CategoryFeedbackAction(StrEnum):
    """Explicit scope selected by the user for one correction."""

    TRANSACTION_ONLY = "transaction_only"
    CREATE_PERSONAL_RULE = "create_personal_rule"


class HybridCategorisationPlan(_HybridModel):
    """One owned hybrid run with an explicit ML auto-apply threshold."""

    user_profile_id: Identifier
    confidence_threshold: Confidence
    transaction_ids: tuple[Identifier, ...] | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> HybridCategorisationPlan:
        """Reject ambiguous or duplicate explicit selections."""
        if self.transaction_ids is not None:
            if not self.transaction_ids:
                raise ValueError("an explicit transaction selection cannot be empty")
            if len(set(self.transaction_ids)) != len(self.transaction_ids):
                raise ValueError("hybrid transaction IDs must be unique")
        return self


class HybridCategoryDecision(_HybridModel):
    """One applied decision or review-only ML suggestion."""

    transaction_id: Identifier
    previous_category_id: CategoryId | None
    category_id: CategoryId
    source: HybridDecisionSource
    status: HybridDecisionStatus
    confidence: Confidence | None = None
    model_version: str | None = Field(default=None, max_length=100)
    changed: bool
    explanation: CategoryExplanation

    @model_validator(mode="after")
    def validate_model_evidence(self) -> HybridCategoryDecision:
        """Bind model evidence to ML choices and keep pending rows non-mutating."""
        is_ml = self.source is HybridDecisionSource.ML_MODEL
        if is_ml != (self.confidence is not None and self.model_version is not None):
            raise ValueError("ML decisions require confidence and model version only")
        if self.status is HybridDecisionStatus.PENDING_REVIEW and self.changed:
            raise ValueError("pending predictions must not change the category")
        return self


class CategoryFeedback(_HybridModel):
    """Explicit user choice; a reusable rule is never inferred automatically."""

    user_profile_id: Identifier
    transaction_id: Identifier
    category_id: CategoryId
    action: CategoryFeedbackAction
    corrected_at: datetime
    personal_rule: ScopedCategoryRule | None = None

    @model_validator(mode="after")
    def validate_feedback(self) -> CategoryFeedback:
        """Require aware audit time and the user's exact rule-creation choice."""
        if self.corrected_at.tzinfo is None or self.corrected_at.utcoffset() is None:
            raise ValueError("corrected_at must be timezone-aware")
        needs_rule = self.action is CategoryFeedbackAction.CREATE_PERSONAL_RULE
        if needs_rule != (self.personal_rule is not None):
            raise ValueError("personal rule is required only for create_personal_rule")
        if self.personal_rule is not None:
            if self.personal_rule.user_profile_id != self.user_profile_id:
                raise ValueError("personal rule must belong to the selected profile")
            if self.personal_rule.category_id != self.category_id:
                raise ValueError("personal rule and feedback category must match")
        return self


class CategoryFeedbackResult(_HybridModel):
    """Auditable result of one atomic correction action."""

    transaction_id: Identifier
    previous_category_id: CategoryId | None
    category_id: CategoryId
    correction_id: Identifier
    created_rule_id: Identifier | None = None
    superseded_decision_count: int = Field(ge=0)


class LowConfidenceReviewItem(_HybridModel):
    """Privacy-safe pending prediction for a later user decision."""

    decision_id: Identifier
    transaction_id: Identifier
    predicted_category_id: CategoryId
    confidence: Confidence
    model_version: str
    created_at: datetime


class ManualRetrainingDataset(_HybridModel):
    """Cutoff-safe corrected examples prepared for an explicitly started retrain."""

    user_profile_id: Identifier
    cutoff: TrainingCutoff
    dataset: MLTrainingDataset

    @model_validator(mode="after")
    def validate_cutoff(self) -> ManualRetrainingDataset:
        """Prevent a wrapper from mislabelling the dataset knowledge cutoff."""
        if self.dataset.cutoff != self.cutoff:
            raise ValueError("manual retraining dataset cutoff must match")
        return self


def deterministic_source(source: CategoryDecisionSource) -> HybridDecisionSource:
    """Map the Commit 18 source enum to the durable hybrid source enum."""
    return HybridDecisionSource(source.value)


__all__ = [
    "CategoryFeedback",
    "CategoryFeedbackAction",
    "CategoryFeedbackResult",
    "HybridCategorisationPlan",
    "HybridCategoryDecision",
    "HybridDecisionSource",
    "HybridDecisionStatus",
    "LowConfidenceReviewItem",
    "ManualRetrainingDataset",
    "deterministic_source",
]
