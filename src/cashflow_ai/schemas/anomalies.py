"""Typed contracts for review-only transaction anomaly detection."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cashflow_ai.schemas.money import Money
from cashflow_ai.schemas.transactions import Identifier


class _AnomalyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class AnomalySignalCode(StrEnum):
    """Controlled reasons that may place a transaction in the review queue."""

    EXACT_DUPLICATE = "exact_duplicate"
    PROBABLE_DUPLICATE = "probable_duplicate"
    UNUSUALLY_LARGE_TRANSACTION = "unusually_large_transaction"
    NEW_MERCHANT_HIGH_SPENDING = "new_merchant_high_spending"
    RECURRING_PRICE_INCREASE = "recurring_price_increase"
    CHARGE_AFTER_CANCELLATION = "charge_after_cancellation"
    UNUSUALLY_HIGH_DAILY_SPENDING = "unusually_high_daily_spending"
    NEGATIVE_BALANCE_EVENT = "negative_balance_event"
    ISOLATION_FOREST = "isolation_forest"


class AnomalyUserLabel(StrEnum):
    """Careful user wording that never claims fraud."""

    UNUSUAL = "Unusual"
    POSSIBLE_DUPLICATE = "Possible duplicate"
    NEEDS_REVIEW = "Needs review"


class AnomalyDetectionMode(StrEnum):
    """Whether adequate evidence allowed the advanced detector to run."""

    RULES_AND_MODEL = "rules_and_model"
    RULES_ONLY = "rules_only"


class AnomalyWarningCode(StrEnum):
    """Stable reasons for returning transparent rule-only results."""

    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    INSUFFICIENT_HISTORY = "insufficient_history"
    NO_ELIGIBLE_DETECTION_TRANSACTIONS = "no_eligible_detection_transactions"


class AnomalyFeedbackAction(StrEnum):
    """User interpretation of one recomputed review suggestion."""

    EXPECTED_ACTIVITY = "expected_activity"
    CONFIRMED_UNUSUAL = "confirmed_unusual"


class AnomalyReviewStatus(StrEnum):
    """Persisted latest-review state supported by the current schema."""

    REVIEWED = "reviewed"
    DISMISSED = "dismissed"


class AnomalyExclusionReason(StrEnum):
    """Why a verified row did not enter Isolation Forest."""

    UNCOVERED_DATE = "uncovered_date"
    TRANSFER = "transfer"
    DUPLICATE = "duplicate"
    PENDING = "pending"
    UNRESOLVED_ROLE = "unresolved_role"


class AnomalyDetectionPolicy(_AnomalyModel):
    """Explicit history, rule, and Isolation Forest thresholds."""

    history_lookback_days: int = Field(default=180, ge=14, le=3_650)
    detection_window_days: int = Field(default=30, ge=1, le=365)
    minimum_covered_days: int = Field(default=90, ge=1, le=3_650)
    minimum_coverage_ratio: float = Field(default=0.8, ge=0, le=1)
    minimum_history_transactions: int = Field(default=20, ge=2, le=100_000)
    large_transaction_mad_multiplier: float = Field(default=6, gt=0, le=100)
    minimum_large_amount: Money = Field(default=Decimal("100.00"), gt=0)
    new_merchant_high_spend_quantile: float = Field(default=0.9, ge=0.5, le=1)
    minimum_new_merchant_amount: Money = Field(default=Decimal("50.00"), gt=0)
    recurring_price_increase_ratio: float = Field(default=0.1, gt=0, le=10)
    minimum_recurring_price_increase: Money = Field(default=Decimal("1.00"), gt=0)
    daily_spending_mad_multiplier: float = Field(default=6, gt=0, le=100)
    minimum_high_daily_spending: Money = Field(default=Decimal("100.00"), gt=0)
    isolation_estimators: int = Field(default=200, ge=50, le=2_000)
    isolation_contamination: float = Field(default=0.05, gt=0, le=0.25)
    maximum_merchant_gap_days: int = Field(default=365, ge=1, le=3_650)
    random_seed: int = Field(default=42, ge=0)

    @model_validator(mode="after")
    def validate_windows(self) -> AnomalyDetectionPolicy:
        """Keep a non-empty reference history before the detection window."""
        reference_days = self.history_lookback_days - self.detection_window_days
        if reference_days < 1:
            raise ValueError("detection window must be shorter than history lookback")
        if self.minimum_covered_days > reference_days:
            raise ValueError(
                "minimum covered days cannot exceed the reference-history window"
            )
        return self


class AnomalyDetectionPlan(_AnomalyModel):
    """Owned accounts and point-in-time boundary for one anomaly scan."""

    user_profile_id: Identifier
    account_ids: tuple[Identifier, ...] = Field(min_length=1)
    as_of_date: date
    knowledge_cutoff_at: AwareDatetime
    policy: AnomalyDetectionPolicy = Field(default_factory=AnomalyDetectionPolicy)

    @model_validator(mode="after")
    def validate_scope(self) -> AnomalyDetectionPlan:
        """Reject ambiguous account scopes and impossible cutoff dates."""
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("anomaly account IDs must be unique")
        complete_at = datetime.combine(
            self.as_of_date + timedelta(days=1), time.min, tzinfo=UTC
        )
        if self.knowledge_cutoff_at.astimezone(UTC) < complete_at:
            raise ValueError(
                "anomaly knowledge cutoff must follow the complete UTC as-of date"
            )
        return self

    @property
    def detection_start_date(self) -> date:
        """First date whose transactions may be returned as current alerts."""
        return self.as_of_date - timedelta(days=self.policy.detection_window_days - 1)

    @property
    def reference_start_date(self) -> date:
        """First date available to establish historical normal behaviour."""
        return self.as_of_date - timedelta(days=self.policy.history_lookback_days - 1)

    @property
    def reference_end_date(self) -> date:
        """Last training date, immediately before current detection starts."""
        return self.detection_start_date - timedelta(days=1)


class AnomalySignal(_AnomalyModel):
    """One controlled, privacy-safe reason supporting a review item."""

    code: AnomalySignalCode
    score: Decimal = Field(ge=0, le=1, max_digits=7, decimal_places=6)
    observed_amount: Money | None = None
    reference_amount: Money | None = None
    related_transaction_id: Identifier | None = None


class TransactionAnomalyAlert(_AnomalyModel):
    """One transaction-level review item without raw bank description text."""

    transaction_id: Identifier
    account_id: Identifier
    transaction_date: date
    label: AnomalyUserLabel
    score: Decimal = Field(ge=0, le=1, max_digits=7, decimal_places=6)
    signals: tuple[AnomalySignal, ...] = Field(min_length=1)
    model_score: Decimal | None = Field(
        default=None, ge=0, le=1, max_digits=7, decimal_places=6
    )
    review_status: AnomalyReviewStatus | None = None

    @model_validator(mode="after")
    def validate_alert(self) -> TransactionAnomalyAlert:
        """Keep wording, score, and model evidence aligned with their signals."""
        codes = tuple(signal.code for signal in self.signals)
        if len(set(codes)) != len(codes):
            raise ValueError("anomaly signal codes must be unique per transaction")
        expected_label = (
            AnomalyUserLabel.POSSIBLE_DUPLICATE
            if any(
                code
                in {
                    AnomalySignalCode.EXACT_DUPLICATE,
                    AnomalySignalCode.PROBABLE_DUPLICATE,
                }
                for code in codes
            )
            else (
                AnomalyUserLabel.UNUSUAL
                if codes == (AnomalySignalCode.ISOLATION_FOREST,)
                else AnomalyUserLabel.NEEDS_REVIEW
            )
        )
        if self.label is not expected_label:
            raise ValueError("anomaly user label does not match its signal types")
        isolation_scores = tuple(
            signal.score
            for signal in self.signals
            if signal.code is AnomalySignalCode.ISOLATION_FOREST
        )
        if (self.model_score is None) != (not isolation_scores):
            raise ValueError("model score must appear exactly with Isolation Forest")
        if isolation_scores and self.model_score != isolation_scores[0]:
            raise ValueError("model score must match the Isolation Forest signal")
        if self.score != max(signal.score for signal in self.signals):
            raise ValueError("alert score must equal its strongest signal")
        return self


class AnomalyExclusionCount(_AnomalyModel):
    """Aggregate privacy-safe count for one model exclusion reason."""

    reason: AnomalyExclusionReason
    count: int = Field(ge=1)


class IsolationForestRunMetadata(_AnomalyModel):
    """Reproducibility facts for the in-memory candidate model run."""

    model_type: str = Field(pattern=r"^IsolationForest$")
    model_version: str = Field(min_length=1, max_length=50)
    feature_schema_version: str = Field(min_length=1, max_length=50)
    feature_names: tuple[str, ...] = Field(min_length=8)
    training_start_date: date
    training_end_date: date
    training_transaction_count: int = Field(ge=2)
    scored_transaction_count: int = Field(ge=1)
    category_levels: tuple[str, ...] = Field(min_length=1)
    estimators: int = Field(ge=50)
    contamination: float = Field(gt=0, le=0.25)
    random_seed: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_training_period(self) -> IsolationForestRunMetadata:
        """Require ordered model-training dates and unique feature metadata."""
        if self.training_end_date < self.training_start_date:
            raise ValueError("anomaly model training dates are reversed")
        if len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("anomaly feature names must be unique")
        if len(set(self.category_levels)) != len(self.category_levels):
            raise ValueError("anomaly category levels must be unique")
        return self


class AnomalyDetectionResult(_AnomalyModel):
    """Complete read-only scan result with explicit evidence sufficiency."""

    plan: AnomalyDetectionPlan
    mode: AnomalyDetectionMode
    alerts: tuple[TransactionAnomalyAlert, ...]
    verified_transaction_count: int = Field(ge=0)
    reference_transaction_count: int = Field(ge=0)
    scored_transaction_count: int = Field(ge=0)
    minimum_reference_covered_days: int = Field(ge=0)
    minimum_reference_coverage_ratio: float = Field(ge=0, le=1)
    exclusions: tuple[AnomalyExclusionCount, ...]
    warnings: tuple[AnomalyWarningCode, ...]
    model_metadata: IsolationForestRunMetadata | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> AnomalyDetectionResult:
        """Tie model evidence to the declared mode and scored-row count."""
        model_ran = self.mode is AnomalyDetectionMode.RULES_AND_MODEL
        if model_ran != (self.model_metadata is not None):
            raise ValueError("anomaly mode and model metadata disagree")
        if model_ran and self.scored_transaction_count < 1:
            raise ValueError("a model run must score at least one transaction")
        if not model_ran and self.scored_transaction_count != 0:
            raise ValueError("rule-only results cannot claim model-scored rows")
        if any(
            later.transaction_date < earlier.transaction_date
            for earlier, later in pairwise(self.alerts)
        ):
            raise ValueError("anomaly alerts must be chronologically ordered")
        return self


class AnomalyFeedbackRequest(_AnomalyModel):
    """Reproducible scan scope and explicit feedback for one transaction alert."""

    plan: AnomalyDetectionPlan
    transaction_id: Identifier
    action: AnomalyFeedbackAction


class AnomalyFeedbackResult(_AnomalyModel):
    """Data-minimised acknowledgement of the latest saved review state."""

    transaction_id: Identifier
    action: AnomalyFeedbackAction
    status: AnomalyReviewStatus


__all__ = [
    "AnomalyDetectionMode",
    "AnomalyDetectionPlan",
    "AnomalyDetectionPolicy",
    "AnomalyDetectionResult",
    "AnomalyExclusionCount",
    "AnomalyExclusionReason",
    "AnomalyFeedbackAction",
    "AnomalyFeedbackRequest",
    "AnomalyFeedbackResult",
    "AnomalyReviewStatus",
    "AnomalySignal",
    "AnomalySignalCode",
    "AnomalyUserLabel",
    "AnomalyWarningCode",
    "IsolationForestRunMetadata",
    "TransactionAnomalyAlert",
]
