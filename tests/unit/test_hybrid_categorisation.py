"""Tests for conservative hybrid categorisation and explicit feedback."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_categorisation import _add_transaction, _rules, _seed_foundation

import cashflow_ai.categorisation.hybrid as hybrid_module
from cashflow_ai.categorisation import (
    CategorisationServiceError,
    HybridCategorisationError,
    HybridCategorisationErrorCode,
    apply_category_feedback,
    hybrid_categorise_verified_transactions,
    list_low_confidence_reviews,
    prepare_manual_retraining_dataset,
)
from cashflow_ai.persistence import Base, create_session_factory, create_sqlite_engine
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    CategoryCorrectionRecord,
    CategoryDecisionRecord,
    PersonalCategoryRuleRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas import (
    CategoryDecisionSource,
    CategoryExplanation,
    CategoryExplanationCode,
    CategoryFeedback,
    CategoryFeedbackAction,
    Direction,
    HybridCategorisationPlan,
    HybridCategoryDecision,
    HybridDecisionSource,
    HybridDecisionStatus,
    ManualRetrainingDataset,
    MLCategorisationPrediction,
    MLCategoryProbability,
    MLTrainingDataset,
    ScopedCategoryRule,
    TrainingCutoff,
)

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine: Engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    _seed_foundation(result)
    return result


def _model(*, selected: bool = True, taxonomy: str = "1.0") -> Any:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            evaluation=SimpleNamespace(candidate_selected=selected),
            manifest=SimpleNamespace(
                taxonomy_version=taxonomy,
                model_version="synthetic-model-v1",
            ),
        )
    )


def _prediction(transaction_id: str, confidence: float) -> MLCategorisationPrediction:
    return MLCategorisationPrediction(
        transaction_id=transaction_id,
        predicted_category_id="groceries",
        probabilities=(
            MLCategoryProbability(category_id="groceries", probability=confidence),
            MLCategoryProbability(category_id="housing", probability=1 - confidence),
        ),
    )


def test_hybrid_applies_high_confidence_and_queues_low_confidence(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_scope(factory) as session:
        _add_transaction(session, "high", merchant="Unknown High")
        _add_transaction(session, "low", merchant="Unknown Low", category_id="other")

    def predict(_model: object, inputs: tuple[object, ...]) -> tuple[Any, ...]:
        transaction_id = cast(Any, inputs[0]).transaction_id
        return (_prediction(transaction_id, 0.9 if transaction_id == "high" else 0.6),)

    monkeypatch.setattr(hybrid_module, "predict_transaction_categories", predict)
    result = hybrid_categorise_verified_transactions(
        factory,
        plan=HybridCategorisationPlan(
            user_profile_id="profile-1", confidence_threshold=0.8
        ),
        rule_set=_rules(),
        model=_model(),
    )
    by_id = {item.transaction_id: item for item in result}
    assert by_id["high"].status is HybridDecisionStatus.APPLIED
    assert by_id["high"].source is HybridDecisionSource.ML_MODEL
    assert by_id["high"].changed is True
    assert by_id["low"].status is HybridDecisionStatus.PENDING_REVIEW
    assert by_id["low"].changed is False
    with session_scope(factory) as session:
        high = session.get(VerifiedTransactionRecord, "high")
        low = session.get(VerifiedTransactionRecord, "low")
        assert high is not None
        assert high.category_id == "groceries"
        assert low is not None
        assert low.category_id == "other"
        assert (
            session.scalar(select(func.count()).select_from(CategoryDecisionRecord))
            == 2
        )
    queue = list_low_confidence_reviews(factory, user_profile_id="profile-1")
    assert [(item.transaction_id, item.predicted_category_id) for item in queue] == [
        ("low", "groceries")
    ]
    # Re-running the same policy is idempotent at the audit boundary.
    hybrid_categorise_verified_transactions(
        factory,
        plan=HybridCategorisationPlan(
            user_profile_id="profile-1", confidence_threshold=0.8
        ),
        rule_set=_rules(),
        model=_model(),
    )
    with session_scope(factory) as session:
        assert (
            session.scalar(select(func.count()).select_from(CategoryDecisionRecord))
            == 2
        )
    # Lowering the explicit threshold resolves the old queue item instead of
    # leaving a stale pending prediction beside the new applied decision.
    hybrid_categorise_verified_transactions(
        factory,
        plan=HybridCategorisationPlan(
            user_profile_id="profile-1", confidence_threshold=0.5
        ),
        rule_set=_rules(),
        model=_model(),
    )
    assert list_low_confidence_reviews(factory, user_profile_id="profile-1") == ()
    with session_scope(factory) as session:
        low_decisions = tuple(
            session.scalars(
                select(CategoryDecisionRecord)
                .where(CategoryDecisionRecord.verified_transaction_id == "low")
                .order_by(CategoryDecisionRecord.created_at)
            )
        )
        assert tuple(item.status for item in low_decisions) == (
            "superseded",
            "applied",
        )


def test_rules_precede_ml_and_feedback_creates_only_explicit_narrow_rule(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_scope(factory) as session:
        _add_transaction(session, "coffee", merchant="Synthetic Cafe")
    monkeypatch.setattr(
        hybrid_module,
        "predict_transaction_categories",
        lambda *_args: pytest.fail("ML must not run after a personal rule match"),
    )
    feedback = CategoryFeedback(
        user_profile_id="profile-1",
        transaction_id="coffee",
        category_id="eating_out",
        action=CategoryFeedbackAction.CREATE_PERSONAL_RULE,
        corrected_at=NOW,
        personal_rule=ScopedCategoryRule(
            rule_id="synthetic_cafe_rule",
            user_profile_id="profile-1",
            category_id="eating_out",
            merchant="Synthetic Cafe",
            direction=Direction.OUTFLOW,
            account_id="current-1",
            priority=10,
        ),
    )
    applied = apply_category_feedback(factory, feedback=feedback)
    assert applied.created_rule_id == "synthetic_cafe_rule"
    assert applied.correction_id
    result = hybrid_categorise_verified_transactions(
        factory,
        plan=HybridCategorisationPlan(
            user_profile_id="profile-1", confidence_threshold=0.9
        ),
        rule_set=_rules(),
        model=_model(),
    )
    # The transaction-specific correction is the strongest precedence tier.
    assert result[0].source is HybridDecisionSource.TRANSACTION_DECISION
    with session_scope(factory) as session:
        rule = session.get(PersonalCategoryRuleRecord, "synthetic_cafe_rule")
        assert rule is not None
        assert rule.merchant == "synthetic cafe"
        assert (
            session.scalar(select(func.count()).select_from(CategoryCorrectionRecord))
            == 1
        )


def test_transaction_only_feedback_supersedes_queue_without_creating_rule(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        transaction = _add_transaction(session, "review", merchant="Synthetic Shop")
        session.add(
            CategoryDecisionRecord(
                id="pending-1",
                verified_transaction_id=transaction.id,
                category_id="groceries",
                source="ml_model",
                status="pending_review",
                confidence=Decimal("0.55"),
                model_version="synthetic-model-v1",
                taxonomy_version="1.0",
                rule_set_version="test-rules-1",
                reason_code="ml_confidence_review",
                created_at=NOW,
            )
        )
    result = apply_category_feedback(
        factory,
        feedback=CategoryFeedback(
            user_profile_id="profile-1",
            transaction_id="review",
            category_id="housing",
            action=CategoryFeedbackAction.TRANSACTION_ONLY,
            corrected_at=NOW,
        ),
    )
    assert result.created_rule_id is None
    assert result.superseded_decision_count == 1
    assert list_low_confidence_reviews(factory, user_profile_id="profile-1") == ()
    with session_scope(factory) as session:
        decision = session.get(CategoryDecisionRecord, "pending-1")
        assert decision is not None
        assert decision.status == "superseded"
        assert decision.reviewed_at is not None
        assert decision.reviewed_at >= NOW


@pytest.mark.parametrize(
    ("model", "code"),
    [
        (_model(selected=False), HybridCategorisationErrorCode.MODEL_NOT_SELECTED),
        (_model(taxonomy="2.0"), HybridCategorisationErrorCode.MODEL_TAXONOMY_MISMATCH),
    ],
)
def test_hybrid_rejects_unqualified_model(
    model: Any, code: HybridCategorisationErrorCode
) -> None:
    with pytest.raises(HybridCategorisationError) as exc_info:
        hybrid_categorise_verified_transactions(
            cast(Any, None),
            plan=HybridCategorisationPlan(
                user_profile_id="profile-1", confidence_threshold=0.8
            ),
            rule_set=_rules(),
            model=model,
        )
    assert exc_info.value.code is code


def test_feedback_and_selection_contracts_fail_closed(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(ValidationError):
        HybridCategorisationPlan(
            user_profile_id="profile-1", confidence_threshold=0.8, transaction_ids=()
        )
    with pytest.raises(ValidationError):
        HybridCategorisationPlan(
            user_profile_id="profile-1",
            confidence_threshold=0.8,
            transaction_ids=("one", "one"),
        )
    with pytest.raises(ValidationError):
        CategoryFeedback(
            user_profile_id="profile-1",
            transaction_id="one",
            category_id="housing",
            action=CategoryFeedbackAction.TRANSACTION_ONLY,
            corrected_at=datetime(2026, 8, 13),
        )
    with pytest.raises(ValidationError):
        CategoryFeedback(
            user_profile_id="profile-1",
            transaction_id="one",
            category_id="housing",
            action=CategoryFeedbackAction.CREATE_PERSONAL_RULE,
            corrected_at=NOW,
        )
    with pytest.raises(CategorisationServiceError):
        apply_category_feedback(
            factory,
            feedback=CategoryFeedback(
                user_profile_id="profile-1",
                transaction_id="missing",
                category_id="housing",
                action=CategoryFeedbackAction.TRANSACTION_ONLY,
                corrected_at=NOW,
            ),
        )
    explanation = CategoryExplanation(
        source=CategoryDecisionSource.NEEDS_REVIEW,
        code=CategoryExplanationCode.NO_DETERMINISTIC_MATCH,
        message="Synthetic controlled reason.",
    )
    with pytest.raises(ValidationError):
        HybridCategoryDecision(
            transaction_id="one",
            previous_category_id=None,
            category_id="groceries",
            source=HybridDecisionSource.ML_MODEL,
            status=HybridDecisionStatus.APPLIED,
            changed=False,
            explanation=explanation,
        )
    with pytest.raises(ValidationError):
        HybridCategoryDecision(
            transaction_id="one",
            previous_category_id=None,
            category_id="groceries",
            source=HybridDecisionSource.NEEDS_REVIEW,
            status=HybridDecisionStatus.PENDING_REVIEW,
            changed=True,
            explanation=explanation,
        )
    base_rule = ScopedCategoryRule(
        rule_id="feedback_validation",
        user_profile_id="profile-2",
        category_id="groceries",
        merchant="Synthetic Merchant",
    )
    with pytest.raises(ValidationError):
        CategoryFeedback(
            user_profile_id="profile-1",
            transaction_id="one",
            category_id="groceries",
            action=CategoryFeedbackAction.CREATE_PERSONAL_RULE,
            corrected_at=NOW,
            personal_rule=base_rule,
        )
    with pytest.raises(ValidationError):
        CategoryFeedback(
            user_profile_id="profile-2",
            transaction_id="one",
            category_id="housing",
            action=CategoryFeedbackAction.CREATE_PERSONAL_RULE,
            corrected_at=NOW,
            personal_rule=base_rule,
        )


def test_manual_retraining_wrapper_uses_exact_cutoff(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    cutoff = TrainingCutoff(transaction_date=date(2026, 8, 1), knowledge_cutoff_at=NOW)
    dataset = MLTrainingDataset(
        taxonomy_version="1.0", cutoff=cutoff, examples=(), exclusions=()
    )
    monkeypatch.setattr(
        hybrid_module, "build_training_dataset", lambda *_args, **_kwargs: dataset
    )
    result = prepare_manual_retraining_dataset(
        factory,
        user_profile_id="profile-1",
        taxonomy_version="1.0",
        cutoff=cutoff,
    )
    assert result.dataset == dataset
    with pytest.raises(ValidationError):
        ManualRetrainingDataset(
            user_profile_id="profile-1",
            cutoff=cutoff,
            dataset=cast(
                Any,
                {
                    "taxonomy_version": "1.0",
                    "cutoff": {
                        "transaction_date": "2026-07-31",
                        "knowledge_cutoff_at": NOW.isoformat(),
                    },
                },
            ),
        )


def test_hybrid_and_feedback_controlled_error_paths(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(CategorisationServiceError):
        hybrid_categorise_verified_transactions(
            factory,
            plan=HybridCategorisationPlan(
                user_profile_id="missing", confidence_threshold=0.8
            ),
            rule_set=_rules(),
            model=None,
        )
    with session_scope(factory) as session:
        _add_transaction(session, "owned", merchant="Synthetic Merchant")
    with pytest.raises(CategorisationServiceError):
        hybrid_categorise_verified_transactions(
            factory,
            plan=HybridCategorisationPlan(
                user_profile_id="profile-1",
                confidence_threshold=0.8,
                transaction_ids=("missing",),
            ),
            rule_set=_rules(),
            model=None,
        )
    fallback = hybrid_categorise_verified_transactions(
        factory,
        plan=HybridCategorisationPlan(
            user_profile_id="profile-1",
            confidence_threshold=0.8,
            transaction_ids=("owned",),
        ),
        rule_set=_rules(),
        model=None,
    )
    assert fallback[0].source is HybridDecisionSource.NEEDS_REVIEW
    monkeypatch.setattr(
        hybrid_module,
        "predict_transaction_categories",
        lambda *_args: (
            MLCategorisationPrediction(
                transaction_id="owned",
                predicted_category_id="missing_category",
                probabilities=(
                    MLCategoryProbability(
                        category_id="missing_category", probability=0.9
                    ),
                    MLCategoryProbability(category_id="groceries", probability=0.1),
                ),
            ),
        ),
    )
    with pytest.raises(CategorisationServiceError):
        hybrid_categorise_verified_transactions(
            factory,
            plan=HybridCategorisationPlan(
                user_profile_id="profile-1",
                confidence_threshold=0.8,
                transaction_ids=("owned",),
            ),
            rule_set=_rules(),
            model=_model(),
        )
    with pytest.raises(CategorisationServiceError):
        apply_category_feedback(
            factory,
            feedback=CategoryFeedback(
                user_profile_id="profile-1",
                transaction_id="owned",
                category_id="missing_category",
                action=CategoryFeedbackAction.TRANSACTION_ONLY,
                corrected_at=NOW,
            ),
        )
    mismatched = CategoryFeedback(
        user_profile_id="profile-1",
        transaction_id="owned",
        category_id="groceries",
        action=CategoryFeedbackAction.CREATE_PERSONAL_RULE,
        corrected_at=NOW,
        personal_rule=ScopedCategoryRule(
            rule_id="conflicting_rule",
            user_profile_id="profile-1",
            category_id="groceries",
            merchant="Different Merchant",
        ),
    )
    with pytest.raises(HybridCategorisationError) as exc_info:
        apply_category_feedback(factory, feedback=mismatched)
    assert exc_info.value.code is HybridCategorisationErrorCode.FEEDBACK_RULE_MISMATCH
    assert mismatched.personal_rule is not None
    valid = mismatched.model_copy(
        update={
            "corrected_at": datetime.now(UTC),
            "personal_rule": mismatched.personal_rule.model_copy(
                update={"merchant": "Synthetic Merchant"}
            ),
        }
    )
    apply_category_feedback(factory, feedback=valid)
    with pytest.raises(HybridCategorisationError) as exc_info:
        apply_category_feedback(factory, feedback=valid)
    assert exc_info.value.code is HybridCategorisationErrorCode.PERSONAL_RULE_CONFLICT


def test_feedback_timestamp_and_full_personal_scope_fail_closed(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        transaction = _add_transaction(
            session,
            "feedback-clock",
            merchant="Synthetic Merchant",
            description="Synthetic weekly purchase",
        )
        session.add(
            CategoryDecisionRecord(
                id="later-decision",
                verified_transaction_id=transaction.id,
                category_id="groceries",
                source="ml_model",
                status="pending_review",
                confidence=Decimal("0.55"),
                model_version="synthetic-model-v1",
                taxonomy_version="1.0",
                rule_set_version="test-rules-1",
                reason_code="ml_confidence_review",
                created_at=NOW + timedelta(seconds=1),
            )
        )

    base = CategoryFeedback(
        user_profile_id="profile-1",
        transaction_id="feedback-clock",
        category_id="groceries",
        action=CategoryFeedbackAction.TRANSACTION_ONLY,
        corrected_at=NOW,
    )
    for timestamp in (NOW - timedelta(seconds=1), datetime(2099, 1, 1, tzinfo=UTC)):
        with pytest.raises(HybridCategorisationError) as exc_info:
            apply_category_feedback(
                factory, feedback=base.model_copy(update={"corrected_at": timestamp})
            )
        assert (
            exc_info.value.code
            is HybridCategorisationErrorCode.INVALID_FEEDBACK_TIMESTAMP
        )
    with pytest.raises(HybridCategorisationError) as exc_info:
        apply_category_feedback(factory, feedback=base)
    assert (
        exc_info.value.code is HybridCategorisationErrorCode.INVALID_FEEDBACK_TIMESTAMP
    )

    staged_rule = base.model_copy(
        update={
            "action": CategoryFeedbackAction.CREATE_PERSONAL_RULE,
            "personal_rule": ScopedCategoryRule(
                rule_id="rolled_back_rule",
                user_profile_id="profile-1",
                category_id="groceries",
                merchant="Synthetic Merchant",
                direction=Direction.OUTFLOW,
            ),
        }
    )
    with pytest.raises(HybridCategorisationError):
        apply_category_feedback(factory, feedback=staged_rule)
    with session_scope(factory) as session:
        assert session.get(PersonalCategoryRuleRecord, "rolled_back_rule") is None

    wrong_direction = base.model_copy(
        update={
            "action": CategoryFeedbackAction.CREATE_PERSONAL_RULE,
            "corrected_at": NOW + timedelta(seconds=2),
            "personal_rule": ScopedCategoryRule(
                rule_id="wrong_direction",
                user_profile_id="profile-1",
                category_id="groceries",
                merchant="Synthetic Merchant",
                direction=Direction.INFLOW,
            ),
        }
    )
    with pytest.raises(HybridCategorisationError) as exc_info:
        apply_category_feedback(factory, feedback=wrong_direction)
    assert exc_info.value.code is HybridCategorisationErrorCode.FEEDBACK_RULE_MISMATCH

    with session_scope(factory) as session:
        later = session.get(CategoryDecisionRecord, "later-decision")
        assert later is not None
        later.created_at = NOW
    accepted = apply_category_feedback(
        factory,
        feedback=base.model_copy(update={"corrected_at": NOW + timedelta(seconds=1)}),
    )
    with session_scope(factory) as session:
        stored = session.get(CategoryCorrectionRecord, accepted.correction_id)
        assert stored is not None
        # A caller may report an older event time, but training visibility starts
        # only at the authoritative server receipt time.
        assert stored.corrected_at > NOW + timedelta(seconds=1)
    with pytest.raises(HybridCategorisationError) as exc_info:
        apply_category_feedback(
            factory,
            feedback=base.model_copy(
                update={"corrected_at": NOW + timedelta(seconds=1)}
            ),
        )
    assert accepted.category_id == "groceries"
    assert (
        exc_info.value.code is HybridCategorisationErrorCode.INVALID_FEEDBACK_TIMESTAMP
    )
