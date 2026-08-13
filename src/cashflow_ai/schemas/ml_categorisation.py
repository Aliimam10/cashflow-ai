"""Typed contracts for leakage-safe transaction-category model development."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.categories import CategoryId
from cashflow_ai.schemas.transactions import Identifier

Probability = Annotated[float, Field(ge=0, le=1)]
SafeModelVersion = Annotated[
    str,
    Field(pattern=r"^[a-z0-9][a-z0-9._-]*$", min_length=1, max_length=100),
]
_SELECTED_REASON = (
    "candidate beats the most-frequent baseline on both required holdouts"
)
_NOT_SELECTED_REASON = "candidate does not consistently beat the required baseline"


class _MLModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class TrainingCutoff(_MLModel):
    """Transaction and knowledge boundaries for one historical data view."""

    transaction_date: date
    knowledge_cutoff_at: AwareDatetime


class MLTrainingPlan(_MLModel):
    """Explicit reproducible policy for training and evaluating one candidate."""

    user_profile_id: Identifier
    model_version: SafeModelVersion
    taxonomy_version: str = Field(min_length=1, max_length=50)
    artifact_directory: Path = Path("models/categorisation")
    final_cutoff: TrainingCutoff
    chronological_training_cutoff: TrainingCutoff
    chronological_test_start: date
    unseen_merchant_test_fraction: float = Field(gt=0, lt=1)
    minimum_training_samples: int = Field(default=4, ge=2)
    minimum_test_samples: int = Field(default=2, ge=1)
    random_seed: int = Field(default=42, ge=0)
    trained_at: AwareDatetime

    @model_validator(mode="after")
    def validate_cutoff_order(self) -> MLTrainingPlan:
        """Keep historical training strictly before the held-out period."""
        if (
            self.chronological_training_cutoff.transaction_date
            >= self.chronological_test_start
        ):
            msg = "chronological training cutoff must precede the test period"
            raise ValueError(msg)
        if self.chronological_test_start > self.final_cutoff.transaction_date:
            msg = "chronological test period must fall within the final cutoff"
            raise ValueError(msg)
        if (
            self.chronological_training_cutoff.knowledge_cutoff_at
            > self.final_cutoff.knowledge_cutoff_at
        ):
            msg = "historical knowledge cutoff cannot follow the final cutoff"
            raise ValueError(msg)
        if (
            self.chronological_training_cutoff.knowledge_cutoff_at.date()
            >= self.chronological_test_start
        ):
            msg = "historical knowledge cutoff must precede the test period"
            raise ValueError(msg)
        if self.trained_at < self.final_cutoff.knowledge_cutoff_at:
            msg = "training time cannot precede the final knowledge cutoff"
            raise ValueError(msg)
        return self


class TrainingExclusionReason(StrEnum):
    """Controlled reason that a persisted transaction cannot train the model."""

    TRANSACTION_AFTER_CUTOFF = "transaction_after_cutoff"
    VERIFIED_AFTER_CUTOFF = "verified_after_cutoff"
    SOURCE_LINEAGE_MISMATCH = "source_lineage_mismatch"
    UNCONFIRMED_SOURCE_ROW = "unconfirmed_source_row"
    UNTRUSTED_DOCUMENT = "untrusted_document"
    DUPLICATE_EVIDENCE = "duplicate_evidence"
    NO_AUTHORITATIVE_LABEL = "no_authoritative_label"
    NEEDS_REVIEW_LABEL = "needs_review_label"
    CATEGORY_NOT_FOUND = "category_not_found"
    CATEGORY_INACTIVE = "category_inactive"
    TAXONOMY_MISMATCH = "taxonomy_mismatch"
    UNRESOLVED_FINANCIAL_ROLE = "unresolved_financial_role"
    EXCLUDED_FINANCIAL_ROLE = "excluded_financial_role"
    NEEDS_REVIEW_FLAG = "needs_review_flag"
    UNRESOLVED_TRANSFER = "unresolved_transfer"
    EMPTY_FEATURE_TEXT = "empty_feature_text"


class TrainingExclusionCount(_MLModel):
    """Aggregate exclusion count that contains no transaction-level values."""

    reason: TrainingExclusionReason
    count: int = Field(ge=1)


class MLTrainingExample(_MLModel):
    """Transient supervised example; never write this contract to metadata."""

    transaction_id: Identifier
    transaction_date: date
    verified_at: AwareDatetime
    merchant: str | None = Field(default=None, max_length=500)
    description: str = Field(max_length=500)
    merchant_group: str | None = Field(default=None, max_length=500)
    category_id: CategoryId
    category_decided_at: AwareDatetime


class MLTrainingDataset(_MLModel):
    """Cutoff-specific examples and privacy-safe aggregate exclusions."""

    taxonomy_version: str = Field(min_length=1, max_length=50)
    cutoff: TrainingCutoff
    examples: tuple[MLTrainingExample, ...] = ()
    exclusions: tuple[TrainingExclusionCount, ...] = ()

    @model_validator(mode="after")
    def validate_unique_examples_and_exclusions(self) -> MLTrainingDataset:
        """Require deterministic one-row examples and one count per reason."""
        transaction_ids = [example.transaction_id for example in self.examples]
        if len(transaction_ids) != len(set(transaction_ids)):
            msg = "training examples must have unique transaction identities"
            raise ValueError(msg)
        if any(
            example.transaction_date > self.cutoff.transaction_date
            or example.verified_at > self.cutoff.knowledge_cutoff_at
            or example.category_decided_at > self.cutoff.knowledge_cutoff_at
            for example in self.examples
        ):
            msg = "training examples must be fully known by the dataset cutoff"
            raise ValueError(msg)
        reasons = [item.reason for item in self.exclusions]
        if len(reasons) != len(set(reasons)):
            msg = "training exclusion reasons must be unique"
            raise ValueError(msg)
        return self


class MLHoldoutKind(StrEnum):
    """Supported leakage-aware classifier evaluation partitions."""

    CHRONOLOGICAL = "chronological"
    UNSEEN_MERCHANT = "unseen_merchant"


class MLHoldoutSplit(_MLModel):
    """Private examples assigned to one deterministic evaluation partition."""

    kind: MLHoldoutKind
    training_examples: tuple[MLTrainingExample, ...] = Field(min_length=1)
    test_examples: tuple[MLTrainingExample, ...] = Field(min_length=1)
    omitted_without_merchant: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_disjoint_rows(self) -> MLHoldoutSplit:
        """Prevent the same transaction from entering both sides of a holdout."""
        training_ids = {item.transaction_id for item in self.training_examples}
        test_ids = {item.transaction_id for item in self.test_examples}
        if training_ids & test_ids:
            msg = "holdout training and test transactions must be disjoint"
            raise ValueError(msg)
        if self.kind is MLHoldoutKind.UNSEEN_MERCHANT:
            training_groups = {
                item.merchant_group
                for item in self.training_examples
                if item.merchant_group is not None
            }
            test_groups = {
                item.merchant_group
                for item in self.test_examples
                if item.merchant_group is not None
            }
            if training_groups & test_groups:
                msg = "unseen-merchant groups must be disjoint"
                raise ValueError(msg)
        return self


class CategoryMetric(_MLModel):
    """Per-category classification metrics with explicit test support."""

    category_id: CategoryId
    precision: Probability
    recall: Probability
    f1: Probability
    support: int = Field(ge=0)


class ConfusionMatrix(_MLModel):
    """Stable labelled confusion matrix."""

    labels: tuple[CategoryId, ...] = Field(min_length=1)
    rows: tuple[tuple[int, ...], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_square_matrix(self) -> ConfusionMatrix:
        """Require one non-negative square row for every label."""
        size = len(self.labels)
        if len(set(self.labels)) != size:
            msg = "confusion-matrix labels must be unique"
            raise ValueError(msg)
        if len(self.rows) != size or any(len(row) != size for row in self.rows):
            msg = "confusion matrix must be square and match its labels"
            raise ValueError(msg)
        if any(value < 0 for row in self.rows for value in row):
            msg = "confusion-matrix counts cannot be negative"
            raise ValueError(msg)
        return self


class ClassificationMetrics(_MLModel):
    """Aggregate and per-category classifier evaluation metrics."""

    macro_f1: Probability
    weighted_f1: Probability
    macro_precision: Probability
    weighted_precision: Probability
    macro_recall: Probability
    weighted_recall: Probability
    per_category: tuple[CategoryMetric, ...] = Field(min_length=1)
    confusion_matrix: ConfusionMatrix

    @model_validator(mode="after")
    def validate_category_order(self) -> ClassificationMetrics:
        """Keep per-category metrics aligned with the matrix labels."""
        if tuple(item.category_id for item in self.per_category) != (
            self.confusion_matrix.labels
        ):
            msg = "per-category metrics must follow confusion-matrix label order"
            raise ValueError(msg)
        row_support = tuple(sum(row) for row in self.confusion_matrix.rows)
        if tuple(item.support for item in self.per_category) != row_support:
            msg = "per-category support must match confusion-matrix rows"
            raise ValueError(msg)
        return self


class HoldoutEvaluation(_MLModel):
    """Candidate and baseline results on one identical holdout."""

    kind: MLHoldoutKind
    training_count: int = Field(ge=1)
    test_count: int = Field(ge=1)
    training_start_date: date
    training_end_date: date
    test_start_date: date
    test_end_date: date
    training_merchant_groups: int = Field(ge=0)
    test_merchant_groups: int = Field(ge=0)
    omitted_without_merchant: int = Field(default=0, ge=0)
    candidate: ClassificationMetrics
    baseline: ClassificationMetrics

    @model_validator(mode="after")
    def validate_date_ranges(self) -> HoldoutEvaluation:
        """Require internally ordered training and test date ranges."""
        if self.training_end_date < self.training_start_date:
            msg = "holdout training date range is invalid"
            raise ValueError(msg)
        if self.test_end_date < self.test_start_date:
            msg = "holdout test date range is invalid"
            raise ValueError(msg)
        if (
            self.kind is MLHoldoutKind.CHRONOLOGICAL
            and self.training_end_date >= self.test_start_date
        ):
            msg = "chronological training dates must precede test dates"
            raise ValueError(msg)
        if sum(sum(row) for row in self.candidate.confusion_matrix.rows) != (
            self.test_count
        ):
            msg = "candidate confusion matrix must account for every test sample"
            raise ValueError(msg)
        if sum(sum(row) for row in self.baseline.confusion_matrix.rows) != (
            self.test_count
        ):
            msg = "baseline confusion matrix must account for every test sample"
            raise ValueError(msg)
        candidate_support = tuple(item.support for item in self.candidate.per_category)
        baseline_support = tuple(item.support for item in self.baseline.per_category)
        if (
            self.candidate.confusion_matrix.labels
            != self.baseline.confusion_matrix.labels
            or candidate_support != baseline_support
        ):
            msg = "candidate and baseline must use the same labelled test rows"
            raise ValueError(msg)
        return self


class MLCategorisationEvaluation(_MLModel):
    """Both required holdouts and the transparent candidate recommendation."""

    chronological: HoldoutEvaluation
    unseen_merchant: HoldoutEvaluation
    candidate_selected: bool
    selection_reason: str = Field(min_length=1, max_length=250)

    @model_validator(mode="after")
    def validate_holdout_kinds(self) -> MLCategorisationEvaluation:
        """Require both holdouts and a selection result derived from their metrics."""
        if self.chronological.kind is not MLHoldoutKind.CHRONOLOGICAL:
            msg = "chronological evaluation has the wrong holdout kind"
            raise ValueError(msg)
        if self.unseen_merchant.kind is not MLHoldoutKind.UNSEEN_MERCHANT:
            msg = "unseen-merchant evaluation has the wrong holdout kind"
            raise ValueError(msg)
        expected_selected = (
            self.chronological.candidate.macro_f1 > self.chronological.baseline.macro_f1
            and self.unseen_merchant.candidate.macro_f1
            > self.unseen_merchant.baseline.macro_f1
            and self.chronological.candidate.weighted_f1
            >= self.chronological.baseline.weighted_f1
            and self.unseen_merchant.candidate.weighted_f1
            >= self.unseen_merchant.baseline.weighted_f1
        )
        expected_reason = (
            _SELECTED_REASON if expected_selected else _NOT_SELECTED_REASON
        )
        if (
            self.candidate_selected is not expected_selected
            or self.selection_reason != expected_reason
        ):
            msg = "candidate selection must be derived from the recorded holdouts"
            raise ValueError(msg)
        return self


class MLPipelineParameters(_MLModel):
    """Controlled reproducible parameters for the candidate pipeline."""

    word_ngram_range: tuple[Literal[1], Literal[2]] = (1, 2)
    character_ngram_range: tuple[Literal[3], Literal[5]] = (3, 5)
    character_analyser: Literal["char_wb"] = "char_wb"
    logistic_max_iterations: Literal[1000] = 1_000
    logistic_class_weight: Literal["balanced"] = "balanced"
    random_seed: int = Field(ge=0)


class CategorySupport(_MLModel):
    """Aggregate training support for one category."""

    category_id: CategoryId
    count: int = Field(ge=1)


class MLModelManifest(_MLModel):
    """Controlled identity embedded in both model and metadata files."""

    artifact_format_version: Literal["1.0"] = "1.0"
    model_name: Literal["transaction_category_tfidf_logistic_regression"] = (
        "transaction_category_tfidf_logistic_regression"
    )
    model_version: SafeModelVersion
    taxonomy_version: str = Field(min_length=1, max_length=50)
    feature_schema_version: str = Field(
        default="category_text_v1",
        min_length=1,
        max_length=50,
    )
    classes: tuple[CategoryId, ...] = Field(min_length=2)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_unique_classes(self) -> MLModelManifest:
        """Require a stable unique estimator class order."""
        if len(set(self.classes)) != len(self.classes):
            msg = "model classes must be unique"
            raise ValueError(msg)
        return self


class MLTrainingMetadata(_MLModel):
    """Privacy-safe sidecar for one evaluated local model candidate."""

    manifest: MLModelManifest
    final_cutoff: TrainingCutoff
    chronological_training_cutoff: TrainingCutoff
    chronological_test_start: date
    unseen_merchant_test_fraction: float = Field(gt=0, lt=1)
    minimum_training_samples: int = Field(ge=2)
    minimum_test_samples: int = Field(ge=1)
    parameters: MLPipelineParameters
    training_count: int = Field(ge=2)
    training_start_date: date
    training_end_date: date
    category_support: tuple[CategorySupport, ...] = Field(min_length=2)
    historical_exclusions: tuple[TrainingExclusionCount, ...] = ()
    final_exclusions: tuple[TrainingExclusionCount, ...] = ()
    evaluation: MLCategorisationEvaluation
    python_version: str = Field(min_length=1, max_length=50)
    scikit_learn_version: str = Field(min_length=1, max_length=50)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_training_summary(self) -> MLTrainingMetadata:
        """Keep aggregate support, class order, and date bounds coherent."""
        if self.training_end_date < self.training_start_date:
            msg = "model training date range is invalid"
            raise ValueError(msg)
        if (
            self.chronological_training_cutoff.transaction_date
            >= self.chronological_test_start
            or self.chronological_test_start > self.final_cutoff.transaction_date
            or self.chronological_training_cutoff.knowledge_cutoff_at
            > self.final_cutoff.knowledge_cutoff_at
            or self.chronological_training_cutoff.knowledge_cutoff_at.date()
            >= self.chronological_test_start
        ):
            msg = "model metadata cutoffs are inconsistent"
            raise ValueError(msg)
        if (
            self.manifest.created_at < self.final_cutoff.knowledge_cutoff_at
            or self.training_end_date > self.final_cutoff.transaction_date
        ):
            msg = "model training summary exceeds its recorded cutoff"
            raise ValueError(msg)
        if (
            self.evaluation.chronological.training_end_date
            > self.chronological_training_cutoff.transaction_date
            or self.evaluation.chronological.test_start_date
            < self.chronological_test_start
            or self.evaluation.chronological.test_end_date
            > self.final_cutoff.transaction_date
            or self.evaluation.unseen_merchant.training_end_date
            > self.final_cutoff.transaction_date
            or self.evaluation.unseen_merchant.test_end_date
            > self.final_cutoff.transaction_date
        ):
            msg = "model evaluation ranges exceed their recorded cutoffs"
            raise ValueError(msg)
        if (
            self.evaluation.chronological.training_count < self.minimum_training_samples
            or self.evaluation.chronological.test_count < self.minimum_test_samples
            or self.evaluation.unseen_merchant.training_count
            < self.minimum_training_samples
            or self.evaluation.unseen_merchant.test_count < self.minimum_test_samples
        ):
            msg = "model evaluation does not meet its recorded sample policy"
            raise ValueError(msg)
        if sum(item.count for item in self.category_support) != self.training_count:
            msg = "category support must account for every training example"
            raise ValueError(msg)
        if tuple(item.category_id for item in self.category_support) != (
            self.manifest.classes
        ):
            msg = "category support must follow the model class order"
            raise ValueError(msg)
        if tuple(sorted(self.manifest.classes)) != self.manifest.classes:
            msg = "model classes must use stable sorted order"
            raise ValueError(msg)
        return self


class MLCategoriserTrainingResult(_MLModel):
    """Paths and safe metadata for one locally persisted candidate."""

    artifact_path: Path
    metadata_path: Path
    metadata: MLTrainingMetadata


class MLCategorisationInput(_MLModel):
    """Standalone inference input that is never persisted by Commit 19."""

    transaction_id: Identifier
    merchant: str | None = Field(default=None, max_length=500)
    description: str = Field(max_length=500)


class MLCategoryProbability(_MLModel):
    """One class probability in stable estimator order."""

    category_id: CategoryId
    probability: Probability


class MLCategorisationPrediction(_MLModel):
    """Standalone prediction with no assignment or confidence policy."""

    transaction_id: Identifier
    predicted_category_id: CategoryId
    probabilities: tuple[MLCategoryProbability, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_probabilities(self) -> MLCategorisationPrediction:
        """Require unique classes and a numerically complete probability mass."""
        category_ids = [item.category_id for item in self.probabilities]
        if len(category_ids) != len(set(category_ids)):
            msg = "prediction categories must be unique"
            raise ValueError(msg)
        if self.predicted_category_id not in category_ids:
            msg = "predicted category must appear in the probability list"
            raise ValueError(msg)
        if abs(sum(item.probability for item in self.probabilities) - 1) > 1e-6:
            msg = "category probabilities must sum to one"
            raise ValueError(msg)
        return self
