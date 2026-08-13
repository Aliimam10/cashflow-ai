"""Tests for leakage-safe transaction-category model development."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import joblib  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.categorisation import ml as ml_module
from cashflow_ai.categorisation.ml import (
    MLCategorisationError,
    MLCategorisationErrorCode,
    build_categorisation_pipeline,
    build_feature_text,
    build_training_dataset,
    create_chronological_split,
    create_unseen_merchant_split,
    evaluate_categorisation_model,
    load_transaction_categoriser,
    predict_transaction_categories,
    train_transaction_categoriser,
)
from cashflow_ai.persistence import (
    Base,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import (
    AccountRecord,
    CategoryCorrectionRecord,
    CategoryRecord,
    FinancialRoleAuditRecord,
    FinancialRoleRecord,
    FinancialRoleSuggestionRecord,
    ImportBatchRecord,
    RawTransactionRecord,
    UserFlagRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.persistence.repositories import (
    MLCategorisationRepository,
    MLTrainingCandidateRow,
)
from cashflow_ai.schemas import FinancialRole, load_taxonomy
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

TAXONOMY_PATH = Path("configs/categories.yaml")
PROFILE_ID = "synthetic-profile"
OTHER_PROFILE_ID = "other-synthetic-profile"
ACCOUNT_ID = "synthetic-current"
OTHER_ACCOUNT_ID = "other-current"
KNOWLEDGE_CUTOFF = datetime(2026, 6, 30, 23, 59, tzinfo=UTC)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


@pytest.fixture
def engine() -> Engine:
    database_engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    return database_engine


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _seed_foundation(factory: sessionmaker[Session]) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    with session_scope(factory) as session:
        session.add_all(
            (
                UserProfileRecord(
                    id=PROFILE_ID,
                    display_name="Synthetic Model User",
                    base_currency="GBP",
                    timezone="Europe/London",
                ),
                UserProfileRecord(
                    id=OTHER_PROFILE_ID,
                    display_name="Other Synthetic User",
                    base_currency="GBP",
                    timezone="Europe/London",
                ),
            )
        )
        session.flush()
        session.add_all(
            (
                AccountRecord(
                    id=ACCOUNT_ID,
                    user_profile_id=PROFILE_ID,
                    name="Synthetic Current",
                    account_type="current",
                    currency="GBP",
                ),
                AccountRecord(
                    id=OTHER_ACCOUNT_ID,
                    user_profile_id=OTHER_PROFILE_ID,
                    name="Other Current",
                    account_type="current",
                    currency="GBP",
                ),
            )
        )
        session.add_all(
            FinancialRoleRecord(
                id=role.value,
                name=role.value.replace("_", " ").title(),
            )
            for role in FinancialRole
        )
        session.add_all(
            CategoryRecord(
                id=category.id,
                name=category.name,
                parent_id=category.parent_id,
                taxonomy_version=taxonomy.version,
                is_active=category.is_active,
            )
            for category in taxonomy.categories
        )


def _add_transaction(
    session: Session,
    transaction_id: str,
    *,
    transaction_date: date,
    verified_at: datetime,
    merchant: str | None = "Fictional Market",
    description: str = "Fictional market purchase",
    account_id: str = ACCOUNT_ID,
    source_type: str = "csv",
    batch_source_type: str | None = None,
    batch_status: str = "verified",
    raw_status: str = "confirmed",
    issues: list[dict[str, object]] | None = None,
    current_category_id: str | None = None,
    current_role: FinancialRole = FinancialRole.EXPENSE,
    corrections: tuple[tuple[str, str, datetime], ...] = (),
    role_audits: tuple[tuple[str, FinancialRole, FinancialRole, datetime], ...]
    | None = None,
    needs_review_at: datetime | None = None,
) -> VerifiedTransactionRecord:
    batch_id = f"batch-{transaction_id}"
    raw_id = f"raw-{transaction_id}"
    extension = "csv" if source_type == "csv" else "pdf"
    session.add(
        ImportBatchRecord(
            id=batch_id,
            account_id=account_id,
            source_type=batch_source_type or source_type,
            source_filename=f"{transaction_id}.{extension}",
            file_hash=_hash(f"file-{transaction_id}"),
            mime_type="text/csv" if source_type == "csv" else "application/pdf",
            byte_size=128,
            verification_status=batch_status,
            imported_at=verified_at,
        )
    )
    session.add(
        RawTransactionRecord(
            id=raw_id,
            import_batch_id=batch_id,
            source_type=source_type,
            source_row_number=2 if source_type == "csv" else None,
            page_number=None if source_type == "csv" else 1,
            page_record_number=None if source_type == "csv" else 1,
            raw_payload={
                "private_poison": "RAW_PRIVATE_TOKEN_MUST_NEVER_BECOME_A_FEATURE"
            },
            original_date_text=transaction_date.isoformat(),
            original_description="ORIGINAL_PRIVATE_TOKEN_MUST_NOT_BE_A_FEATURE",
            original_amount_text="-10.00",
            parser_name="synthetic_parser",
            parser_version="1.0",
            source_fingerprint=_hash(f"source-{transaction_id}"),
            canonical_fingerprint=_hash(f"canonical-{transaction_id}"),
            issues_json=issues or [],
            review_status=raw_status,
            created_at=verified_at,
        )
    )
    transaction = VerifiedTransactionRecord(
        id=transaction_id,
        raw_transaction_id=raw_id,
        account_id=account_id,
        transaction_date=transaction_date,
        posting_date=transaction_date,
        description=description,
        merchant=merchant,
        amount=Decimal("-10.00"),
        balance_after=None,
        currency="GBP",
        external_id=f"external-{transaction_id}",
        transaction_type="synthetic",
        direction="outflow",
        category_id=current_category_id,
        financial_role_id=current_role.value,
        verified_at=verified_at,
    )
    session.add(transaction)
    session.flush()
    session.add_all(
        CategoryCorrectionRecord(
            id=correction_id,
            verified_transaction_id=transaction_id,
            previous_category_id=None,
            new_category_id=category_id,
            corrected_at=corrected_at,
        )
        for correction_id, category_id, corrected_at in corrections
    )
    session.add_all(
        FinancialRoleAuditRecord(
            id=audit_id,
            verified_transaction_id=transaction_id,
            previous_role_id=previous.value,
            new_role_id=new.value,
            suggestion_id=None,
            source="user_override",
            changed_at=changed_at,
        )
        for audit_id, previous, new, changed_at in (
            role_audits
            if role_audits is not None
            else (
                (
                    f"role-audit-{transaction_id}",
                    FinancialRole.UNKNOWN,
                    current_role,
                    verified_at,
                ),
            )
            if current_role is not FinancialRole.UNKNOWN
            else ()
        )
    )
    if needs_review_at is not None:
        session.add(
            UserFlagRecord(
                id=f"flag-{transaction_id}",
                verified_transaction_id=transaction_id,
                flag="needs_review",
                note="SYNTHETIC_PRIVATE_NOTE",
                created_at=needs_review_at,
            )
        )
    session.flush()
    return transaction


def _add_transfer_suggestion(
    session: Session,
    transaction_id: str,
    *,
    counterpart_transaction_id: str | None = None,
    created_at: datetime,
    status: str = "pending",
    reviewed_at: datetime | None = None,
) -> None:
    session.add(
        FinancialRoleSuggestionRecord(
            id=f"suggestion-{transaction_id}",
            suggestion_key=_hash(f"suggestion-{transaction_id}"),
            verified_transaction_id=transaction_id,
            counterpart_transaction_id=counterpart_transaction_id,
            kind="transfer",
            suggested_role_id="transfer_out",
            counterpart_role_id=(
                "transfer_in" if counterpart_transaction_id is not None else None
            ),
            confidence=Decimal("0.5500"),
            reason_codes_json=["transfer_language"],
            algorithm_version="synthetic-role-rules",
            status=status,
            created_at=created_at,
            reviewed_at=reviewed_at,
        )
    )


def _cutoff(
    *,
    transaction_date: date = date(2026, 6, 30),
    knowledge_cutoff_at: datetime = KNOWLEDGE_CUTOFF,
) -> TrainingCutoff:
    return TrainingCutoff(
        transaction_date=transaction_date,
        knowledge_cutoff_at=knowledge_cutoff_at,
    )


def _plan(
    artifact_directory: Path = Path("models/categorisation"),
    **changes: Any,
) -> MLTrainingPlan:
    values: dict[str, Any] = {
        "user_profile_id": PROFILE_ID,
        "model_version": "synthetic-1",
        "taxonomy_version": "1.0",
        "artifact_directory": artifact_directory,
        "final_cutoff": _cutoff(),
        "chronological_training_cutoff": _cutoff(
            transaction_date=date(2026, 3, 31),
            knowledge_cutoff_at=datetime(2026, 3, 31, 23, 59, tzinfo=UTC),
        ),
        "chronological_test_start": date(2026, 4, 1),
        "unseen_merchant_test_fraction": 0.34,
        "minimum_training_samples": 2,
        "minimum_test_samples": 1,
        "random_seed": 7,
        "trained_at": datetime(2026, 7, 1, tzinfo=UTC),
    }
    return MLTrainingPlan.model_validate({**values, **changes})


def _example(
    transaction_id: str,
    *,
    transaction_date: date = date(2026, 1, 1),
    merchant: str | None = "Fictional Market",
    merchant_group: str | None = "fictional market",
    category_id: str = "groceries",
) -> MLTrainingExample:
    return MLTrainingExample(
        transaction_id=transaction_id,
        transaction_date=transaction_date,
        verified_at=datetime(2026, 1, 2, tzinfo=UTC),
        merchant=merchant,
        description="Fictional purchase",
        merchant_group=merchant_group,
        category_id=category_id,
        category_decided_at=datetime(2026, 1, 3, tzinfo=UTC),
    )


def _metrics(
    labels: tuple[str, ...] = ("groceries", "housing"),
) -> ClassificationMetrics:
    per_category = tuple(
        CategoryMetric(
            category_id=label,
            precision=0.5,
            recall=0.5,
            f1=0.5,
            support=1,
        )
        for label in labels
    )
    size = len(labels)
    return ClassificationMetrics(
        macro_f1=0.5,
        weighted_f1=0.5,
        macro_precision=0.5,
        weighted_precision=0.5,
        macro_recall=0.5,
        weighted_recall=0.5,
        per_category=per_category,
        confusion_matrix=ConfusionMatrix(
            labels=labels,
            rows=tuple(
                tuple(1 if row == column else 0 for column in range(size))
                for row in range(size)
            ),
        ),
    )


def _holdout_evaluation(
    kind: MLHoldoutKind,
) -> HoldoutEvaluation:
    chronological = kind is MLHoldoutKind.CHRONOLOGICAL
    return HoldoutEvaluation(
        kind=kind,
        training_count=2,
        test_count=2,
        training_start_date=date(2026, 1, 1),
        training_end_date=date(2026, 1, 10),
        test_start_date=(date(2026, 4, 1) if chronological else date(2026, 1, 1)),
        test_end_date=(date(2026, 4, 10) if chronological else date(2026, 2, 10)),
        training_merchant_groups=2,
        test_merchant_groups=2,
        candidate=_metrics(),
        baseline=_metrics(),
    )


def _dataset(
    examples: tuple[MLTrainingExample, ...],
    *,
    cutoff: TrainingCutoff | None = None,
) -> MLTrainingDataset:
    return MLTrainingDataset(
        taxonomy_version="1.0",
        cutoff=cutoff or _cutoff(),
        examples=examples,
    )


def _evaluation_datasets() -> tuple[MLTrainingDataset, MLTrainingDataset]:
    historical_examples: list[MLTrainingExample] = []
    final_examples: list[MLTrainingExample] = []
    group_categories = (
        ("market north", "groceries", "weekly groceries"),
        ("market south", "groceries", "fresh groceries"),
        ("homes north", "housing", "monthly housing"),
        ("homes south", "housing", "rental housing"),
    )
    for index, (merchant_group, category_id, description) in enumerate(
        group_categories
    ):
        early = _example(
            f"early-{index}",
            transaction_date=date(2026, 1, index + 1),
            merchant=merchant_group,
            merchant_group=merchant_group,
            category_id=category_id,
        ).model_copy(update={"description": description})
        late = _example(
            f"late-{index}",
            transaction_date=date(2026, 4, index + 1),
            merchant=merchant_group,
            merchant_group=merchant_group,
            category_id=("travel" if index == 0 else category_id),
        ).model_copy(
            update={
                "description": (
                    "single future travel class" if index == 0 else description
                )
            }
        )
        historical_examples.append(early)
        final_examples.extend((early, late))
    historical = _dataset(
        tuple(historical_examples),
        cutoff=_cutoff(
            transaction_date=date(2026, 3, 31),
            knowledge_cutoff_at=datetime(2026, 3, 31, 23, 59, tzinfo=UTC),
        ),
    )
    return historical, _dataset(tuple(final_examples))


def _seed_trainable_history(factory: sessionmaker[Session]) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        categories = ("groceries", "groceries", "housing", "housing")
        merchants = ("Market North", "Market South", "Homes North", "Homes South")
        descriptions = (
            "weekly grocery basket",
            "fresh grocery basket",
            "monthly housing rent",
            "rental housing payment",
        )
        for index, (category_id, merchant, description) in enumerate(
            zip(categories, merchants, descriptions, strict=True)
        ):
            early_verified = datetime(2026, 1, index + 2, 12, tzinfo=UTC)
            _add_transaction(
                session,
                f"trainable-early-{index}",
                transaction_date=date(2026, 1, index + 1),
                verified_at=early_verified,
                merchant=merchant,
                description=description,
                corrections=(
                    (f"correction-early-{index}", category_id, early_verified),
                ),
            )
            late_verified = datetime(2026, 4, index + 2, 12, tzinfo=UTC)
            _add_transaction(
                session,
                f"trainable-late-{index}",
                transaction_date=date(2026, 4, index + 1),
                verified_at=late_verified,
                merchant=merchant,
                description=description,
                corrections=((f"correction-late-{index}", category_id, late_verified),),
            )


@pytest.fixture
def trained_candidate(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> MLCategoriserTrainingResult:
    _seed_trainable_history(factory)
    return train_transaction_categoriser(
        factory,
        plan=_plan(tmp_path / "trained-candidate"),
    )


def _write_metadata(path: Path, metadata: MLTrainingMetadata) -> None:
    path.write_text(
        json.dumps(metadata.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_payload_pair(
    directory: Path,
    payload: object,
    metadata: MLTrainingMetadata,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True)
    artifact_path = directory / "candidate.joblib"
    metadata_path = directory / "candidate.metadata.json"
    joblib.dump(payload, artifact_path)
    final_metadata = metadata.model_copy(
        update={"artifact_sha256": sha256(artifact_path.read_bytes()).hexdigest()}
    )
    _write_metadata(metadata_path, final_metadata)
    return artifact_path, metadata_path


def test_feature_text_is_normalised_private_and_has_stable_boundaries() -> None:
    assert build_feature_text("  Café—North ", "WEEKLY\tBasket!!!") == (
        "merchant café north description weekly basket"
    )
    assert build_feature_text(None, "Weekly basket") == (
        "merchant missing description weekly basket"
    )
    assert build_feature_text("Fictional Market", "---") == (
        "merchant fictional market description missing"
    )
    with pytest.raises(MLCategorisationError) as exc_info:
        build_feature_text(None, "---")
    assert exc_info.value.code is MLCategorisationErrorCode.EMPTY_FEATURE_TEXT
    assert "---" not in str(exc_info.value)


def test_defensive_missing_category_target_is_excluded() -> None:
    candidate = MLTrainingCandidateRow(
        transaction_id="synthetic-defensive-row",
        transaction_date=date(2026, 1, 1),
        verified_at=datetime(2026, 1, 2, tzinfo=UTC),
        merchant="Synthetic Merchant",
        description="synthetic purchase",
        account_lineage_matches=True,
        verified_source_type="csv",
        raw_review_status="confirmed",
        issue_codes=(),
        batch_source_type="csv",
        batch_verification_status="verified",
    )
    reason = ml_module._candidate_exclusion(
        candidate,
        cutoff=_cutoff(),
        taxonomy_version="1.0",
        category_id="missing_category",
        category_exists=False,
        category_active=False,
        category_taxonomy_version=None,
        financial_role="expense",
        needs_review_ids=frozenset(),
        unresolved_transfer_ids=frozenset(),
    )
    assert reason is TrainingExclusionReason.CATEGORY_NOT_FOUND


def test_chronological_split_uses_historical_labels_and_never_shuffles() -> None:
    historical = _dataset(
        (
            _example(
                "train-b",
                transaction_date=date(2026, 2, 1),
                category_id="housing",
            ),
            _example("train-a", transaction_date=date(2026, 1, 1)),
        ),
        cutoff=_cutoff(
            transaction_date=date(2026, 3, 31),
            knowledge_cutoff_at=datetime(2026, 3, 31, 23, 59, tzinfo=UTC),
        ),
    )
    final = _dataset(
        (
            _example("train-a", transaction_date=date(2026, 1, 1)),
            _example(
                "train-b",
                transaction_date=date(2026, 2, 1),
                category_id="housing",
            ),
            _example("test-a", transaction_date=date(2026, 4, 1)),
            _example(
                "test-b",
                transaction_date=date(2026, 5, 1),
                category_id="housing",
            ),
        )
    )
    split = create_chronological_split(historical, final, _plan())
    assert tuple(item.transaction_id for item in split.training_examples) == (
        "train-a",
        "train-b",
    )
    assert tuple(item.transaction_id for item in split.test_examples) == (
        "test-a",
        "test-b",
    )
    assert max(item.transaction_date for item in split.training_examples) < min(
        item.transaction_date for item in split.test_examples
    )

    with pytest.raises(MLCategorisationError) as exc_info:
        create_chronological_split(
            historical.model_copy(update={"taxonomy_version": "2.0"}),
            final,
            _plan(),
        )
    assert exc_info.value.code is MLCategorisationErrorCode.DATASET_PLAN_MISMATCH


def test_partition_size_errors_are_controlled_and_privacy_safe() -> None:
    plan = _plan(minimum_training_samples=3, minimum_test_samples=2)
    one = _dataset(
        (_example("private-transaction-one"),),
        cutoff=plan.chronological_training_cutoff,
    )
    one_final = _dataset((_example("private-transaction-one"),))
    with pytest.raises(MLCategorisationError) as exc_info:
        create_chronological_split(one, one_final, plan)
    assert exc_info.value.code is MLCategorisationErrorCode.TOO_FEW_TRAINING_SAMPLES
    assert "private-transaction-one" not in str(exc_info.value)

    training = _dataset(
        (
            _example("train-a", transaction_date=date(2026, 1, 1)),
            _example(
                "train-b",
                transaction_date=date(2026, 2, 1),
                category_id="housing",
            ),
            _example("train-c", transaction_date=date(2026, 3, 1)),
        ),
        cutoff=plan.chronological_training_cutoff,
    )
    one_test = _dataset(
        (
            *training.examples,
            _example("test-one", transaction_date=date(2026, 4, 1)),
        )
    )
    with pytest.raises(MLCategorisationError) as exc_info:
        create_chronological_split(training, one_test, plan)
    assert exc_info.value.code is MLCategorisationErrorCode.TOO_FEW_TEST_SAMPLES

    one_class_plan = _plan(minimum_training_samples=2, minimum_test_samples=1)
    one_class = _dataset(
        (
            _example("same-a", transaction_date=date(2026, 1, 1)),
            _example("same-b", transaction_date=date(2026, 2, 1)),
        ),
        cutoff=one_class_plan.chronological_training_cutoff,
    )
    final = _dataset(
        (
            *one_class.examples,
            _example("test", transaction_date=date(2026, 4, 1)),
        )
    )
    with pytest.raises(MLCategorisationError) as exc_info:
        create_chronological_split(
            one_class,
            final,
            one_class_plan,
        )
    assert exc_info.value.code is MLCategorisationErrorCode.ONE_CLASS_TRAINING_SPLIT


def test_unseen_merchant_split_is_grouped_deterministic_and_reports_omissions() -> None:
    examples = (
        _example("market-a", merchant="Café North", merchant_group="café north"),
        _example(
            "market-b",
            merchant="CAFÉ-NORTH",
            merchant_group="café north",
            category_id="housing",
        ),
        _example(
            "home-a",
            merchant="Fictional Homes",
            merchant_group="fictional homes",
            category_id="housing",
        ),
        _example(
            "home-b",
            merchant="Fictional Homes",
            merchant_group="fictional homes",
            category_id="housing",
        ),
        _example(
            "travel-a",
            merchant="Example Travel",
            merchant_group="example travel",
            category_id="travel",
        ),
        _example(
            "travel-b",
            merchant="Example Travel",
            merchant_group="example travel",
            category_id="travel",
        ),
        _example("missing", merchant=None, merchant_group=None),
    )
    dataset = _dataset(examples)
    plan = _plan(minimum_training_samples=2, minimum_test_samples=1)
    first = create_unseen_merchant_split(dataset, plan)
    second = create_unseen_merchant_split(dataset, plan)

    assert first == second
    training_groups = {item.merchant_group for item in first.training_examples}
    test_groups = {item.merchant_group for item in first.test_examples}
    assert training_groups.isdisjoint(test_groups)
    assert first.omitted_without_merchant == 1
    assert "missing" not in {
        item.transaction_id for item in (*first.training_examples, *first.test_examples)
    }

    with pytest.raises(MLCategorisationError) as exc_info:
        create_unseen_merchant_split(
            _dataset(examples[:2]),
            _plan(minimum_training_samples=2, minimum_test_samples=1),
        )
    assert exc_info.value.code is MLCategorisationErrorCode.TOO_FEW_MERCHANT_GROUPS


def test_pipeline_combines_word_and_character_tfidf_with_logistic_regression() -> None:
    pipeline = build_categorisation_pipeline(19)
    features = pipeline.named_steps["features"]
    classifier = pipeline.named_steps["classifier"]
    assert features.transformer_list[0][0] == "word"
    assert features.transformer_list[0][1].ngram_range == (1, 2)
    assert features.transformer_list[1][0] == "character"
    assert features.transformer_list[1][1].analyzer == "char_wb"
    assert features.transformer_list[1][1].ngram_range == (3, 5)
    assert classifier.random_state == 19
    assert classifier.class_weight == "balanced"
    assert classifier.max_iter == 1_000

    texts = [
        build_feature_text("North Market", "weekly basket"),
        build_feature_text("North Market", "fresh groceries"),
        build_feature_text("Home Lettings", "monthly rent"),
        build_feature_text("Home Lettings", "housing payment"),
    ]
    pipeline.fit(texts, ["groceries", "groceries", "housing", "housing"])
    assert pipeline.predict(texts).tolist() == [
        "groceries",
        "groceries",
        "housing",
        "housing",
    ]
    assert pipeline.predict_proba(texts).shape == (4, 2)


def test_evaluation_fits_fresh_pipelines_and_reports_baseline_and_fixed_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical, final = _evaluation_datasets()
    created_pipeline_ids: list[int] = []
    original_builder = ml_module.build_categorisation_pipeline

    def tracking_builder(random_seed: int) -> object:
        pipeline = original_builder(random_seed)
        created_pipeline_ids.append(id(pipeline))
        return pipeline

    monkeypatch.setattr(ml_module, "build_categorisation_pipeline", tracking_builder)
    evaluation = evaluate_categorisation_model(historical, final, _plan())

    assert len(created_pipeline_ids) == 2
    assert len(set(created_pipeline_ids)) == 2
    assert evaluation.chronological.training_end_date < (
        evaluation.chronological.test_start_date
    )
    assert evaluation.chronological.candidate.confusion_matrix.labels == (
        "groceries",
        "housing",
        "travel",
    )
    travel_metric = next(
        metric
        for metric in evaluation.chronological.candidate.per_category
        if metric.category_id == "travel"
    )
    assert travel_metric.support == 1
    assert evaluation.chronological.baseline.confusion_matrix.labels == (
        "groceries",
        "housing",
        "travel",
    )
    for holdout in (evaluation.chronological, evaluation.unseen_merchant):
        for metrics in (holdout.candidate, holdout.baseline):
            assert 0 <= metrics.macro_f1 <= 1
            assert 0 <= metrics.weighted_f1 <= 1
            assert (
                sum(sum(row) for row in metrics.confusion_matrix.rows)
                == holdout.test_count
            )
            assert sum(item.support for item in metrics.per_category) == (
                holdout.test_count
            )
    expected_reason = (
        "candidate beats the most-frequent baseline on both required holdouts"
        if evaluation.candidate_selected
        else "candidate does not consistently beat the required baseline"
    )
    assert evaluation.selection_reason == expected_reason


def test_training_persists_private_candidate_and_standalone_predictions(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed_trainable_history(factory)
    plan = _plan(tmp_path / "private-models")
    result = train_transaction_categoriser(factory, plan=plan)

    assert result.artifact_path == plan.artifact_directory / "synthetic-1.joblib"
    assert result.metadata_path == (
        plan.artifact_directory / "synthetic-1.metadata.json"
    )
    assert result.artifact_path.is_file()
    assert result.metadata_path.is_file()
    assert result.metadata.training_count == 8
    assert result.metadata.manifest.classes == ("groceries", "housing")
    assert result.metadata.category_support == (
        CategorySupport(category_id="groceries", count=4),
        CategorySupport(category_id="housing", count=4),
    )
    assert (
        result.metadata.artifact_sha256
        == sha256(result.artifact_path.read_bytes()).hexdigest()
    )

    safe_metadata = result.metadata_path.read_text(encoding="utf-8")
    assert json.loads(safe_metadata)["training_count"] == 8
    for private_value in (
        PROFILE_ID,
        ACCOUNT_ID,
        "trainable-early-0",
        "Market North",
        "weekly grocery basket",
        "RAW_PRIVATE_TOKEN",
    ):
        assert private_value not in safe_metadata

    loaded = load_transaction_categoriser(
        result.artifact_path,
        result.metadata_path,
        expected_taxonomy_version="1.0",
        trusted_local_artifact=True,
    )
    assert predict_transaction_categories(loaded, ()) == ()
    inputs = (
        MLCategorisationInput(
            transaction_id="prediction-grocery",
            merchant="Market North",
            description="fresh grocery basket",
        ),
        MLCategorisationInput(
            transaction_id="prediction-housing",
            merchant="Homes South",
            description="monthly housing rent",
        ),
    )
    predictions = predict_transaction_categories(loaded, inputs)
    assert tuple(item.transaction_id for item in predictions) == (
        "prediction-grocery",
        "prediction-housing",
    )
    for prediction in predictions:
        assert tuple(item.category_id for item in prediction.probabilities) == (
            "groceries",
            "housing",
        )
        assert sum(
            item.probability for item in prediction.probabilities
        ) == pytest.approx(1)

    with session_scope(factory) as session:
        stored = session.query(VerifiedTransactionRecord).all()
        assert all(item.category_id is None for item in stored)

    with pytest.raises(MLCategorisationError) as exc_info:
        train_transaction_categoriser(factory, plan=plan)
    assert exc_info.value.code is MLCategorisationErrorCode.ARTIFACT_EXISTS


def test_empty_dataset_repository_paths_and_training_failures_are_controlled(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    with pytest.raises(MLCategorisationError) as exc_info:
        build_training_dataset(
            factory,
            user_profile_id="missing-private-profile",
            taxonomy_version="1.0",
            cutoff=_cutoff(),
        )
    assert exc_info.value.code is MLCategorisationErrorCode.PROFILE_NOT_FOUND
    assert "missing-private-profile" not in str(exc_info.value)

    _seed_foundation(factory)
    empty = build_training_dataset(
        factory,
        user_profile_id=PROFILE_ID,
        taxonomy_version="1.0",
        cutoff=_cutoff(),
    )
    assert empty.examples == ()
    assert empty.exclusions == ()
    with pytest.raises(MLCategorisationError) as exc_info:
        train_transaction_categoriser(
            factory,
            plan=_plan(tmp_path / "empty-model"),
        )
    assert exc_info.value.code is MLCategorisationErrorCode.NO_ELIGIBLE_SAMPLES


@pytest.mark.parametrize(
    ("categories", "minimum_samples", "expected_code"),
    [
        (
            ("groceries",),
            2,
            MLCategorisationErrorCode.TOO_FEW_TRAINING_SAMPLES,
        ),
        (
            ("groceries", "groceries"),
            2,
            MLCategorisationErrorCode.TOO_FEW_CLASSES,
        ),
    ],
)
def test_final_training_requires_enough_samples_and_categories(
    factory: sessionmaker[Session],
    tmp_path: Path,
    categories: tuple[str, ...],
    minimum_samples: int,
    expected_code: MLCategorisationErrorCode,
) -> None:
    _seed_foundation(factory)
    with session_scope(factory) as session:
        for index, category_id in enumerate(categories):
            known_at = datetime(2026, 1, index + 2, tzinfo=UTC)
            _add_transaction(
                session,
                f"limited-{index}",
                transaction_date=date(2026, 1, index + 1),
                verified_at=known_at,
                merchant=f"Limited Merchant {index}",
                corrections=((f"limited-correction-{index}", category_id, known_at),),
            )
    with pytest.raises(MLCategorisationError) as exc_info:
        train_transaction_categoriser(
            factory,
            plan=_plan(
                tmp_path / f"limited-{len(categories)}",
                minimum_training_samples=minimum_samples,
            ),
        )
    assert exc_info.value.code is expected_code


def test_loader_rejects_untrusted_missing_invalid_and_incompatible_files(
    trained_candidate: MLCategoriserTrainingResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = trained_candidate.artifact_path
    metadata_path = trained_candidate.metadata_path
    metadata = trained_candidate.metadata

    with pytest.raises(MLCategorisationError) as exc_info:
        load_transaction_categoriser(
            artifact,
            metadata_path,
            expected_taxonomy_version="1.0",
            trusted_local_artifact=False,
        )
    assert exc_info.value.code is MLCategorisationErrorCode.UNTRUSTED_ARTIFACT

    with pytest.raises(MLCategorisationError) as exc_info:
        load_transaction_categoriser(
            tmp_path / "missing.joblib",
            tmp_path / "missing.json",
            expected_taxonomy_version="1.0",
            trusted_local_artifact=True,
        )
    assert exc_info.value.code is MLCategorisationErrorCode.ARTIFACT_NOT_FOUND

    invalid_metadata = tmp_path / "invalid.json"
    invalid_metadata.write_text("not-json", encoding="utf-8")
    with pytest.raises(MLCategorisationError) as exc_info:
        load_transaction_categoriser(
            artifact,
            invalid_metadata,
            expected_taxonomy_version="1.0",
            trusted_local_artifact=True,
        )
    assert exc_info.value.code is MLCategorisationErrorCode.INVALID_MODEL_METADATA

    with pytest.raises(MLCategorisationError) as exc_info:
        load_transaction_categoriser(
            artifact,
            metadata_path,
            expected_taxonomy_version="2.0",
            trusted_local_artifact=True,
        )
    assert exc_info.value.code is MLCategorisationErrorCode.TAXONOMY_MISMATCH

    altered_manifest = metadata.manifest.model_copy(
        update={"feature_schema_version": "future-private-schema"}
    )
    altered_metadata = metadata.model_copy(update={"manifest": altered_manifest})
    monkeypatch.setattr(
        MLTrainingMetadata,
        "model_validate_json",
        classmethod(lambda cls, value: altered_metadata),
    )
    with pytest.raises(MLCategorisationError) as exc_info:
        load_transaction_categoriser(
            artifact,
            metadata_path,
            expected_taxonomy_version="1.0",
            trusted_local_artifact=True,
        )
    assert exc_info.value.code is MLCategorisationErrorCode.FEATURE_SCHEMA_MISMATCH

    wrong_software = metadata.model_copy(
        update={"scikit_learn_version": "0.0.synthetic"}
    )
    monkeypatch.setattr(
        MLTrainingMetadata,
        "model_validate_json",
        classmethod(lambda cls, value: wrong_software),
    )
    with pytest.raises(MLCategorisationError) as exc_info:
        load_transaction_categoriser(
            artifact,
            metadata_path,
            expected_taxonomy_version="1.0",
            trusted_local_artifact=True,
        )
    assert exc_info.value.code is MLCategorisationErrorCode.SOFTWARE_VERSION_MISMATCH


def test_loader_rejects_tampered_or_malformed_artifact_payloads(
    trained_candidate: MLCategoriserTrainingResult,
    tmp_path: Path,
) -> None:
    metadata = trained_candidate.metadata

    tampered = tmp_path / "tampered.joblib"
    tampered.write_bytes(trained_candidate.artifact_path.read_bytes() + b"tampered")
    with pytest.raises(MLCategorisationError) as exc_info:
        load_transaction_categoriser(
            tampered,
            trained_candidate.metadata_path,
            expected_taxonomy_version="1.0",
            trusted_local_artifact=True,
        )
    assert exc_info.value.code is MLCategorisationErrorCode.ARTIFACT_CHECKSUM_MISMATCH

    bad_bytes = tmp_path / "bad-bytes.joblib"
    bad_bytes.write_bytes(b"not a joblib model")
    bad_metadata = metadata.model_copy(
        update={"artifact_sha256": sha256(bad_bytes.read_bytes()).hexdigest()}
    )
    bad_metadata_path = tmp_path / "bad-bytes.json"
    _write_metadata(bad_metadata_path, bad_metadata)
    with pytest.raises(MLCategorisationError) as exc_info:
        load_transaction_categoriser(
            bad_bytes,
            bad_metadata_path,
            expected_taxonomy_version="1.0",
            trusted_local_artifact=True,
        )
    assert exc_info.value.code is MLCategorisationErrorCode.ARTIFACT_MANIFEST_MISMATCH


def test_loader_rejects_pipeline_structure_and_parameter_mismatches(
    trained_candidate: MLCategoriserTrainingResult,
    tmp_path: Path,
) -> None:
    loaded = load_transaction_categoriser(
        trained_candidate.artifact_path,
        trained_candidate.metadata_path,
        expected_taxonomy_version="1.0",
        trusted_local_artifact=True,
    )
    metadata = trained_candidate.metadata
    payloads: list[object] = []

    malformed_steps = build_categorisation_pipeline(7)
    malformed_steps.steps = cast(Any, None)
    payloads.append(
        {
            "manifest": metadata.manifest.model_dump(mode="json"),
            "pipeline": malformed_steps,
        }
    )

    wrong_steps = build_categorisation_pipeline(7)
    wrong_steps.steps = list(reversed(wrong_steps.steps))
    payloads.append(
        {"manifest": metadata.manifest.model_dump(mode="json"), "pipeline": wrong_steps}
    )

    wrong_component = build_categorisation_pipeline(7)
    wrong_component.steps = (
        ("features", "not-features"),
        wrong_component.steps[1],
    )
    payloads.append(
        {
            "manifest": metadata.manifest.model_dump(mode="json"),
            "pipeline": wrong_component,
        }
    )

    malformed_transformers = build_categorisation_pipeline(7)
    malformed_transformers.named_steps["features"].transformer_list = cast(Any, None)
    payloads.append(
        {
            "manifest": metadata.manifest.model_dump(mode="json"),
            "pipeline": malformed_transformers,
        }
    )

    wrong_transformer_names = build_categorisation_pipeline(7)
    existing_transformers = wrong_transformer_names.named_steps[
        "features"
    ].transformer_list
    wrong_transformer_names.named_steps["features"].transformer_list = (
        ("unexpected", existing_transformers[0][1]),
        existing_transformers[1],
    )
    payloads.append(
        {
            "manifest": metadata.manifest.model_dump(mode="json"),
            "pipeline": wrong_transformer_names,
        }
    )

    wrong_transformer_type = build_categorisation_pipeline(7)
    existing_transformers = wrong_transformer_type.named_steps[
        "features"
    ].transformer_list
    wrong_transformer_type.named_steps["features"].transformer_list = (
        ("word", "not-a-vectorizer"),
        existing_transformers[1],
    )
    payloads.append(
        {
            "manifest": metadata.manifest.model_dump(mode="json"),
            "pipeline": wrong_transformer_type,
        }
    )

    wrong_parameters = build_categorisation_pipeline(7)
    wrong_parameters.named_steps["classifier"].max_iter = 2
    payloads.append(
        {
            "manifest": metadata.manifest.model_dump(mode="json"),
            "pipeline": wrong_parameters,
        }
    )

    unfitted = build_categorisation_pipeline(7)
    payloads.append(
        {"manifest": metadata.manifest.model_dump(mode="json"), "pipeline": unfitted}
    )

    wrong_classes = build_categorisation_pipeline(7)
    wrong_classes.fit(
        (
            build_feature_text("Market", "groceries"),
            build_feature_text("Homes", "housing"),
        ),
        ("groceries", "travel"),
    )
    payloads.append(
        {
            "manifest": metadata.manifest.model_dump(mode="json"),
            "pipeline": wrong_classes,
        }
    )

    assert loaded.pipeline is not None
    for index, payload in enumerate(payloads):
        artifact_path, metadata_path = _write_payload_pair(
            tmp_path / f"pipeline-{index}",
            payload,
            metadata,
        )
        with pytest.raises(MLCategorisationError) as exc_info:
            load_transaction_categoriser(
                artifact_path,
                metadata_path,
                expected_taxonomy_version="1.0",
                trusted_local_artifact=True,
            )
        assert (
            exc_info.value.code is MLCategorisationErrorCode.ARTIFACT_MANIFEST_MISMATCH
        )

    for name, payload in (
        ("not-dict", ["synthetic"]),
        (
            "wrong-manifest",
            {"manifest": {"wrong": True}, "pipeline": loaded.pipeline},
        ),
        (
            "not-pipeline",
            {
                "manifest": metadata.manifest.model_dump(mode="json"),
                "pipeline": "synthetic-not-a-pipeline",
            },
        ),
    ):
        artifact_path, sidecar_path = _write_payload_pair(
            tmp_path / name,
            payload,
            metadata,
        )
        with pytest.raises(MLCategorisationError) as exc_info:
            load_transaction_categoriser(
                artifact_path,
                sidecar_path,
                expected_taxonomy_version="1.0",
                trusted_local_artifact=True,
            )
        assert (
            exc_info.value.code is MLCategorisationErrorCode.ARTIFACT_MANIFEST_MISMATCH
        )

    wrong_classes = metadata.model_copy(
        update={
            "manifest": metadata.manifest.model_copy(
                update={"classes": tuple(reversed(metadata.manifest.classes))}
            )
        }
    )
    with pytest.raises(MLCategorisationError) as exc_info:
        predict_transaction_categories(
            ml_module.LoadedTransactionCategoriser(
                pipeline=loaded.pipeline,
                metadata=wrong_classes,
            ),
            (
                MLCategorisationInput(
                    transaction_id="class-mismatch",
                    merchant="Synthetic Merchant",
                    description="synthetic description",
                ),
            ),
        )
    assert exc_info.value.code is MLCategorisationErrorCode.ARTIFACT_MANIFEST_MISMATCH


@pytest.mark.parametrize("collision_number", [1, 2])
def test_atomic_artifact_persistence_cleans_up_race_collisions(
    trained_candidate: MLCategoriserTrainingResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision_number: int,
) -> None:
    loaded = load_transaction_categoriser(
        trained_candidate.artifact_path,
        trained_candidate.metadata_path,
        expected_taxonomy_version="1.0",
        trusted_local_artifact=True,
    )
    directory = tmp_path / f"collision-{collision_number}"
    plan = _plan(directory, model_version=f"collision-{collision_number}")
    original_link = os.link
    link_count = 0

    def colliding_link(source: Path, destination: Path) -> None:
        nonlocal link_count
        link_count += 1
        if link_count == collision_number:
            raise FileExistsError("synthetic race collision")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", colliding_link)
    with pytest.raises(MLCategorisationError) as exc_info:
        ml_module._persist_candidate(
            plan=plan,
            pipeline=loaded.pipeline,
            metadata=trained_candidate.metadata,
        )
    assert exc_info.value.code is MLCategorisationErrorCode.ARTIFACT_EXISTS
    assert not (directory / f"collision-{collision_number}.joblib").exists()
    assert not (directory / f"collision-{collision_number}.metadata.json").exists()
    assert tuple(directory.iterdir()) == ()


def test_ml_training_plan_rejects_incoherent_cutoffs() -> None:
    base: dict[str, Any] = {
        "user_profile_id": PROFILE_ID,
        "model_version": "synthetic-1",
        "taxonomy_version": "1.0",
        "final_cutoff": _cutoff(),
        "chronological_training_cutoff": _cutoff(
            transaction_date=date(2026, 3, 31),
            knowledge_cutoff_at=datetime(2026, 3, 31, 23, 59, tzinfo=UTC),
        ),
        "chronological_test_start": date(2026, 4, 1),
        "unseen_merchant_test_fraction": 0.25,
        "trained_at": datetime(2026, 7, 1, tzinfo=UTC),
    }
    MLTrainingPlan.model_validate(base)

    cases: tuple[dict[str, Any], ...] = (
        {
            "chronological_training_cutoff": _cutoff(
                transaction_date=date(2026, 4, 1),
                knowledge_cutoff_at=datetime(2026, 3, 31, tzinfo=UTC),
            )
        },
        {"chronological_test_start": date(2026, 7, 1)},
        {
            "chronological_training_cutoff": _cutoff(
                transaction_date=date(2026, 3, 31),
                knowledge_cutoff_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
        },
        {
            "chronological_training_cutoff": _cutoff(
                transaction_date=date(2026, 3, 1),
                knowledge_cutoff_at=datetime(2026, 4, 1, tzinfo=UTC),
            )
        },
        {"trained_at": datetime(2026, 6, 1, tzinfo=UTC)},
    )
    expected = (
        "chronological training cutoff must precede",
        "chronological test period must fall within",
        "historical knowledge cutoff cannot follow",
        "historical knowledge cutoff must precede",
        "training time cannot precede",
    )
    for changes, message in zip(cases, expected, strict=True):
        with pytest.raises(ValidationError, match=message):
            MLTrainingPlan.model_validate({**base, **changes})


def test_training_dataset_and_holdout_contracts_reject_duplicate_rows() -> None:
    example = _example("duplicate")
    exclusion = TrainingExclusionCount(
        reason=TrainingExclusionReason.NO_AUTHORITATIVE_LABEL,
        count=1,
    )
    with pytest.raises(ValidationError, match="unique transaction identities"):
        MLTrainingDataset(
            taxonomy_version="1.0",
            cutoff=_cutoff(),
            examples=(example, example),
        )
    with pytest.raises(ValidationError, match="exclusion reasons must be unique"):
        MLTrainingDataset(
            taxonomy_version="1.0",
            cutoff=_cutoff(),
            exclusions=(exclusion, exclusion),
        )
    with pytest.raises(ValidationError, match="fully known by the dataset cutoff"):
        MLTrainingDataset(
            taxonomy_version="1.0",
            cutoff=_cutoff(transaction_date=date(2025, 12, 31)),
            examples=(example,),
        )
    with pytest.raises(ValidationError, match="must be disjoint"):
        MLHoldoutSplit(
            kind=MLHoldoutKind.CHRONOLOGICAL,
            training_examples=(example,),
            test_examples=(example,),
        )
    with pytest.raises(ValidationError, match="merchant groups must be disjoint"):
        MLHoldoutSplit(
            kind=MLHoldoutKind.UNSEEN_MERCHANT,
            training_examples=(_example("group-train"),),
            test_examples=(_example("group-test"),),
        )


@pytest.mark.parametrize(
    ("labels", "rows", "message"),
    [
        (("groceries", "groceries"), ((1, 0), (0, 1)), "labels must be unique"),
        (("groceries", "housing"), ((1,), (0, 1)), "must be square"),
        (("groceries", "housing"), ((1, 0), (0, -1)), "cannot be negative"),
    ],
)
def test_confusion_matrix_validates_shape_and_counts(
    labels: tuple[str, ...],
    rows: tuple[tuple[int, ...], ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ConfusionMatrix(labels=labels, rows=rows)


def test_metrics_and_evaluations_require_stable_semantics() -> None:
    metrics = _metrics()
    with pytest.raises(ValidationError, match="must follow confusion-matrix"):
        ClassificationMetrics(
            **{
                **metrics.model_dump(),
                "per_category": tuple(reversed(metrics.per_category)),
            }
        )
    with pytest.raises(ValidationError, match="support must match"):
        ClassificationMetrics(
            **{
                **metrics.model_dump(),
                "per_category": (
                    metrics.per_category[0].model_copy(update={"support": 0}),
                    metrics.per_category[1],
                ),
            }
        )

    chronological = _holdout_evaluation(MLHoldoutKind.CHRONOLOGICAL)
    unseen = _holdout_evaluation(MLHoldoutKind.UNSEEN_MERCHANT)
    evaluation = MLCategorisationEvaluation(
        chronological=chronological,
        unseen_merchant=unseen,
        candidate_selected=False,
        selection_reason="candidate does not consistently beat the required baseline",
    )
    assert evaluation.candidate_selected is False
    with pytest.raises(ValidationError, match="must be derived"):
        MLCategorisationEvaluation(
            chronological=chronological,
            unseen_merchant=unseen,
            candidate_selected=True,
            selection_reason=(
                "candidate beats the most-frequent baseline on both required holdouts"
            ),
        )

    invalid_ranges = (
        {
            "training_start_date": date(2026, 1, 11),
            "training_end_date": date(2026, 1, 10),
        },
        {
            "test_start_date": date(2026, 2, 11),
            "test_end_date": date(2026, 2, 10),
        },
        {
            "training_end_date": date(2026, 2, 1),
            "test_start_date": date(2026, 2, 1),
        },
    )
    messages = (
        "training date range is invalid",
        "test date range is invalid",
        "training dates must precede",
    )
    for changes, message in zip(invalid_ranges, messages, strict=True):
        with pytest.raises(ValidationError, match=message):
            HoldoutEvaluation.model_validate({**chronological.model_dump(), **changes})

    invalid_evaluation_metrics = (
        (
            {
                "candidate": _metrics().model_copy(
                    update={
                        "confusion_matrix": ConfusionMatrix(
                            labels=("groceries", "housing"),
                            rows=((0, 0), (0, 1)),
                        ),
                        "per_category": (
                            CategoryMetric(
                                category_id="groceries",
                                precision=0,
                                recall=0,
                                f1=0,
                                support=0,
                            ),
                            CategoryMetric(
                                category_id="housing",
                                precision=1,
                                recall=1,
                                f1=1,
                                support=1,
                            ),
                        ),
                    }
                )
            },
            "candidate confusion matrix",
        ),
        (
            {
                "baseline": _metrics().model_copy(
                    update={
                        "confusion_matrix": ConfusionMatrix(
                            labels=("groceries", "housing"),
                            rows=((0, 0), (0, 1)),
                        ),
                        "per_category": (
                            CategoryMetric(
                                category_id="groceries",
                                precision=0,
                                recall=0,
                                f1=0,
                                support=0,
                            ),
                            CategoryMetric(
                                category_id="housing",
                                precision=1,
                                recall=1,
                                f1=1,
                                support=1,
                            ),
                        ),
                    }
                )
            },
            "baseline confusion matrix",
        ),
        (
            {
                "baseline": _metrics(("groceries", "travel")),
            },
            "same labelled test rows",
        ),
    )
    for metric_changes, message in invalid_evaluation_metrics:
        with pytest.raises(ValidationError, match=message):
            HoldoutEvaluation.model_validate(
                {**chronological.model_dump(), **metric_changes}
            )

    with pytest.raises(ValidationError, match="chronological evaluation"):
        MLCategorisationEvaluation(
            chronological=unseen,
            unseen_merchant=unseen,
            candidate_selected=False,
            selection_reason="invalid fixture",
        )
    with pytest.raises(ValidationError, match="unseen-merchant evaluation"):
        MLCategorisationEvaluation(
            chronological=chronological,
            unseen_merchant=chronological,
            candidate_selected=False,
            selection_reason="invalid fixture",
        )


def test_metadata_and_prediction_contracts_validate_internal_totals() -> None:
    chronological = _holdout_evaluation(MLHoldoutKind.CHRONOLOGICAL)
    unseen = _holdout_evaluation(MLHoldoutKind.UNSEEN_MERCHANT)
    evaluation = MLCategorisationEvaluation(
        chronological=chronological,
        unseen_merchant=unseen,
        candidate_selected=False,
        selection_reason="candidate does not consistently beat the required baseline",
    )
    manifest = MLModelManifest(
        model_version="synthetic-1",
        taxonomy_version="1.0",
        classes=("groceries", "housing"),
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    with pytest.raises(ValidationError, match="model classes must be unique"):
        MLModelManifest(
            model_version="synthetic-duplicate",
            taxonomy_version="1.0",
            classes=("groceries", "groceries"),
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    metadata_values: dict[str, Any] = {
        "manifest": manifest,
        "final_cutoff": _cutoff(),
        "chronological_training_cutoff": _cutoff(
            transaction_date=date(2026, 3, 31),
            knowledge_cutoff_at=datetime(2026, 3, 31, 23, 59, tzinfo=UTC),
        ),
        "chronological_test_start": date(2026, 4, 1),
        "unseen_merchant_test_fraction": 0.25,
        "minimum_training_samples": 2,
        "minimum_test_samples": 1,
        "parameters": MLPipelineParameters(random_seed=42),
        "training_count": 4,
        "training_start_date": date(2026, 1, 1),
        "training_end_date": date(2026, 6, 1),
        "category_support": (
            CategorySupport(category_id="groceries", count=2),
            CategorySupport(category_id="housing", count=2),
        ),
        "evaluation": evaluation,
        "python_version": "3.12.11",
        "scikit_learn_version": "1.7.1",
        "artifact_sha256": "a" * 64,
    }
    metadata = MLTrainingMetadata.model_validate(metadata_values)
    assert metadata.training_count == 4

    invalid_metadata = (
        (
            {
                "training_start_date": date(2026, 6, 2),
                "training_end_date": date(2026, 6, 1),
            },
            "training date range is invalid",
        ),
        (
            {
                "category_support": (
                    CategorySupport(category_id="groceries", count=1),
                    CategorySupport(category_id="housing", count=2),
                )
            },
            "account for every training example",
        ),
        (
            {
                "category_support": tuple(
                    reversed(
                        cast(
                            tuple[CategorySupport, ...],
                            metadata_values["category_support"],
                        )
                    )
                )
            },
            "follow the model class order",
        ),
        (
            {
                "chronological_training_cutoff": _cutoff(
                    transaction_date=date(2026, 4, 1),
                    knowledge_cutoff_at=datetime(2026, 3, 31, tzinfo=UTC),
                )
            },
            "metadata cutoffs are inconsistent",
        ),
        (
            {
                "manifest": manifest.model_copy(
                    update={"created_at": datetime(2026, 6, 1, tzinfo=UTC)}
                )
            },
            "training summary exceeds",
        ),
        (
            {
                "evaluation": evaluation.model_copy(
                    update={
                        "chronological": chronological.model_copy(
                            update={"test_end_date": date(2026, 7, 1)}
                        )
                    }
                )
            },
            "evaluation ranges exceed",
        ),
        (
            {
                "minimum_test_samples": 3,
            },
            "does not meet its recorded sample policy",
        ),
        (
            {
                "manifest": manifest.model_copy(
                    update={"classes": ("housing", "groceries")}
                ),
                "category_support": (
                    CategorySupport(category_id="housing", count=2),
                    CategorySupport(category_id="groceries", count=2),
                ),
            },
            "stable sorted order",
        ),
    )
    for changes, message in invalid_metadata:
        with pytest.raises(ValidationError, match=message):
            MLTrainingMetadata.model_validate({**metadata_values, **changes})

    probabilities = (
        MLCategoryProbability(category_id="groceries", probability=0.75),
        MLCategoryProbability(category_id="housing", probability=0.25),
    )
    prediction = MLCategorisationPrediction(
        transaction_id="prediction-1",
        predicted_category_id="groceries",
        probabilities=probabilities,
    )
    assert prediction.predicted_category_id == "groceries"

    invalid_predictions = (
        (
            (probabilities[0], probabilities[0]),
            "housing",
            "categories must be unique",
        ),
        (probabilities, "travel", "must appear"),
        (
            (
                MLCategoryProbability(category_id="groceries", probability=0.5),
                MLCategoryProbability(category_id="housing", probability=0.4),
            ),
            "groceries",
            "must sum to one",
        ),
    )
    for invalid_probabilities, predicted, message in invalid_predictions:
        with pytest.raises(ValidationError, match=message):
            MLCategorisationPrediction(
                transaction_id="prediction-invalid",
                predicted_category_id=predicted,
                probabilities=invalid_probabilities,
            )


def test_training_dataset_uses_only_as_of_verified_authoritative_labels(
    factory: sessionmaker[Session],
) -> None:
    _seed_foundation(factory)
    known_at = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    future_at = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    with session_scope(factory) as session:
        _add_transaction(
            session,
            "eligible-base",
            transaction_date=date(2026, 1, 1),
            verified_at=known_at,
            merchant="North Market",
            description="North Market weekly basket",
            current_category_id="shopping",
            corrections=(("correction-base", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "eligible-prior",
            transaction_date=date(2026, 1, 2),
            verified_at=known_at,
            merchant="Prior Label",
            corrections=(
                ("correction-prior", "groceries", known_at),
                ("correction-future", "utilities", future_at),
            ),
            current_category_id="utilities",
        )
        _add_transaction(
            session,
            "eligible-tie",
            transaction_date=date(2026, 1, 3),
            verified_at=known_at,
            merchant="Tie Merchant",
            corrections=(
                ("correction-tie-a", "groceries", known_at),
                ("correction-tie-z", "housing", known_at),
            ),
            current_category_id="housing",
        )
        _add_transaction(
            session,
            "eligible-csv-review-batch",
            transaction_date=date(2026, 1, 4),
            verified_at=known_at,
            merchant="Home Lettings",
            batch_status="needs_review",
            corrections=(("correction-csv", "housing", known_at),),
        )
        _add_transaction(
            session,
            "eligible-pdf",
            transaction_date=date(2026, 1, 5),
            verified_at=known_at,
            merchant="Example Travel",
            source_type="digital_pdf",
            corrections=(("correction-pdf", "travel", known_at),),
        )
        _add_transaction(
            session,
            "eligible-prior-role",
            transaction_date=date(2026, 1, 6),
            verified_at=known_at,
            merchant="Earlier Role",
            current_role=FinancialRole.EXCLUDED,
            role_audits=(
                (
                    "role-audit-prior",
                    FinancialRole.UNKNOWN,
                    FinancialRole.EXPENSE,
                    known_at,
                ),
                (
                    "role-audit-future",
                    FinancialRole.EXPENSE,
                    FinancialRole.EXCLUDED,
                    future_at,
                ),
            ),
            corrections=(("correction-prior-role", "shopping", known_at),),
        )
        future_suggestion = _add_transaction(
            session,
            "eligible-future-suggestion",
            transaction_date=date(2026, 1, 7),
            verified_at=known_at,
            corrections=(("correction-future-suggestion", "shopping", known_at),),
        )
        _add_transfer_suggestion(
            session,
            future_suggestion.id,
            created_at=future_at,
        )
        _add_transaction(
            session,
            "eligible-future-flag",
            transaction_date=date(2026, 1, 8),
            verified_at=known_at,
            corrections=(("correction-future-flag", "groceries", known_at),),
            needs_review_at=future_at,
        )
        _add_transaction(
            session,
            "auto-label-only",
            transaction_date=date(2026, 2, 1),
            verified_at=known_at,
            current_category_id="groceries",
        )
        _add_transaction(
            session,
            "future-label-only",
            transaction_date=date(2026, 2, 2),
            verified_at=known_at,
            current_category_id="groceries",
            corrections=(("correction-too-new", "groceries", future_at),),
        )
        _add_transaction(
            session,
            "future-transaction",
            transaction_date=date(2026, 7, 1),
            verified_at=known_at,
            corrections=(("correction-future-date", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "future-verification",
            transaction_date=date(2026, 2, 3),
            verified_at=future_at,
            corrections=(("correction-future-verify", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "foreign-profile",
            transaction_date=date(2026, 2, 4),
            verified_at=known_at,
            account_id=OTHER_ACCOUNT_ID,
            corrections=(("correction-foreign", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "raw-needs-review",
            transaction_date=date(2026, 2, 5),
            verified_at=known_at,
            raw_status="needs_review",
            corrections=(("correction-raw", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "unverified-ocr",
            transaction_date=date(2026, 2, 6),
            verified_at=known_at,
            source_type="ocr_pdf",
            batch_status="needs_review",
            corrections=(("correction-ocr", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "unverified-csv-batch",
            transaction_date=date(2026, 2, 6),
            verified_at=known_at,
            batch_status="unverified",
            corrections=(("correction-unverified-csv", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "rejected-csv-batch",
            transaction_date=date(2026, 2, 6),
            verified_at=known_at,
            batch_status="rejected",
            corrections=(("correction-rejected-csv", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "probable-duplicate",
            transaction_date=date(2026, 2, 7),
            verified_at=known_at,
            issues=[{"code": "probable_duplicate", "score": 0.9}],
            corrections=(("correction-probable", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "exact-duplicate",
            transaction_date=date(2026, 2, 8),
            verified_at=known_at,
            issues=[{"code": "exact_duplicate"}],
            corrections=(("correction-exact", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "source-mismatch",
            transaction_date=date(2026, 2, 8),
            verified_at=known_at,
            source_type="ocr_pdf",
            batch_source_type="digital_pdf",
            corrections=(("correction-source", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "unknown-role",
            transaction_date=date(2026, 2, 9),
            verified_at=known_at,
            current_role=FinancialRole.UNKNOWN,
            corrections=(("correction-unknown", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "excluded-role",
            transaction_date=date(2026, 2, 10),
            verified_at=known_at,
            current_role=FinancialRole.EXCLUDED,
            corrections=(("correction-excluded", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "future-role-only",
            transaction_date=date(2026, 2, 10),
            verified_at=known_at,
            role_audits=(
                (
                    "role-audit-too-new",
                    FinancialRole.UNKNOWN,
                    FinancialRole.EXPENSE,
                    future_at,
                ),
            ),
            corrections=(("correction-future-role", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "category-needs-review",
            transaction_date=date(2026, 2, 11),
            verified_at=known_at,
            corrections=(("correction-category-review", "needs_review", known_at),),
        )
        _add_transaction(
            session,
            "empty-feature",
            transaction_date=date(2026, 2, 11),
            verified_at=known_at,
            merchant=None,
            description="---",
            corrections=(("correction-empty", "groceries", known_at),),
        )
        _add_transaction(
            session,
            "inactive-category",
            transaction_date=date(2026, 2, 11),
            verified_at=known_at,
            corrections=(("correction-inactive", "entertainment", known_at),),
        )
        _add_transaction(
            session,
            "wrong-taxonomy",
            transaction_date=date(2026, 2, 11),
            verified_at=known_at,
            corrections=(("correction-taxonomy", "education", known_at),),
        )
        entertainment = session.get(CategoryRecord, "entertainment")
        education = session.get(CategoryRecord, "education")
        assert entertainment is not None
        assert education is not None
        entertainment.is_active = False
        education.taxonomy_version = "2.0"
        _add_transaction(
            session,
            "flagged-review",
            transaction_date=date(2026, 2, 12),
            verified_at=known_at,
            corrections=(("correction-flag", "groceries", known_at),),
            needs_review_at=known_at,
        )
        pending = _add_transaction(
            session,
            "pending-transfer",
            transaction_date=date(2026, 2, 13),
            verified_at=known_at,
            corrections=(("correction-transfer", "transfers", known_at),),
        )
        _add_transfer_suggestion(session, pending.id, created_at=known_at)
        paired_subject = _add_transaction(
            session,
            "paired-transfer-out",
            transaction_date=date(2026, 2, 13),
            verified_at=known_at,
            corrections=(("correction-paired-out", "transfers", known_at),),
        )
        paired_counterpart = _add_transaction(
            session,
            "paired-transfer-in",
            transaction_date=date(2026, 2, 13),
            verified_at=known_at,
            corrections=(("correction-paired-in", "transfers", known_at),),
        )
        _add_transfer_suggestion(
            session,
            paired_subject.id,
            counterpart_transaction_id=paired_counterpart.id,
            created_at=known_at,
        )
        later_rejected = _add_transaction(
            session,
            "later-rejected-transfer",
            transaction_date=date(2026, 2, 14),
            verified_at=known_at,
            corrections=(("correction-later-rejected", "transfers", known_at),),
        )
        _add_transfer_suggestion(
            session,
            later_rejected.id,
            created_at=known_at,
            status="rejected",
            reviewed_at=future_at,
        )
        resolved_transfer = _add_transaction(
            session,
            "eligible-resolved-transfer",
            transaction_date=date(2026, 2, 15),
            verified_at=known_at,
            current_role=FinancialRole.TRANSFER_OUT,
            corrections=(("correction-resolved", "transfers", known_at),),
        )
        _add_transfer_suggestion(
            session,
            resolved_transfer.id,
            created_at=known_at,
            status="confirmed",
            reviewed_at=known_at,
        )

    dataset = build_training_dataset(
        factory,
        user_profile_id=PROFILE_ID,
        taxonomy_version="1.0",
        cutoff=_cutoff(),
    )

    assert {
        example.transaction_id: example.category_id for example in dataset.examples
    } == {
        "eligible-base": "groceries",
        "eligible-prior": "groceries",
        "eligible-tie": "housing",
        "eligible-csv-review-batch": "housing",
        "eligible-pdf": "travel",
        "eligible-prior-role": "shopping",
        "eligible-future-suggestion": "shopping",
        "eligible-future-flag": "groceries",
        "eligible-resolved-transfer": "transfers",
    }
    assert dataset.examples == tuple(
        sorted(
            dataset.examples,
            key=lambda example: (example.transaction_date, example.transaction_id),
        )
    )
    exclusion_counts = {item.reason: item.count for item in dataset.exclusions}
    assert sum(exclusion_counts.values()) == 23
    assert exclusion_counts[TrainingExclusionReason.CATEGORY_INACTIVE] == 1
    assert exclusion_counts[TrainingExclusionReason.TAXONOMY_MISMATCH] == 1
    assert exclusion_counts[TrainingExclusionReason.SOURCE_LINEAGE_MISMATCH] == 1
    with session_scope(factory) as session:
        unresolved = MLCategorisationRepository(
            session
        ).list_unresolved_transfer_transaction_ids_as_of(
            ("paired-transfer-in",),
            knowledge_cutoff_at=KNOWLEDGE_CUTOFF,
        )
    assert unresolved == frozenset({"paired-transfer-in"})
