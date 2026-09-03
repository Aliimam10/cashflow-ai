"""Public deterministic and learned transaction categorisation boundaries."""

from cashflow_ai.categorisation.hybrid import (
    HybridCategorisationError,
    HybridCategorisationErrorCode,
    apply_category_feedback,
    hybrid_categorise_verified_transactions,
    list_low_confidence_reviews,
    prepare_manual_retraining_dataset,
)
from cashflow_ai.categorisation.ml import (
    LoadedTransactionCategoriser,
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
from cashflow_ai.categorisation.service import (
    CategorisationServiceError,
    CategorisationServiceErrorCode,
    categorise_verified_transactions,
    list_categories,
)

__all__ = [
    "CategorisationServiceError",
    "CategorisationServiceErrorCode",
    "HybridCategorisationError",
    "HybridCategorisationErrorCode",
    "LoadedTransactionCategoriser",
    "MLCategorisationError",
    "MLCategorisationErrorCode",
    "apply_category_feedback",
    "build_categorisation_pipeline",
    "build_feature_text",
    "build_training_dataset",
    "categorise_verified_transactions",
    "create_chronological_split",
    "create_unseen_merchant_split",
    "evaluate_categorisation_model",
    "hybrid_categorise_verified_transactions",
    "list_categories",
    "list_low_confidence_reviews",
    "load_transaction_categoriser",
    "predict_transaction_categories",
    "prepare_manual_retraining_dataset",
    "train_transaction_categoriser",
]
