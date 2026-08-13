"""Public deterministic and learned transaction categorisation boundaries."""

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
)

__all__ = [
    "CategorisationServiceError",
    "CategorisationServiceErrorCode",
    "LoadedTransactionCategoriser",
    "MLCategorisationError",
    "MLCategorisationErrorCode",
    "build_categorisation_pipeline",
    "build_feature_text",
    "build_training_dataset",
    "categorise_verified_transactions",
    "create_chronological_split",
    "create_unseen_merchant_split",
    "evaluate_categorisation_model",
    "load_transaction_categoriser",
    "predict_transaction_categories",
    "train_transaction_categoriser",
]
