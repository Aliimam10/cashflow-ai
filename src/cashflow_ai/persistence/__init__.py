"""Public local persistence interfaces."""

from cashflow_ai.persistence.base import Base, UTCDateTime
from cashflow_ai.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.repositories import (
    AccountRepository,
    AnalyticsRepository,
    BalanceSnapshotRepository,
    CategorisationRepository,
    FinancialRoleRepository,
    ImportBatchRepository,
    MLCategorisationRepository,
    MLTrainingCandidateRow,
    ModelMetadataRepository,
    StatementRepository,
    TransactionRepository,
    UserProfileRepository,
)

__all__ = [
    "AccountRepository",
    "AnalyticsRepository",
    "BalanceSnapshotRepository",
    "Base",
    "CategorisationRepository",
    "FinancialRoleRepository",
    "ImportBatchRepository",
    "MLCategorisationRepository",
    "MLTrainingCandidateRow",
    "ModelMetadataRepository",
    "StatementRepository",
    "TransactionRepository",
    "UTCDateTime",
    "UserProfileRepository",
    "create_session_factory",
    "create_sqlite_engine",
    "session_scope",
]
