"""Coverage-aware rule alerts and in-memory Isolation Forest detection."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from statistics import median
from typing import Any

from sklearn import ensemble  # type: ignore[import-untyped]
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.imports.duplicates import assess_duplicate_facts
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    CategoryDecisionRecord,
    FinancialRoleAuditRecord,
    ImportBatchRecord,
    ImportContextRecord,
    RawTransactionRecord,
    RecurringPaymentCandidateRecord,
    StatementCoverageRecord,
    UserProfileRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.schemas.anomalies import (
    AnomalyDetectionMode,
    AnomalyDetectionPlan,
    AnomalyDetectionResult,
    AnomalyExclusionCount,
    AnomalyExclusionReason,
    AnomalySignal,
    AnomalySignalCode,
    AnomalyUserLabel,
    AnomalyWarningCode,
    IsolationForestRunMetadata,
    TransactionAnomalyAlert,
)
from cashflow_ai.schemas.categorisation import normalise_rule_text
from cashflow_ai.schemas.duplicates import DuplicateFacts, DuplicateStatus

_ZERO = Decimal("0.00")
_SCORE_QUANTUM = Decimal("0.000001")
_MONEY_QUANTUM = Decimal("0.01")
_TRANSFER_ROLES = frozenset({"transfer_in", "transfer_out"})
_UNRESOLVED_ROLES = frozenset({"unknown", "excluded"})
_SPENDING_ROLES = frozenset({"expense", "cash_withdrawal"})
_PENDING_TYPES = frozenset(
    {"pending", "pending card", "authorization", "authorisation"}
)
_FEATURE_NAMES = (
    "log_absolute_amount",
    "merchant_frequency",
    "category",
    "weekday",
    "days_since_previous_merchant_transaction",
    "difference_from_merchant_median",
    "difference_from_category_median",
    "merchant_novelty",
)


class AnomalyDetectionErrorCode(StrEnum):
    """Stable failures that callers may map to an API response later."""

    PROFILE_NOT_FOUND = "profile_not_found"
    ACCOUNT_NOT_FOUND = "account_not_found"
    ACCOUNT_NOT_OWNED = "account_not_owned"


class AnomalyDetectionError(ValueError):
    """Expected anomaly-service failure with a controlled code."""

    def __init__(self, code: AnomalyDetectionErrorCode, message: str) -> None:
        """Attach a stable machine-readable code to a safe message."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _TransactionEvidence:
    transaction_id: str
    account_id: str
    transaction_date: date
    description: str
    merchant: str | None
    merchant_key: str
    amount: Decimal
    balance_after: Decimal | None
    category_id: str | None
    financial_role: str
    external_id: str | None
    transaction_type: str | None
    source_fingerprint: str
    canonical_fingerprint: str | None
    issue_codes: frozenset[str]

    @property
    def absolute_amount(self) -> Decimal:
        return abs(self.amount)

    @property
    def category_key(self) -> str:
        return self.category_id or "uncategorised"

    @property
    def pending(self) -> bool:
        normalised = normalise_rule_text(self.transaction_type or "")
        return normalised in _PENDING_TYPES or normalised.startswith("pending ")


@dataclass(frozen=True)
class _RecurringEvidence:
    status: str
    expected_amount: Decimal
    reviewed_date: date


