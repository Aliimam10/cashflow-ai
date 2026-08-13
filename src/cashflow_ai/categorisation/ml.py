"""Leakage-safe training and evaluation for the local category classifier."""

from __future__ import annotations

import json
import os
import platform
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import joblib  # type: ignore[import-untyped]
import sklearn  # type: ignore[import-untyped]
from pydantic import ValidationError
from sklearn.dummy import DummyClassifier  # type: ignore[import-untyped]
from sklearn.feature_extraction.text import (  # type: ignore[import-untyped]
    TfidfVectorizer,
)
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.metrics import (  # type: ignore[import-untyped]
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
    precision_score,
    recall_score,
)
from sklearn.model_selection import GroupShuffleSplit  # type: ignore[import-untyped]
from sklearn.pipeline import FeatureUnion, Pipeline  # type: ignore[import-untyped]
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import CategoryCorrectionRecord
from cashflow_ai.persistence.repositories import (
    MLCategorisationRepository,
    MLTrainingCandidateRow,
    UserProfileRepository,
)
from cashflow_ai.schemas.categorisation import normalise_rule_text
from cashflow_ai.schemas.ml_categorisation import (
    CategoryMetric,
    CategorySupport,
    ClassificationMetrics,
    ConfusionMatrix,
    HoldoutEvaluation,
    MLCategorisationEvaluation,
    MLCategorisationInput,
    MLCategorisationPrediction,
    MLCategoriserTrainingResult,
    MLCategoryProbability,
    MLHoldoutKind,
    MLHoldoutSplit,
    MLModelManifest,
    MLPipelineParameters,
    MLTrainingDataset,
    MLTrainingExample,
    MLTrainingMetadata,
    MLTrainingPlan,
    TrainingCutoff,
    TrainingExclusionCount,
    TrainingExclusionReason,
)
from cashflow_ai.schemas.transactions import FinancialRole

_FEATURE_SCHEMA_VERSION = "category_text_v1"
_DUPLICATE_CODES = frozenset({"exact_duplicate", "probable_duplicate"})
_DOCUMENT_SOURCES = frozenset({"digital_pdf", "ocr_pdf"})


class MLCategorisationErrorCode(StrEnum):
    """Stable failures that never reveal private transaction text."""

    PROFILE_NOT_FOUND = "profile_not_found"
    NO_ELIGIBLE_SAMPLES = "no_eligible_samples"
    TOO_FEW_CLASSES = "too_few_classes"
    TOO_FEW_TRAINING_SAMPLES = "too_few_training_samples"
    TOO_FEW_TEST_SAMPLES = "too_few_test_samples"
    TOO_FEW_MERCHANT_GROUPS = "too_few_merchant_groups"
    ONE_CLASS_TRAINING_SPLIT = "one_class_training_split"
    EMPTY_FEATURE_TEXT = "empty_feature_text"
    DATASET_PLAN_MISMATCH = "dataset_plan_mismatch"
    ARTIFACT_EXISTS = "artifact_exists"
    ARTIFACT_NOT_FOUND = "artifact_not_found"
    ARTIFACT_CHECKSUM_MISMATCH = "artifact_checksum_mismatch"
    UNTRUSTED_ARTIFACT = "untrusted_artifact"
    INVALID_MODEL_METADATA = "invalid_model_metadata"
    SOFTWARE_VERSION_MISMATCH = "software_version_mismatch"
    TAXONOMY_MISMATCH = "taxonomy_mismatch"
    FEATURE_SCHEMA_MISMATCH = "feature_schema_mismatch"
    ARTIFACT_MANIFEST_MISMATCH = "artifact_manifest_mismatch"