@dataclass
class _FeatureState:
    merchant_counts: Counter[str] = field(default_factory=Counter)
    merchant_amounts: defaultdict[str, list[Decimal]] = field(
        default_factory=lambda: defaultdict(list)
    )
    category_amounts: defaultdict[str, list[Decimal]] = field(
        default_factory=lambda: defaultdict(list)
    )
    last_merchant_date: dict[str, date] = field(default_factory=dict)

    def vector(
        self,
        item: _TransactionEvidence,
        *,
        category_levels: tuple[str, ...],
        maximum_gap_days: int,
    ) -> list[float]:
        """Build past-only numeric features before updating state with this row."""
        merchant_history = self.merchant_amounts[item.merchant_key]
        category_history = self.category_amounts[item.category_key]
        merchant_median = (
            Decimal(median(merchant_history))
            if merchant_history
            else item.absolute_amount
        )
        category_median = (
            Decimal(median(category_history))
            if category_history
            else item.absolute_amount
        )
        previous_date = self.last_merchant_date.get(item.merchant_key)
        merchant_gap = (
            maximum_gap_days
            if previous_date is None
            else min(
                maximum_gap_days,
                max(0, (item.transaction_date - previous_date).days),
            )
        )
        values = [
            math.log1p(float(item.absolute_amount)),
            float(self.merchant_counts[item.merchant_key]),
            float(item.transaction_date.weekday()),
            float(merchant_gap),
            float(item.absolute_amount - merchant_median),
            float(item.absolute_amount - category_median),
            1.0 if not merchant_history else 0.0,
        ]
        values.extend(
            1.0 if item.category_key == category else 0.0
            for category in category_levels
        )
        return values

    def update(self, item: _TransactionEvidence) -> None:
        """Reveal one observed row only after its feature vector is complete."""
        self.merchant_counts[item.merchant_key] += 1
        self.merchant_amounts[item.merchant_key].append(item.absolute_amount)
        self.category_amounts[item.category_key].append(item.absolute_amount)
        self.last_merchant_date[item.merchant_key] = item.transaction_date


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _score(value: float | Decimal) -> Decimal:
    parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    return max(Decimal("0"), min(Decimal("1"), parsed)).quantize(
        _SCORE_QUANTUM, rounding=ROUND_HALF_UP
    )


def _signal_strength(observed: Decimal, reference: Decimal) -> Decimal:
    if reference <= 0:
        return Decimal("1.000000")
    excess_ratio = max(_ZERO, (observed - reference) / reference)
    return _score(Decimal("0.60") + min(Decimal("0.39"), excess_ratio / 2))