class MLCategorisationError(ValueError):
    """Controlled ML workflow error with a stable privacy-safe code."""

    def __init__(self, code: MLCategorisationErrorCode, message: str) -> None:
        """Store a safe error code and message."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LoadedTransactionCategoriser:
    """Checksum-verified local pipeline and its safe metadata."""

    pipeline: Pipeline
    metadata: MLTrainingMetadata


def build_feature_text(merchant: str | None, description: str) -> str:
    """Build transient normalized text without changing persisted source values."""
    merchant_text = normalise_rule_text(merchant or "")
    description_text = normalise_rule_text(description)
    if not merchant_text and not description_text:
        raise MLCategorisationError(
            MLCategorisationErrorCode.EMPTY_FEATURE_TEXT,
            "transaction contains no usable categorisation text",
        )
    merchant_feature = merchant_text or "missing"
    description_feature = description_text or "missing"
    return f"merchant {merchant_feature} description {description_feature}"


def _candidate_exclusion(
    candidate: MLTrainingCandidateRow,
    *,
    cutoff: TrainingCutoff,
    taxonomy_version: str,
    category_id: str | None,
    category_exists: bool,
    category_active: bool,
    category_taxonomy_version: str | None,
    financial_role: str,
    needs_review_ids: frozenset[str],
    unresolved_transfer_ids: frozenset[str],
) -> TrainingExclusionReason | None:
    if candidate.transaction_date > cutoff.transaction_date:
        return TrainingExclusionReason.TRANSACTION_AFTER_CUTOFF
    if candidate.verified_at > cutoff.knowledge_cutoff_at:
        return TrainingExclusionReason.VERIFIED_AFTER_CUTOFF
    if (
        not candidate.account_lineage_matches
        or candidate.verified_source_type != candidate.batch_source_type
    ):
        return TrainingExclusionReason.SOURCE_LINEAGE_MISMATCH
    if candidate.raw_review_status != "confirmed":
        return TrainingExclusionReason.UNCONFIRMED_SOURCE_ROW
    if candidate.batch_source_type in _DOCUMENT_SOURCES and (
        candidate.batch_verification_status != "verified"
    ):
        return TrainingExclusionReason.UNTRUSTED_DOCUMENT
    if candidate.batch_source_type == "csv" and (
        candidate.batch_verification_status not in {"verified", "needs_review"}
    ):
        return TrainingExclusionReason.UNTRUSTED_DOCUMENT
    if _DUPLICATE_CODES.intersection(candidate.issue_codes):
        return TrainingExclusionReason.DUPLICATE_EVIDENCE
    if category_id is None:
        return TrainingExclusionReason.NO_AUTHORITATIVE_LABEL
    if category_id == "needs_review":
        return TrainingExclusionReason.NEEDS_REVIEW_LABEL
    if not category_exists:
        return TrainingExclusionReason.CATEGORY_NOT_FOUND
    if not category_active:
        return TrainingExclusionReason.CATEGORY_INACTIVE
    if category_taxonomy_version != taxonomy_version:
        return TrainingExclusionReason.TAXONOMY_MISMATCH
    if financial_role == FinancialRole.UNKNOWN.value:
        return TrainingExclusionReason.UNRESOLVED_FINANCIAL_ROLE
    if financial_role == FinancialRole.EXCLUDED.value:
        return TrainingExclusionReason.EXCLUDED_FINANCIAL_ROLE
    if candidate.transaction_id in needs_review_ids:
        return TrainingExclusionReason.NEEDS_REVIEW_FLAG
    if candidate.transaction_id in unresolved_transfer_ids:
        return TrainingExclusionReason.UNRESOLVED_TRANSFER
    try:
        build_feature_text(candidate.merchant, candidate.description)
    except MLCategorisationError:
        return TrainingExclusionReason.EMPTY_FEATURE_TEXT
    return None


def build_training_dataset(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
    taxonomy_version: str,
    cutoff: TrainingCutoff,
) -> MLTrainingDataset:
    """Build a deterministic supervised dataset from facts known at a cutoff."""
    with session_scope(factory) as session:
        if UserProfileRepository(session).get(user_profile_id) is None:
            raise MLCategorisationError(
                MLCategorisationErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        repository = MLCategorisationRepository(session)
        candidates = repository.list_training_candidates(user_profile_id)
        transaction_ids = tuple(candidate.transaction_id for candidate in candidates)
        corrections = repository.latest_category_corrections_as_of(
            transaction_ids,
            knowledge_cutoff_at=cutoff.knowledge_cutoff_at,
        )
        role_audits = repository.latest_financial_role_audits_as_of(
            transaction_ids,
            knowledge_cutoff_at=cutoff.knowledge_cutoff_at,
        )
        needs_review_ids = repository.list_needs_review_transaction_ids_as_of(
            transaction_ids,
            knowledge_cutoff_at=cutoff.knowledge_cutoff_at,
        )
        unresolved_transfer_ids = (
            repository.list_unresolved_transfer_transaction_ids_as_of(
                transaction_ids,
                knowledge_cutoff_at=cutoff.knowledge_cutoff_at,
            )
        )
        category_ids = tuple(
            sorted({correction.new_category_id for correction in corrections.values()})
        )
        categories = {
            category.id: category
            for category in repository.list_categories(category_ids)
        }

        examples: list[MLTrainingExample] = []
        exclusions: Counter[TrainingExclusionReason] = Counter()
        for candidate in candidates:
            correction = corrections.get(candidate.transaction_id)
            category_id = correction.new_category_id if correction is not None else None
            category = categories.get(category_id) if category_id is not None else None
            role_audit = role_audits.get(candidate.transaction_id)
            financial_role = (
                role_audit.new_role_id
                if role_audit is not None
                else FinancialRole.UNKNOWN.value
            )
            reason = _candidate_exclusion(
                candidate,
                cutoff=cutoff,
                taxonomy_version=taxonomy_version,
                category_id=category_id,
                category_exists=category is not None,
                category_active=category.is_active if category is not None else False,
                category_taxonomy_version=(
                    category.taxonomy_version if category is not None else None
                ),
                financial_role=financial_role,
                needs_review_ids=needs_review_ids,
                unresolved_transfer_ids=unresolved_transfer_ids,
            )
            if reason is not None:
                exclusions[reason] += 1
                continue
            eligible_correction = cast(CategoryCorrectionRecord, correction)
            eligible_category_id = cast(str, category_id)
            merchant_group = normalise_rule_text(candidate.merchant or "") or None
            examples.append(
                MLTrainingExample(
                    transaction_id=candidate.transaction_id,
                    transaction_date=candidate.transaction_date,
                    verified_at=candidate.verified_at,
                    merchant=candidate.merchant,
                    description=candidate.description,
                    merchant_group=merchant_group,
                    category_id=eligible_category_id,
                    category_decided_at=eligible_correction.corrected_at,
                )
            )
        ordered_exclusions = tuple(
            TrainingExclusionCount(reason=reason, count=exclusions[reason])
            for reason in TrainingExclusionReason
            if exclusions[reason]
        )
        return MLTrainingDataset(
            taxonomy_version=taxonomy_version,
            cutoff=cutoff,
            examples=tuple(examples),
            exclusions=ordered_exclusions,
        )


def _validate_partition_sizes(
    training_examples: tuple[MLTrainingExample, ...],
    test_examples: tuple[MLTrainingExample, ...],
    *,
    plan: MLTrainingPlan,
) -> None:
    if len(training_examples) < plan.minimum_training_samples:
        raise MLCategorisationError(
            MLCategorisationErrorCode.TOO_FEW_TRAINING_SAMPLES,
            "evaluation partition has too few training samples",
        )
    if len(test_examples) < plan.minimum_test_samples:
        raise MLCategorisationError(
            MLCategorisationErrorCode.TOO_FEW_TEST_SAMPLES,
            "evaluation partition has too few test samples",
        )
    if len({example.category_id for example in training_examples}) < 2:
        raise MLCategorisationError(
            MLCategorisationErrorCode.ONE_CLASS_TRAINING_SPLIT,
            "evaluation training partition requires at least two categories",
        )


def _validate_dataset_plan(
    dataset: MLTrainingDataset,
    *,
    plan: MLTrainingPlan,
    expected_cutoff: TrainingCutoff,
) -> None:
    if (
        dataset.taxonomy_version != plan.taxonomy_version
        or dataset.cutoff != expected_cutoff
    ):
        raise MLCategorisationError(
            MLCategorisationErrorCode.DATASET_PLAN_MISMATCH,
            "evaluation dataset does not match the requested training plan",
        )


def _ordered_examples(
    examples: tuple[MLTrainingExample, ...],
) -> tuple[MLTrainingExample, ...]:
    return tuple(
        sorted(
            examples,
            key=lambda item: (
                item.transaction_date,
                item.verified_at,
                item.transaction_id,
            ),
        )
    )


def create_chronological_split(
    historical_dataset: MLTrainingDataset,
    final_dataset: MLTrainingDataset,
    plan: MLTrainingPlan,
) -> MLHoldoutSplit:
    """Create an unshuffled test period with historically available labels."""
    _validate_dataset_plan(
        historical_dataset,
        plan=plan,
        expected_cutoff=plan.chronological_training_cutoff,
    )
    _validate_dataset_plan(
        final_dataset,
        plan=plan,
        expected_cutoff=plan.final_cutoff,
    )
    training_examples = _ordered_examples(
        tuple(
            example
            for example in historical_dataset.examples
            if example.transaction_date < plan.chronological_test_start
        )
    )
    test_examples = _ordered_examples(
        tuple(
            example
            for example in final_dataset.examples
            if example.transaction_date >= plan.chronological_test_start
        )
    )
    _validate_partition_sizes(training_examples, test_examples, plan=plan)
    return MLHoldoutSplit(
        kind=MLHoldoutKind.CHRONOLOGICAL,
        training_examples=training_examples,
        test_examples=test_examples,
    )


def create_unseen_merchant_split(
    dataset: MLTrainingDataset,
    plan: MLTrainingPlan,
) -> MLHoldoutSplit:
    """Create a deterministic partition with disjoint normalized merchants."""
    _validate_dataset_plan(
        dataset,
        plan=plan,
        expected_cutoff=plan.final_cutoff,
    )
    merchant_examples = _ordered_examples(
        tuple(
            example
            for example in dataset.examples
            if example.merchant_group is not None
        )
    )
    groups = tuple(cast(str, example.merchant_group) for example in merchant_examples)
    if len(set(groups)) < 2:
        raise MLCategorisationError(
            MLCategorisationErrorCode.TOO_FEW_MERCHANT_GROUPS,
            "unseen-merchant evaluation requires at least two merchant groups",
        )
    splitter = GroupShuffleSplit(
        n_splits=1,
        test_size=plan.unseen_merchant_test_fraction,
        random_state=plan.random_seed,
    )
    training_indices, test_indices = next(
        splitter.split(merchant_examples, groups=groups)
    )
    training_examples = tuple(merchant_examples[index] for index in training_indices)
    test_examples = tuple(merchant_examples[index] for index in test_indices)
    _validate_partition_sizes(training_examples, test_examples, plan=plan)
    return MLHoldoutSplit(
        kind=MLHoldoutKind.UNSEEN_MERCHANT,
        training_examples=training_examples,
        test_examples=test_examples,
        omitted_without_merchant=len(dataset.examples) - len(merchant_examples),
    )


def build_categorisation_pipeline(random_seed: int) -> Pipeline:
    """Create a fresh word-and-character TF-IDF Logistic Regression pipeline."""
    features = FeatureUnion(
        (
            (
                "word",
                TfidfVectorizer(
                    analyzer="word",
                    ngram_range=(1, 2),
                    lowercase=False,
                ),
            ),
            (
                "character",
                TfidfVectorizer(
                    analyzer="char_wb",
                    ngram_range=(3, 5),
                    lowercase=False,
                ),
            ),
        )
    )
    return Pipeline(
        (
            ("features", features),
            (
                "classifier",
                LogisticRegression(
                    max_iter=1_000,
                    class_weight="balanced",
                    random_state=random_seed,
                ),
            ),
        )
    )


def _feature_texts(
    examples: tuple[MLTrainingExample, ...],
) -> list[str]:
    return [build_feature_text(item.merchant, item.description) for item in examples]


def _classification_metrics(
    expected: list[str],
    predicted: list[str],
    labels: tuple[str, ...],
) -> ClassificationMetrics:
    precision, recall, f1, support = precision_recall_fscore_support(
        expected,
        predicted,
        labels=list(labels),
        zero_division=0,
    )
    matrix = confusion_matrix(expected, predicted, labels=list(labels))
    return ClassificationMetrics(
        macro_f1=float(
            f1_score(
                expected,
                predicted,
                labels=list(labels),
                average="macro",
                zero_division=0,
            )
        ),
        weighted_f1=float(
            f1_score(
                expected,
                predicted,
                labels=list(labels),
                average="weighted",
                zero_division=0,
            )
        ),
        macro_precision=float(
            precision_score(
                expected,
                predicted,
                labels=list(labels),
                average="macro",
                zero_division=0,
            )
        ),
        weighted_precision=float(
            precision_score(
                expected,
                predicted,
                labels=list(labels),
                average="weighted",
                zero_division=0,
            )
        ),
        macro_recall=float(
            recall_score(
                expected,
                predicted,
                labels=list(labels),
                average="macro",
                zero_division=0,
            )
        ),
        weighted_recall=float(
            recall_score(
                expected,
                predicted,
                labels=list(labels),
                average="weighted",
                zero_division=0,
            )
        ),
        per_category=tuple(
            CategoryMetric(
                category_id=category_id,
                precision=float(precision[index]),
                recall=float(recall[index]),
                f1=float(f1[index]),
                support=int(support[index]),
            )
            for index, category_id in enumerate(labels)
        ),
        confusion_matrix=ConfusionMatrix(
            labels=labels,
            rows=tuple(tuple(int(value) for value in row) for row in matrix),
        ),
    )


def _evaluate_holdout(
    split: MLHoldoutSplit,
    *,
    random_seed: int,
) -> HoldoutEvaluation:
    training_text = _feature_texts(split.training_examples)
    test_text = _feature_texts(split.test_examples)
    training_labels = [item.category_id for item in split.training_examples]
    expected = [item.category_id for item in split.test_examples]
    labels = tuple(sorted(set(training_labels) | set(expected)))

    candidate = build_categorisation_pipeline(random_seed)
    candidate.fit(training_text, training_labels)
    candidate_predictions = cast(list[str], candidate.predict(test_text).tolist())

    baseline = DummyClassifier(strategy="most_frequent")
    baseline.fit(training_text, training_labels)
    baseline_predictions = cast(list[str], baseline.predict(test_text).tolist())

    training_dates = [item.transaction_date for item in split.training_examples]
    test_dates = [item.transaction_date for item in split.test_examples]
    return HoldoutEvaluation(
        kind=split.kind,
        training_count=len(split.training_examples),
        test_count=len(split.test_examples),
        training_start_date=min(training_dates),
        training_end_date=max(training_dates),
        test_start_date=min(test_dates),
        test_end_date=max(test_dates),
        training_merchant_groups=len(
            {
                item.merchant_group
                for item in split.training_examples
                if item.merchant_group is not None
            }
        ),
        test_merchant_groups=len(
            {
                item.merchant_group
                for item in split.test_examples
                if item.merchant_group is not None
            }
        ),
        omitted_without_merchant=split.omitted_without_merchant,
        candidate=_classification_metrics(expected, candidate_predictions, labels),
        baseline=_classification_metrics(expected, baseline_predictions, labels),
    )


def evaluate_categorisation_model(
    historical_dataset: MLTrainingDataset,
    final_dataset: MLTrainingDataset,
    plan: MLTrainingPlan,
) -> MLCategorisationEvaluation:
    """Evaluate fresh candidate and baseline models on both required holdouts."""
    chronological = _evaluate_holdout(
        create_chronological_split(historical_dataset, final_dataset, plan),
        random_seed=plan.random_seed,
    )
    unseen_merchant = _evaluate_holdout(
        create_unseen_merchant_split(final_dataset, plan),
        random_seed=plan.random_seed,
    )
    selected = (
        chronological.candidate.macro_f1 > chronological.baseline.macro_f1
        and unseen_merchant.candidate.macro_f1 > unseen_merchant.baseline.macro_f1
        and chronological.candidate.weighted_f1 >= chronological.baseline.weighted_f1
        and unseen_merchant.candidate.weighted_f1
        >= unseen_merchant.baseline.weighted_f1
    )
    reason = (
        "candidate beats the most-frequent baseline on both required holdouts"
        if selected
        else "candidate does not consistently beat the required baseline"
    )
    return MLCategorisationEvaluation(
        chronological=chronological,
        unseen_merchant=unseen_merchant,
        candidate_selected=selected,
        selection_reason=reason,
    )


def _artifact_paths(plan: MLTrainingPlan) -> tuple[Path, Path]:
    return (
        plan.artifact_directory / f"{plan.model_version}.joblib",
        plan.artifact_directory / f"{plan.model_version}.metadata.json",
    )


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file_handle:
        for block in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _persist_candidate(
    *,
    plan: MLTrainingPlan,
    pipeline: Pipeline,
    metadata: MLTrainingMetadata,
) -> MLCategoriserTrainingResult:
    artifact_path, metadata_path = _artifact_paths(plan)
    if artifact_path.exists() or metadata_path.exists():
        raise MLCategorisationError(
            MLCategorisationErrorCode.ARTIFACT_EXISTS,
            "model candidate version already exists",
        )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    artifact_temporary = artifact_path.with_name(f".{artifact_path.name}.{token}.tmp")
    metadata_temporary = metadata_path.with_name(f".{metadata_path.name}.{token}.tmp")
    artifact_installed = False
    try:
        joblib.dump(
            {
                "manifest": metadata.manifest.model_dump(mode="json"),
                "pipeline": pipeline,
            },
            artifact_temporary,
        )
        artifact_temporary.chmod(0o600)
        digest = _file_digest(artifact_temporary)
        final_metadata = metadata.model_copy(update={"artifact_sha256": digest})
        metadata_temporary.write_text(
            json.dumps(
                final_metadata.model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        metadata_temporary.chmod(0o600)
        try:
            os.link(artifact_temporary, artifact_path)
        except FileExistsError as exc:
            raise MLCategorisationError(
                MLCategorisationErrorCode.ARTIFACT_EXISTS,
                "model candidate version already exists",
            ) from exc
        artifact_temporary.unlink()
        artifact_installed = True
        try:
            os.link(metadata_temporary, metadata_path)
        except FileExistsError as exc:
            raise MLCategorisationError(
                MLCategorisationErrorCode.ARTIFACT_EXISTS,
                "model candidate version already exists",
            ) from exc
        metadata_temporary.unlink()
        return MLCategoriserTrainingResult(
            artifact_path=artifact_path,
            metadata_path=metadata_path,
            metadata=final_metadata,
        )
    except Exception:
        artifact_temporary.unlink(missing_ok=True)
        metadata_temporary.unlink(missing_ok=True)
        if artifact_installed:
            artifact_path.unlink(missing_ok=True)
        raise


def train_transaction_categoriser(
    factory: sessionmaker[Session],
    *,
    plan: MLTrainingPlan,
) -> MLCategoriserTrainingResult:
    """Evaluate, fit, and privately persist one reproducible model candidate."""
    historical_dataset = build_training_dataset(
        factory,
        user_profile_id=plan.user_profile_id,
        taxonomy_version=plan.taxonomy_version,
        cutoff=plan.chronological_training_cutoff,
    )
    final_dataset = build_training_dataset(
        factory,
        user_profile_id=plan.user_profile_id,
        taxonomy_version=plan.taxonomy_version,
        cutoff=plan.final_cutoff,
    )
    if not final_dataset.examples:
        raise MLCategorisationError(
            MLCategorisationErrorCode.NO_ELIGIBLE_SAMPLES,
            "no eligible verified training samples are available",
        )
    if len(final_dataset.examples) < plan.minimum_training_samples:
        raise MLCategorisationError(
            MLCategorisationErrorCode.TOO_FEW_TRAINING_SAMPLES,
            "final model has too few training samples",
        )
    classes = tuple(sorted({item.category_id for item in final_dataset.examples}))
    if len(classes) < 2:
        raise MLCategorisationError(
            MLCategorisationErrorCode.TOO_FEW_CLASSES,
            "final model requires at least two categories",
        )
    evaluation = evaluate_categorisation_model(
        historical_dataset,
        final_dataset,
        plan,
    )
    pipeline = build_categorisation_pipeline(plan.random_seed)
    pipeline.fit(
        _feature_texts(final_dataset.examples),
        [item.category_id for item in final_dataset.examples],
    )
    support = Counter(item.category_id for item in final_dataset.examples)
    dates = [item.transaction_date for item in final_dataset.examples]
    manifest = MLModelManifest(
        model_version=plan.model_version,
        taxonomy_version=plan.taxonomy_version,
        classes=classes,
        created_at=plan.trained_at,
    )
    metadata = MLTrainingMetadata(
        manifest=manifest,
        final_cutoff=plan.final_cutoff,
        chronological_training_cutoff=plan.chronological_training_cutoff,
        chronological_test_start=plan.chronological_test_start,
        unseen_merchant_test_fraction=plan.unseen_merchant_test_fraction,
        minimum_training_samples=plan.minimum_training_samples,
        minimum_test_samples=plan.minimum_test_samples,
        parameters=MLPipelineParameters(random_seed=plan.random_seed),
        training_count=len(final_dataset.examples),
        training_start_date=min(dates),
        training_end_date=max(dates),
        category_support=tuple(
            CategorySupport(category_id=category_id, count=support[category_id])
            for category_id in classes
        ),
        historical_exclusions=historical_dataset.exclusions,
        final_exclusions=final_dataset.exclusions,
        evaluation=evaluation,
        python_version=platform.python_version(),
        scikit_learn_version=sklearn.__version__,
        artifact_sha256="0" * 64,
    )
    return _persist_candidate(plan=plan, pipeline=pipeline, metadata=metadata)


def _pipeline_matches_metadata(
    pipeline: Pipeline,
    metadata: MLTrainingMetadata,
) -> bool:
    try:
        step_names = tuple(name for name, _ in pipeline.steps)
    except (TypeError, ValueError):
        return False
    if step_names != ("features", "classifier"):
        return False
    features = pipeline.named_steps.get("features")
    classifier = pipeline.named_steps.get("classifier")
    if not isinstance(features, FeatureUnion) or not isinstance(
        classifier, LogisticRegression
    ):
        return False
    try:
        transformer_names = tuple(name for name, _ in features.transformer_list)
    except (TypeError, ValueError):
        return False
    if transformer_names != (
        "word",
        "character",
    ):
        return False
    word = features.transformer_list[0][1]
    character = features.transformer_list[1][1]
    parameters = metadata.parameters
    if not isinstance(word, TfidfVectorizer) or not isinstance(
        character, TfidfVectorizer
    ):
        return False
    if (
        word.analyzer != "word"
        or word.ngram_range != parameters.word_ngram_range
        or word.lowercase
        or character.analyzer != parameters.character_analyser
        or character.ngram_range != parameters.character_ngram_range
        or character.lowercase
        or classifier.max_iter != parameters.logistic_max_iterations
        or classifier.class_weight != parameters.logistic_class_weight
        or classifier.random_state != parameters.random_seed
    ):
        return False
    if not hasattr(word, "vocabulary_") or not hasattr(character, "vocabulary_"):
        return False
    classes = tuple(str(value) for value in getattr(classifier, "classes_", ()))
    return classes == metadata.manifest.classes


def load_transaction_categoriser(
    artifact_path: Path,
    metadata_path: Path,
    *,
    expected_taxonomy_version: str,
    trusted_local_artifact: bool,
) -> LoadedTransactionCategoriser:
    """Load only an explicitly trusted local candidate after digest validation."""
    if not trusted_local_artifact:
        raise MLCategorisationError(
            MLCategorisationErrorCode.UNTRUSTED_ARTIFACT,
            "joblib models may only be loaded from a trusted local source",
        )
    if not artifact_path.is_file() or not metadata_path.is_file():
        raise MLCategorisationError(
            MLCategorisationErrorCode.ARTIFACT_NOT_FOUND,
            "model candidate files are unavailable",
        )
    try:
        metadata = MLTrainingMetadata.model_validate_json(
            metadata_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError) as exc:
        raise MLCategorisationError(
            MLCategorisationErrorCode.INVALID_MODEL_METADATA,
            "model metadata is invalid",
        ) from exc
    if metadata.manifest.taxonomy_version != expected_taxonomy_version:
        raise MLCategorisationError(
            MLCategorisationErrorCode.TAXONOMY_MISMATCH,
            "model taxonomy does not match the requested taxonomy",
        )
    if metadata.manifest.feature_schema_version != _FEATURE_SCHEMA_VERSION:
        raise MLCategorisationError(
            MLCategorisationErrorCode.FEATURE_SCHEMA_MISMATCH,
            "model feature schema is unsupported",
        )
    if metadata.scikit_learn_version != sklearn.__version__:
        raise MLCategorisationError(
            MLCategorisationErrorCode.SOFTWARE_VERSION_MISMATCH,
            "model scikit-learn version does not match this environment",
        )
    if _file_digest(artifact_path) != metadata.artifact_sha256:
        raise MLCategorisationError(
            MLCategorisationErrorCode.ARTIFACT_CHECKSUM_MISMATCH,
            "model artifact checksum does not match its metadata",
        )
    try:
        payload = joblib.load(artifact_path)
    except Exception as exc:
        raise MLCategorisationError(
            MLCategorisationErrorCode.ARTIFACT_MANIFEST_MISMATCH,
            "model artifact could not be validated",
        ) from exc
    if not isinstance(payload, dict):
        raise MLCategorisationError(
            MLCategorisationErrorCode.ARTIFACT_MANIFEST_MISMATCH,
            "model artifact manifest does not match its metadata",
        )
    pipeline = payload.get("pipeline")
    if (
        set(payload) != {"manifest", "pipeline"}
        or payload.get("manifest") != metadata.manifest.model_dump(mode="json")
        or not isinstance(pipeline, Pipeline)
        or not _pipeline_matches_metadata(pipeline, metadata)
    ):
        raise MLCategorisationError(
            MLCategorisationErrorCode.ARTIFACT_MANIFEST_MISMATCH,
            "model artifact manifest does not match its metadata",
        )
    return LoadedTransactionCategoriser(pipeline=pipeline, metadata=metadata)


def predict_transaction_categories(
    model: LoadedTransactionCategoriser,
    inputs: tuple[MLCategorisationInput, ...],
) -> tuple[MLCategorisationPrediction, ...]:
    """Return standalone probabilities without writing category assignments."""
    if not inputs:
        return ()
    texts = [build_feature_text(item.merchant, item.description) for item in inputs]
    classes = tuple(str(value) for value in getattr(model.pipeline, "classes_", ()))
    if classes != model.metadata.manifest.classes:
        raise MLCategorisationError(
            MLCategorisationErrorCode.ARTIFACT_MANIFEST_MISMATCH,
            "model classes do not match the trusted manifest",
        )
    probability_rows = cast(Any, model.pipeline.predict_proba(texts))
    predictions: list[MLCategorisationPrediction] = []
    for item, row in zip(inputs, probability_rows, strict=True):
        values = tuple(float(value) for value in row)
        predicted_index = max(range(len(classes)), key=values.__getitem__)
        predictions.append(
            MLCategorisationPrediction(
                transaction_id=item.transaction_id,
                predicted_category_id=classes[predicted_index],
                probabilities=tuple(
                    MLCategoryProbability(
                        category_id=category_id,
                        probability=values[index],
                    )
                    for index, category_id in enumerate(classes)
                ),
            )
        )
    return tuple(predictions)