def _issue_codes(value: Any) -> frozenset[str]:
    """Extract controlled issue identifiers without retaining raw issue messages."""
    codes: set[str] = set()
    if isinstance(value, list | tuple):
        for item in value:
            codes.update(_issue_codes(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"code", "issue_code"} and isinstance(item, str):
                codes.add(item.casefold())
            elif isinstance(item, list | tuple | dict):
                codes.update(_issue_codes(item))
    return frozenset(codes)


def _latest_roles_as_of(
    session: Session,
    transaction_ids: tuple[str, ...],
    cutoff: Any,
) -> dict[str, str]:
    if not transaction_ids:
        return {}
    rows = session.execute(
        select(FinancialRoleAuditRecord)
        .where(
            FinancialRoleAuditRecord.verified_transaction_id.in_(transaction_ids),
            FinancialRoleAuditRecord.changed_at <= cutoff,
        )
        .order_by(
            FinancialRoleAuditRecord.changed_at,
            FinancialRoleAuditRecord.id,
        )
    ).scalars()
    return {row.verified_transaction_id: row.new_role_id for row in rows}


def _latest_categories_as_of(
    session: Session,
    transaction_ids: tuple[str, ...],
    cutoff: Any,
) -> dict[str, str]:
    if not transaction_ids:
        return {}
    rows = session.scalars(
        select(CategoryDecisionRecord)
        .where(
            CategoryDecisionRecord.verified_transaction_id.in_(transaction_ids),
            CategoryDecisionRecord.created_at <= cutoff,
            or_(
                CategoryDecisionRecord.status != "superseded",
                CategoryDecisionRecord.reviewed_at > cutoff,
            ),
        )
        .order_by(CategoryDecisionRecord.created_at, CategoryDecisionRecord.id)
    )
    return {row.verified_transaction_id: row.category_id for row in rows}


def _load_transactions(
    session: Session,
    plan: AnomalyDetectionPlan,
) -> tuple[_TransactionEvidence, ...]:
    rows = session.execute(
        select(VerifiedTransactionRecord, RawTransactionRecord)
        .join(
            RawTransactionRecord,
            RawTransactionRecord.id == VerifiedTransactionRecord.raw_transaction_id,
        )
        .join(
            ImportBatchRecord,
            ImportBatchRecord.id == RawTransactionRecord.import_batch_id,
        )
        .where(
            VerifiedTransactionRecord.account_id.in_(plan.account_ids),
            VerifiedTransactionRecord.transaction_date >= plan.reference_start_date,
            VerifiedTransactionRecord.transaction_date <= plan.as_of_date,
            VerifiedTransactionRecord.verified_at <= plan.knowledge_cutoff_at,
            RawTransactionRecord.created_at <= plan.knowledge_cutoff_at,
            ImportBatchRecord.imported_at <= plan.knowledge_cutoff_at,
            ImportBatchRecord.account_id == VerifiedTransactionRecord.account_id,
            RawTransactionRecord.review_status == "confirmed",
            RawTransactionRecord.source_type == ImportBatchRecord.source_type,
            or_(
                and_(
                    ImportBatchRecord.source_type == "csv",
                    ImportBatchRecord.verification_status.in_(
                        ("verified", "needs_review")
                    ),
                ),
                and_(
                    ImportBatchRecord.source_type.in_(("digital_pdf", "ocr_pdf")),
                    ImportBatchRecord.verification_status == "verified",
                ),
            ),
        )
        .order_by(
            VerifiedTransactionRecord.transaction_date,
            VerifiedTransactionRecord.account_id,
            VerifiedTransactionRecord.id,
        )
    ).all()
    ids = tuple(transaction.id for transaction, _raw in rows)
    roles = _latest_roles_as_of(session, ids, plan.knowledge_cutoff_at)
    categories = _latest_categories_as_of(session, ids, plan.knowledge_cutoff_at)
    return tuple(
        _TransactionEvidence(
            transaction_id=transaction.id,
            account_id=transaction.account_id,
            transaction_date=transaction.transaction_date,
            description=transaction.description,
            merchant=transaction.merchant,
            merchant_key=normalise_rule_text(
                transaction.merchant or transaction.description
            ),
            amount=transaction.amount,
            balance_after=transaction.balance_after,
            category_id=categories.get(transaction.id),
            financial_role=roles.get(transaction.id, "unknown"),
            external_id=transaction.external_id,
            transaction_type=transaction.transaction_type,
            source_fingerprint=raw.source_fingerprint,
            canonical_fingerprint=raw.canonical_fingerprint,
            issue_codes=_issue_codes(raw.issues_json),
        )
        for transaction, raw in rows
    )


def _subtract_missing_days(
    start: date,
    end: date,
    missing: list[dict[str, str]],
) -> set[date]:
    known = {start + timedelta(days=offset) for offset in range((end - start).days + 1)}
    for period in missing:
        missing_start = max(start, date.fromisoformat(period["start_date"]))
        missing_end = min(end, date.fromisoformat(period["end_date"]))
        if missing_start <= missing_end:
            for offset in range((missing_end - missing_start).days + 1):
                known.discard(missing_start + timedelta(days=offset))
    return known


def _covered_account_days(
    session: Session,
    plan: AnomalyDetectionPlan,
) -> set[tuple[str, date]]:
    rows = session.execute(
        select(ImportBatchRecord.account_id, StatementCoverageRecord)
        .join(
            ImportContextRecord,
            ImportContextRecord.import_batch_id == ImportBatchRecord.id,
        )
        .join(
            StatementCoverageRecord,
            StatementCoverageRecord.import_context_id == ImportContextRecord.id,
        )
        .where(
            ImportBatchRecord.account_id.in_(plan.account_ids),
            ImportBatchRecord.imported_at <= plan.knowledge_cutoff_at,
            StatementCoverageRecord.statement_end_date >= plan.reference_start_date,
            StatementCoverageRecord.statement_start_date <= plan.as_of_date,
            StatementCoverageRecord.coverage_status.in_(
                ("complete", "overlapping", "gapped")
            ),
            or_(
                and_(
                    ImportBatchRecord.source_type == "csv",
                    ImportBatchRecord.verification_status.in_(
                        ("verified", "needs_review")
                    ),
                ),
                and_(
                    ImportBatchRecord.source_type.in_(("digital_pdf", "ocr_pdf")),
                    ImportBatchRecord.verification_status == "verified",
                ),
            ),
        )
    ).all()
    result: set[tuple[str, date]] = set()
    for account_id, coverage in rows:
        start = max(plan.reference_start_date, coverage.statement_start_date)
        end = min(plan.as_of_date, coverage.statement_end_date)
        for covered_date in _subtract_missing_days(
            start, end, coverage.missing_periods_json
        ):
            result.add((account_id, covered_date))
    return result


def _recurring_evidence(
    session: Session,
    plan: AnomalyDetectionPlan,
) -> dict[tuple[str, str], _RecurringEvidence]:
    rows = session.scalars(
        select(RecurringPaymentCandidateRecord)
        .where(
            RecurringPaymentCandidateRecord.account_id.in_(plan.account_ids),
            RecurringPaymentCandidateRecord.detected_at <= plan.knowledge_cutoff_at,
            RecurringPaymentCandidateRecord.knowledge_cutoff_at
            <= plan.knowledge_cutoff_at,
            RecurringPaymentCandidateRecord.reviewed_at <= plan.knowledge_cutoff_at,
            RecurringPaymentCandidateRecord.status.in_(("confirmed", "cancelled")),
        )
        .order_by(
            RecurringPaymentCandidateRecord.reviewed_at,
            RecurringPaymentCandidateRecord.id,
        )
    )
    return {
        (row.account_id, normalise_rule_text(row.merchant_group)): _RecurringEvidence(
            status=row.status,
            expected_amount=abs(row.expected_amount),
            reviewed_date=row.reviewed_at.date(),
        )
        for row in rows
        if row.reviewed_at is not None
    }


def _duplicate_signal(
    item: _TransactionEvidence,
    previous: tuple[_TransactionEvidence, ...],
) -> AnomalySignal | None:
    if "exact_duplicate" in item.issue_codes:
        return AnomalySignal(
            code=AnomalySignalCode.EXACT_DUPLICATE,
            score=Decimal("1.000000"),
            observed_amount=item.absolute_amount,
        )
    if "probable_duplicate" in item.issue_codes:
        return AnomalySignal(
            code=AnomalySignalCode.PROBABLE_DUPLICATE,
            score=Decimal("0.800000"),
            observed_amount=item.absolute_amount,
        )
    if item.canonical_fingerprint is None:
        return None
    incoming = DuplicateFacts(
        source_fingerprint=item.source_fingerprint,
        canonical_fingerprint=item.canonical_fingerprint,
        account_id=item.account_id,
        transaction_date=item.transaction_date,
        amount=item.amount,
        description=item.description,
        merchant=item.merchant,
        external_id=item.external_id,
    )
    best: tuple[DuplicateStatus, float, str] | None = None
    for candidate in previous:
        if candidate.canonical_fingerprint is None:
            continue
        assessment = assess_duplicate_facts(
            incoming,
            DuplicateFacts(
                source_fingerprint=candidate.source_fingerprint,
                canonical_fingerprint=candidate.canonical_fingerprint,
                account_id=candidate.account_id,
                transaction_date=candidate.transaction_date,
                amount=candidate.amount,
                description=candidate.description,
                merchant=candidate.merchant,
                external_id=candidate.external_id,
            ),
        )
        if assessment.status is DuplicateStatus.UNIQUE:
            continue
        if best is None or assessment.score > best[1]:
            best = (assessment.status, assessment.score, candidate.transaction_id)
    if best is None:
        return None
    status, duplicate_score, related_id = best
    code = (
        AnomalySignalCode.EXACT_DUPLICATE
        if status is DuplicateStatus.EXACT
        else AnomalySignalCode.PROBABLE_DUPLICATE
    )
    return AnomalySignal(
        code=code,
        score=_score(duplicate_score),
        observed_amount=item.absolute_amount,
        related_transaction_id=related_id,
    )


def _median_and_mad(values: tuple[Decimal, ...]) -> tuple[Decimal, Decimal]:
    centre = Decimal(median(values))
    deviation = Decimal(median(tuple(abs(item - centre) for item in values)))
    return centre, deviation


def _quantile(values: tuple[Decimal, ...], probability: float) -> Decimal:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = Decimal(str(probability)) * (len(ordered) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return (
        ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction
    )


def _model_exclusion(
    item: _TransactionEvidence,
    *,
    covered: set[tuple[str, date]],
    duplicate_ids: set[str],
) -> AnomalyExclusionReason | None:
    if (item.account_id, item.transaction_date) not in covered:
        return AnomalyExclusionReason.UNCOVERED_DATE
    if item.pending:
        return AnomalyExclusionReason.PENDING
    if item.transaction_id in duplicate_ids:
        return AnomalyExclusionReason.DUPLICATE
    if item.financial_role in _TRANSFER_ROLES:
        return AnomalyExclusionReason.TRANSFER
    if item.financial_role in _UNRESOLVED_ROLES:
        return AnomalyExclusionReason.UNRESOLVED_ROLE
    return None


def _general_rule_signals(
    item: _TransactionEvidence,
    *,
    reference: tuple[_TransactionEvidence, ...],
    recurring: _RecurringEvidence | None,
    large_threshold: Decimal | None,
    new_merchant_threshold: Decimal | None,
    high_daily_dates: dict[date, tuple[Decimal, str]],
    plan: AnomalyDetectionPlan,
) -> tuple[AnomalySignal, ...]:
    signals: list[AnomalySignal] = []
    if item.balance_after is not None and item.balance_after < 0:
        signals.append(
            AnomalySignal(
                code=AnomalySignalCode.NEGATIVE_BALANCE_EVENT,
                score=Decimal("0.950000"),
                observed_amount=abs(item.balance_after),
                reference_amount=Decimal("0.00"),
            )
        )
    if recurring is not None:
        if (
            recurring.status == "cancelled"
            and item.transaction_date > recurring.reviewed_date
        ):
            signals.append(
                AnomalySignal(
                    code=AnomalySignalCode.CHARGE_AFTER_CANCELLATION,
                    score=Decimal("0.950000"),
                    observed_amount=item.absolute_amount,
                    reference_amount=recurring.expected_amount,
                )
            )
        elif recurring.status == "confirmed":
            increase = item.absolute_amount - recurring.expected_amount
            ratio_threshold = recurring.expected_amount * Decimal(
                str(plan.policy.recurring_price_increase_ratio)
            )
            required_increase = max(
                plan.policy.minimum_recurring_price_increase, ratio_threshold
            )
            if increase >= required_increase:
                signals.append(
                    AnomalySignal(
                        code=AnomalySignalCode.RECURRING_PRICE_INCREASE,
                        score=_signal_strength(
                            item.absolute_amount, recurring.expected_amount
                        ),
                        observed_amount=item.absolute_amount,
                        reference_amount=recurring.expected_amount,
                    )
                )
            return tuple(signals)
    if large_threshold is not None and item.absolute_amount > large_threshold:
        signals.append(
            AnomalySignal(
                code=AnomalySignalCode.UNUSUALLY_LARGE_TRANSACTION,
                score=_signal_strength(item.absolute_amount, large_threshold),
                observed_amount=item.absolute_amount,
                reference_amount=_money(large_threshold),
            )
        )
    known_merchants = {row.merchant_key for row in reference}
    if (
        new_merchant_threshold is not None
        and item.financial_role in _SPENDING_ROLES
        and item.merchant_key not in known_merchants
        and item.absolute_amount >= new_merchant_threshold
    ):
        signals.append(
            AnomalySignal(
                code=AnomalySignalCode.NEW_MERCHANT_HIGH_SPENDING,
                score=_signal_strength(item.absolute_amount, new_merchant_threshold),
                observed_amount=item.absolute_amount,
                reference_amount=_money(new_merchant_threshold),
            )
        )
    high_daily = high_daily_dates.get(item.transaction_date)
    if high_daily is not None and high_daily[1] == item.transaction_id:
        observed, _transaction_id = high_daily
        signals.append(
            AnomalySignal(
                code=AnomalySignalCode.UNUSUALLY_HIGH_DAILY_SPENDING,
                score=Decimal("0.900000"),
                observed_amount=_money(observed),
            )
        )
    return tuple(signals)


def _model_feature_rows(
    reference: tuple[_TransactionEvidence, ...],
    detection: tuple[_TransactionEvidence, ...],
    *,
    maximum_gap_days: int,
) -> tuple[tuple[list[float], ...], tuple[list[float], ...], tuple[str, ...]]:
    category_levels = tuple(sorted({item.category_key for item in reference}))
    if not category_levels:
        category_levels = ("uncategorised",)
    state = _FeatureState()
    training_vectors: list[list[float]] = []
    for item in reference:
        training_vectors.append(
            state.vector(
                item,
                category_levels=category_levels,
                maximum_gap_days=maximum_gap_days,
            )
        )
        state.update(item)
    detection_vectors: list[list[float]] = []
    for item in detection:
        detection_vectors.append(
            state.vector(
                item,
                category_levels=category_levels,
                maximum_gap_days=maximum_gap_days,
            )
        )
        state.update(item)
    return tuple(training_vectors), tuple(detection_vectors), category_levels


def _isolation_scores(
    reference: tuple[_TransactionEvidence, ...],
    detection: tuple[_TransactionEvidence, ...],
    plan: AnomalyDetectionPlan,
) -> tuple[dict[str, Decimal], IsolationForestRunMetadata]:
    train_features, detection_features, category_levels = _model_feature_rows(
        reference,
        detection,
        maximum_gap_days=plan.policy.maximum_merchant_gap_days,
    )
    model = ensemble.IsolationForest(
        n_estimators=plan.policy.isolation_estimators,
        contamination=plan.policy.isolation_contamination,
        random_state=plan.policy.random_seed,
        n_jobs=1,
    )
    model.fit(train_features)
    decisions = model.decision_function(detection_features)
    predictions = model.predict(detection_features)
    flagged: dict[str, Decimal] = {}
    for item, decision, prediction in zip(
        detection, decisions, predictions, strict=True
    ):
        if prediction == -1:
            probability_like_score = 1 / (1 + math.exp(8 * float(decision)))
            flagged[item.transaction_id] = _score(probability_like_score)
    metadata = IsolationForestRunMetadata(
        model_type="IsolationForest",
        model_version="isolation-forest-1.0",
        feature_schema_version="anomaly-features-1.0",
        feature_names=_FEATURE_NAMES,
        training_start_date=min(item.transaction_date for item in reference),
        training_end_date=max(item.transaction_date for item in reference),
        training_transaction_count=len(reference),
        scored_transaction_count=len(detection),
        category_levels=category_levels,
        estimators=plan.policy.isolation_estimators,
        contamination=plan.policy.isolation_contamination,
        random_seed=plan.policy.random_seed,
    )
    return flagged, metadata


def _coverage_sufficiency(
    plan: AnomalyDetectionPlan,
    covered: set[tuple[str, date]],
) -> tuple[int, float, bool]:
    reference_days = (plan.reference_end_date - plan.reference_start_date).days + 1
    counts = tuple(
        sum(
            (account_id, plan.reference_start_date + timedelta(days=offset)) in covered
            for offset in range(reference_days)
        )
        for account_id in plan.account_ids
    )
    minimum_days = min(counts, default=0)
    minimum_ratio = minimum_days / reference_days
    adequate = (
        minimum_days >= plan.policy.minimum_covered_days
        and minimum_ratio >= plan.policy.minimum_coverage_ratio
    )
    return minimum_days, minimum_ratio, adequate


def detect_unusual_transactions(
    factory: sessionmaker[Session],
    *,
    plan: AnomalyDetectionPlan,
) -> AnomalyDetectionResult:
    """Return review-only rule and model alerts without changing stored data."""
    with session_scope(factory) as session:
        if session.get(UserProfileRecord, plan.user_profile_id) is None:
            raise AnomalyDetectionError(
                AnomalyDetectionErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        accounts = tuple(
            session.scalars(
                select(AccountRecord).where(AccountRecord.id.in_(plan.account_ids))
            )
        )
        found_ids = {account.id for account in accounts}
        if missing := set(plan.account_ids) - found_ids:
            raise AnomalyDetectionError(
                AnomalyDetectionErrorCode.ACCOUNT_NOT_FOUND,
                f"anomaly account does not exist: {sorted(missing)[0]}",
            )
        if foreign := tuple(
            account.id
            for account in accounts
            if account.user_profile_id != plan.user_profile_id
        ):
            raise AnomalyDetectionError(
                AnomalyDetectionErrorCode.ACCOUNT_NOT_OWNED,
                f"anomaly account is not owned by the profile: {sorted(foreign)[0]}",
            )
        transactions = _load_transactions(session, plan)
        covered = _covered_account_days(session, plan)
        recurring = _recurring_evidence(session, plan)

    detection = tuple(
        item
        for item in transactions
        if item.transaction_date >= plan.detection_start_date
    )
    duplicate_signals: dict[str, AnomalySignal] = {}
    for index, item in enumerate(transactions):
        if item.transaction_date < plan.detection_start_date or item.pending:
            continue
        signal = _duplicate_signal(
            item,
            tuple(
                candidate for candidate in transactions[:index] if not candidate.pending
            ),
        )
        if signal is not None:
            duplicate_signals[item.transaction_id] = signal
    duplicate_ids = set(duplicate_signals)

    exclusions: Counter[AnomalyExclusionReason] = Counter()
    reference_eligible: list[_TransactionEvidence] = []
    detection_eligible: list[_TransactionEvidence] = []
    for item in transactions:
        exclusion = _model_exclusion(item, covered=covered, duplicate_ids=duplicate_ids)
        if exclusion is not None:
            exclusions[exclusion] += 1
        elif item.transaction_date <= plan.reference_end_date:
            reference_eligible.append(item)
        else:
            detection_eligible.append(item)

    reference_rows = tuple(reference_eligible)
    detection_rows = tuple(detection_eligible)
    reference_amounts = tuple(item.absolute_amount for item in reference_rows)
    large_threshold: Decimal | None = None
    if len(reference_amounts) >= 2:
        centre, mad = _median_and_mad(reference_amounts)
        large_threshold = max(
            plan.policy.minimum_large_amount,
            centre
            + max(mad, Decimal("1.00"))
            * Decimal(str(plan.policy.large_transaction_mad_multiplier)),
        )
    spending_reference = tuple(
        item.absolute_amount
        for item in reference_rows
        if item.financial_role in _SPENDING_ROLES
    )
    new_merchant_threshold = (
        max(
            plan.policy.minimum_new_merchant_amount,
            _quantile(spending_reference, plan.policy.new_merchant_high_spend_quantile),
        )
        if spending_reference
        else None
    )

    reference_daily: defaultdict[date, Decimal] = defaultdict(lambda: _ZERO)
    for item in reference_rows:
        if item.financial_role in _SPENDING_ROLES:
            reference_daily[item.transaction_date] += item.absolute_amount
    historical_daily_values = tuple(
        reference_daily.get(plan.reference_start_date + timedelta(days=offset), _ZERO)
        for offset in range(
            (plan.reference_end_date - plan.reference_start_date).days + 1
        )
        if any(
            (
                account_id,
                plan.reference_start_date + timedelta(days=offset),
            )
            in covered
            for account_id in plan.account_ids
        )
    )
    daily_threshold: Decimal | None = None
    if historical_daily_values:
        daily_centre, daily_mad = _median_and_mad(historical_daily_values)
        daily_threshold = max(
            plan.policy.minimum_high_daily_spending,
            daily_centre
            + max(daily_mad, Decimal("1.00"))
            * Decimal(str(plan.policy.daily_spending_mad_multiplier)),
        )
    current_daily: defaultdict[date, list[_TransactionEvidence]] = defaultdict(list)
    for item in detection_rows:
        recurrence = recurring.get((item.account_id, item.merchant_key))
        if item.financial_role in _SPENDING_ROLES and not (
            recurrence is not None and recurrence.status == "confirmed"
        ):
            current_daily[item.transaction_date].append(item)
    high_daily_dates: dict[date, tuple[Decimal, str]] = {}
    if daily_threshold is not None:
        for spending_date, rows in current_daily.items():
            total = sum((item.absolute_amount for item in rows), _ZERO)
            if total > daily_threshold:
                anchor = max(
                    rows, key=lambda item: (item.absolute_amount, item.transaction_id)
                )
                high_daily_dates[spending_date] = (total, anchor.transaction_id)

    signal_map: defaultdict[str, list[AnomalySignal]] = defaultdict(list)
    for item in detection:
        if item.pending:
            continue
        if duplicate := duplicate_signals.get(item.transaction_id):
            signal_map[item.transaction_id].append(duplicate)
            continue
        exclusion = _model_exclusion(item, covered=covered, duplicate_ids=duplicate_ids)
        if exclusion is not None:
            continue
        signal_map[item.transaction_id].extend(
            _general_rule_signals(
                item,
                reference=reference_rows,
                recurring=recurring.get((item.account_id, item.merchant_key)),
                large_threshold=large_threshold,
                new_merchant_threshold=new_merchant_threshold,
                high_daily_dates=high_daily_dates,
                plan=plan,
            )
        )

    minimum_covered_days, minimum_coverage_ratio, adequate_coverage = (
        _coverage_sufficiency(plan, covered)
    )
    adequate_history = len(reference_rows) >= plan.policy.minimum_history_transactions
    model_detection = tuple(
        item
        for item in detection_rows
        if not (
            (recurrence := recurring.get((item.account_id, item.merchant_key)))
            is not None
            and recurrence.status == "confirmed"
        )
    )
    warnings: list[AnomalyWarningCode] = []
    if not adequate_coverage:
        warnings.append(AnomalyWarningCode.INSUFFICIENT_COVERAGE)
    if not adequate_history:
        warnings.append(AnomalyWarningCode.INSUFFICIENT_HISTORY)
    if not model_detection:
        warnings.append(AnomalyWarningCode.NO_ELIGIBLE_DETECTION_TRANSACTIONS)

    model_metadata: IsolationForestRunMetadata | None = None
    if adequate_coverage and adequate_history and model_detection:
        model_scores, model_metadata = _isolation_scores(
            reference_rows, model_detection, plan
        )
        for transaction_id, model_score in model_scores.items():
            signal_map[transaction_id].append(
                AnomalySignal(
                    code=AnomalySignalCode.ISOLATION_FOREST,
                    score=model_score,
                )
            )

    transaction_by_id = {item.transaction_id: item for item in detection}
    alerts: list[TransactionAnomalyAlert] = []
    for transaction_id, signals in signal_map.items():
        if not signals:
            continue
        item = transaction_by_id[transaction_id]
        ordered_signals = tuple(sorted(signals, key=lambda signal: signal.code.value))
        codes = {signal.code for signal in ordered_signals}
        label = (
            AnomalyUserLabel.POSSIBLE_DUPLICATE
            if codes
            & {
                AnomalySignalCode.EXACT_DUPLICATE,
                AnomalySignalCode.PROBABLE_DUPLICATE,
            }
            else (
                AnomalyUserLabel.UNUSUAL
                if codes == {AnomalySignalCode.ISOLATION_FOREST}
                else AnomalyUserLabel.NEEDS_REVIEW
            )
        )
        alert_model_score = next(
            (
                signal.score
                for signal in ordered_signals
                if signal.code is AnomalySignalCode.ISOLATION_FOREST
            ),
            None,
        )
        alerts.append(
            TransactionAnomalyAlert(
                transaction_id=transaction_id,
                account_id=item.account_id,
                transaction_date=item.transaction_date,
                label=label,
                score=max(signal.score for signal in ordered_signals),
                signals=ordered_signals,
                model_score=alert_model_score,
            )
        )
    alerts.sort(
        key=lambda item: (item.transaction_date, item.account_id, item.transaction_id)
    )
    return AnomalyDetectionResult(
        plan=plan,
        mode=(
            AnomalyDetectionMode.RULES_AND_MODEL
            if model_metadata is not None
            else AnomalyDetectionMode.RULES_ONLY
        ),
        alerts=tuple(alerts),
        verified_transaction_count=len(transactions),
        reference_transaction_count=len(reference_rows),
        scored_transaction_count=(
            model_metadata.scored_transaction_count if model_metadata else 0
        ),
        minimum_reference_covered_days=minimum_covered_days,
        minimum_reference_coverage_ratio=minimum_coverage_ratio,
        exclusions=tuple(
            AnomalyExclusionCount(reason=reason, count=count)
            for reason, count in sorted(
                exclusions.items(), key=lambda item: item[0].value
            )
        ),
        warnings=tuple(warnings),
        model_metadata=model_metadata,
    )


__all__ = [
    "AnomalyDetectionError",
    "AnomalyDetectionErrorCode",
    "detect_unusual_transactions",
]
