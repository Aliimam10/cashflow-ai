"""Conservative, user-confirmed financial-role interpretation services."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum
from hashlib import sha256
from typing import cast

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.invalidation import invalidate_derived_results_in_session
from cashflow_ai.persistence.base import utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    AccountRecord,
    FinancialRoleAuditRecord,
    FinancialRoleSuggestionRecord,
    VerifiedTransactionRecord,
)
from cashflow_ai.persistence.repositories import (
    FinancialRoleRepository,
    UserProfileRepository,
)
from cashflow_ai.schemas.financial_roles import (
    FinancialRoleAudit,
    FinancialRoleSuggestion,
    RoleAssignment,
    RoleDecisionResult,
    RoleDecisionSource,
    RoleReviewItem,
    RoleSuggestionKind,
    RoleSuggestionReason,
    RoleSuggestionStatus,
    TransactionReviewAction,
)
from cashflow_ai.schemas.invalidation import SourceDataChangeType
from cashflow_ai.schemas.transactions import FinancialRole

SUGGESTION_ALGORITHM_VERSION = "role-rules-1.0"
TRANSFER_DATE_WINDOW_DAYS = 3
_WORD_PATTERN = re.compile(r"[^a-z0-9]+")
_TRANSFER_TERMS = frozenset({"transfer", "xfer", "savings", "internal"})
_REFUND_TERMS = frozenset({"refund", "refunded", "reversal", "reversed", "chargeback"})
_REIMBURSEMENT_PHRASES = (
    "reimbursement",
    "reimbursed",
    "expense repayment",
    "expenses repayment",
)


class FinancialRoleServiceErrorCode(StrEnum):
    """Stable public failures from the role-review boundary."""

    PROFILE_NOT_FOUND = "profile_not_found"
    TRANSACTION_NOT_FOUND = "transaction_not_found"
    SUGGESTION_NOT_FOUND = "suggestion_not_found"
    SUGGESTION_ALREADY_REVIEWED = "suggestion_already_reviewed"
    STALE_SUGGESTION = "stale_suggestion"
    SIGN_INCOMPATIBLE_ROLE = "sign_incompatible_role"
    INVALID_REVIEW_CHRONOLOGY = "invalid_review_chronology"


class FinancialRoleServiceError(ValueError):
    """Controlled role-review failure that never contains raw descriptions."""

    def __init__(self, code: FinancialRoleServiceErrorCode, message: str) -> None:
        """Store a stable error code and privacy-safe message."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _Candidate:
    transaction: VerifiedTransactionRecord
    account: AccountRecord


@dataclass(frozen=True, slots=True)
class _SuggestionDraft:
    subject: _Candidate
    counterpart: _Candidate | None
    kind: RoleSuggestionKind
    suggested_role: FinancialRole
    counterpart_role: FinancialRole | None
    confidence: float
    reasons: tuple[RoleSuggestionReason, ...]


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "role-review timestamps must be timezone-aware"
        raise ValueError(msg)


def _authoritative_review_time(reported_at: datetime) -> datetime:
    """Return server receipt time after validating the reported client value."""
    _require_aware(reported_at)
    received_at = utc_now()
    _require_aware(received_at)
    return received_at


def _require_receipt_after_evidence(
    repository: FinancialRoleRepository,
    transaction: VerifiedTransactionRecord,
    *,
    received_at: datetime,
    suggestion_created_at: datetime | None = None,
) -> None:
    """Prevent server receipt time from preceding persisted transaction history."""
    evidence_times = [transaction.verified_at]
    if suggestion_created_at is not None:
        evidence_times.append(suggestion_created_at)
    audits = repository.list_audits_for_transaction(transaction.id)
    if audits:
        evidence_times.append(audits[-1].changed_at)
    if any(received_at < evidence_at for evidence_at in evidence_times):
        raise FinancialRoleServiceError(
            FinancialRoleServiceErrorCode.INVALID_REVIEW_CHRONOLOGY,
            "role decision was received before its existing verified evidence",
        )


def _normalise_text(value: str) -> str:
    return " ".join(_WORD_PATTERN.sub(" ", value.casefold()).split())


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_normalise_text(value).split())


def _has_transfer_language(value: str) -> bool:
    return bool(_tokens(value) & _TRANSFER_TERMS)


def _has_refund_language(value: str) -> bool:
    return bool(_tokens(value) & _REFUND_TERMS)


def _has_reimbursement_language(value: str) -> bool:
    normalised = _normalise_text(value)
    return any(phrase in normalised for phrase in _REIMBURSEMENT_PHRASES)


def _description_similarity(left: str, right: str) -> float:
    return SequenceMatcher(
        None,
        _normalise_text(left),
        _normalise_text(right),
        autojunk=False,
    ).ratio()


def _mentions_account(description: str, account_name: str) -> bool:
    normalised_name = _normalise_text(account_name)
    return len(normalised_name) >= 4 and normalised_name in _normalise_text(description)


def _transfer_match(
    outflow: _Candidate,
    inflow: _Candidate,
) -> _SuggestionDraft | None:
    outgoing = outflow.transaction
    incoming = inflow.transaction
    if outflow.account.user_profile_id != inflow.account.user_profile_id:
        return None
    if outgoing.account_id == incoming.account_id:
        return None
    if outgoing.currency != incoming.currency:
        return None
    if outgoing.amount >= 0 or incoming.amount <= 0:
        return None
    if outgoing.amount != -incoming.amount:
        return None
    date_distance = abs((outgoing.transaction_date - incoming.transaction_date).days)
    if date_distance > TRANSFER_DATE_WINDOW_DAYS:
        return None

    similarity = _description_similarity(outgoing.description, incoming.description)
    transfer_language = _has_transfer_language(
        f"{outgoing.description} {incoming.description}"
    )
    account_reference = _mentions_account(
        outgoing.description, inflow.account.name
    ) or _mentions_account(incoming.description, outflow.account.name)
    reasons = [
        RoleSuggestionReason.EXACT_OPPOSITE_AMOUNT,
        RoleSuggestionReason.CLOSE_DATE,
        RoleSuggestionReason.SAME_OWNER,
        RoleSuggestionReason.SAME_CURRENCY,
    ]
    confidence = 0.72 + max(0, TRANSFER_DATE_WINDOW_DAYS - date_distance) * 0.02
    if similarity >= 0.45:
        reasons.append(RoleSuggestionReason.DESCRIPTION_SIMILARITY)
        confidence += min(0.12, similarity * 0.12)
    if account_reference:
        reasons.append(RoleSuggestionReason.ACCOUNT_REFERENCE)
        confidence += 0.07
    if transfer_language:
        reasons.append(RoleSuggestionReason.TRANSFER_LANGUAGE)
        confidence += 0.07
    return _SuggestionDraft(
        subject=outflow,
        counterpart=inflow,
        kind=RoleSuggestionKind.TRANSFER,
        suggested_role=FinancialRole.TRANSFER_OUT,
        counterpart_role=FinancialRole.TRANSFER_IN,
        confidence=min(confidence, 0.99),
        reasons=tuple(reasons),
    )


def _single_row_suggestion(candidate: _Candidate) -> _SuggestionDraft | None:
    transaction = candidate.transaction
    if _has_transfer_language(transaction.description):
        role = (
            FinancialRole.TRANSFER_IN
            if transaction.amount > 0
            else FinancialRole.TRANSFER_OUT
        )
        return _SuggestionDraft(
            subject=candidate,
            counterpart=None,
            kind=RoleSuggestionKind.TRANSFER,
            suggested_role=role,
            counterpart_role=None,
            confidence=0.55,
            reasons=(RoleSuggestionReason.TRANSFER_LANGUAGE,),
        )
    if transaction.amount > 0 and _has_refund_language(transaction.description):
        return _SuggestionDraft(
            subject=candidate,
            counterpart=None,
            kind=RoleSuggestionKind.REFUND,
            suggested_role=FinancialRole.REFUND,
            counterpart_role=None,
            confidence=0.80,
            reasons=(RoleSuggestionReason.REFUND_LANGUAGE,),
        )
    if transaction.amount > 0 and _has_reimbursement_language(transaction.description):
        return _SuggestionDraft(
            subject=candidate,
            counterpart=None,
            kind=RoleSuggestionKind.REIMBURSEMENT,
            suggested_role=FinancialRole.REIMBURSEMENT,
            counterpart_role=None,
            confidence=0.80,
            reasons=(RoleSuggestionReason.REIMBURSEMENT_LANGUAGE,),
        )
    return None


def _suggestion_key(draft: _SuggestionDraft) -> str:
    counterpart_id = (
        draft.counterpart.transaction.id if draft.counterpart is not None else "-"
    )
    value = "|".join(
        (
            SUGGESTION_ALGORITHM_VERSION,
            draft.kind.value,
            draft.subject.transaction.id,
            counterpart_id,
            draft.suggested_role.value,
        )
    )
    return sha256(value.encode()).hexdigest()


def _suggestion_contract(
    record: FinancialRoleSuggestionRecord,
) -> FinancialRoleSuggestion:
    return FinancialRoleSuggestion(
        suggestion_id=record.id,
        transaction_id=record.verified_transaction_id,
        counterpart_transaction_id=record.counterpart_transaction_id,
        kind=RoleSuggestionKind(record.kind),
        suggested_role=FinancialRole(record.suggested_role_id),
        counterpart_role=(
            FinancialRole(record.counterpart_role_id)
            if record.counterpart_role_id is not None
            else None
        ),
        confidence=float(record.confidence),
        reasons=tuple(
            RoleSuggestionReason(reason) for reason in record.reason_codes_json
        ),
        algorithm_version=record.algorithm_version,
        status=RoleSuggestionStatus(record.status),
        created_at=record.created_at,
        reviewed_at=record.reviewed_at,
    )


def _audit_contract(record: FinancialRoleAuditRecord) -> FinancialRoleAudit:
    return FinancialRoleAudit(
        audit_id=record.id,
        transaction_id=record.verified_transaction_id,
        previous_role=FinancialRole(record.previous_role_id),
        new_role=FinancialRole(record.new_role_id),
        suggestion_id=record.suggestion_id,
        source=RoleDecisionSource(record.source),
        changed_at=record.changed_at,
    )


def _store_suggestion(
    repository: FinancialRoleRepository,
    draft: _SuggestionDraft,
    *,
    generated_at: datetime,
) -> FinancialRoleSuggestionRecord:
    key = _suggestion_key(draft)
    existing = repository.get_suggestion_by_key(key)
    if existing is not None:
        return existing
    return repository.add_suggestion(
        FinancialRoleSuggestionRecord(
            suggestion_key=key,
            verified_transaction_id=draft.subject.transaction.id,
            counterpart_transaction_id=(
                draft.counterpart.transaction.id
                if draft.counterpart is not None
                else None
            ),
            kind=draft.kind.value,
            suggested_role_id=draft.suggested_role.value,
            counterpart_role_id=(
                draft.counterpart_role.value
                if draft.counterpart_role is not None
                else None
            ),
            confidence=Decimal(f"{draft.confidence:.4f}"),
            reason_codes_json=[reason.value for reason in draft.reasons],
            algorithm_version=SUGGESTION_ALGORITHM_VERSION,
            status=RoleSuggestionStatus.PENDING.value,
            created_at=generated_at,
            reviewed_at=None,
        )
    )


def generate_financial_role_suggestions(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
    generated_at: datetime,
) -> tuple[FinancialRoleSuggestion, ...]:
    """Persist deterministic advisory suggestions without changing roles."""
    _require_aware(generated_at)
    with session_scope(factory) as session:
        if UserProfileRepository(session).get(user_profile_id) is None:
            raise FinancialRoleServiceError(
                FinancialRoleServiceErrorCode.PROFILE_NOT_FOUND,
                "local user profile does not exist",
            )
        repository = FinancialRoleRepository(session)
        candidates = tuple(
            _Candidate(transaction=transaction, account=account)
            for transaction, account in repository.list_unknown_candidates_for_user(
                user_profile_id
            )
        )
        outflows = tuple(item for item in candidates if item.transaction.amount < 0)
        inflows = tuple(item for item in candidates if item.transaction.amount > 0)
        paired_ids: set[str] = set()
        drafts: list[_SuggestionDraft] = []
        for outflow in outflows:
            matches = tuple(
                match
                for inflow in inflows
                if inflow.transaction.id not in paired_ids
                and (match := _transfer_match(outflow, inflow)) is not None
            )
            if not matches:
                continue
            best = max(
                matches,
                key=lambda item: (
                    item.confidence,
                    item.counterpart.transaction.id if item.counterpart else "",
                ),
            )
            drafts.append(best)
            paired_ids.add(outflow.transaction.id)
            paired_ids.add(cast(_Candidate, best.counterpart).transaction.id)

        for candidate in candidates:
            if candidate.transaction.id in paired_ids:
                continue
            single = _single_row_suggestion(candidate)
            if single is not None:
                drafts.append(single)

        records = tuple(
            _store_suggestion(repository, draft, generated_at=generated_at)
            for draft in drafts
        )
        return tuple(_suggestion_contract(record) for record in records)


def list_financial_role_review_queue(
    factory: sessionmaker[Session],
    *,
    user_profile_id: str,
) -> tuple[RoleReviewItem, ...]:
    """Return pending suggestions with notes and flags as inert reference data."""
    with session_scope(factory) as session:
        rows = FinancialRoleRepository(session).list_pending_for_user(user_profile_id)
        return tuple(
            RoleReviewItem(
                suggestion=_suggestion_contract(suggestion),
                account_id=transaction.account_id,
                transaction_date=transaction.transaction_date,
                description=transaction.description,
                amount=transaction.amount,
                current_role=FinancialRole(transaction.financial_role_id),
                statement_flags=(
                    tuple(context.flags_json) if context is not None else ()
                ),
                statement_note=context.note if context is not None else None,
            )
            for suggestion, transaction, context in rows
        )


def _require_pending(
    record: FinancialRoleSuggestionRecord | None,
) -> FinancialRoleSuggestionRecord:
    if record is None:
        raise FinancialRoleServiceError(
            FinancialRoleServiceErrorCode.SUGGESTION_NOT_FOUND,
            "financial-role suggestion does not exist",
        )
    if record.status != RoleSuggestionStatus.PENDING.value:
        raise FinancialRoleServiceError(
            FinancialRoleServiceErrorCode.SUGGESTION_ALREADY_REVIEWED,
            "financial-role suggestion has already been reviewed",
        )
    return record


def _require_transaction(
    repository: FinancialRoleRepository,
    transaction_id: str,
) -> VerifiedTransactionRecord:
    transaction = repository.get_transaction(transaction_id)
    if transaction is None:
        raise FinancialRoleServiceError(
            FinancialRoleServiceErrorCode.TRANSACTION_NOT_FOUND,
            "verified transaction does not exist",
        )
    return transaction


def _ensure_role_sign(
    transaction: VerifiedTransactionRecord,
    role: FinancialRole,
) -> None:
    positive_roles = {
        FinancialRole.INCOME,
        FinancialRole.TRANSFER_IN,
        FinancialRole.REFUND,
        FinancialRole.REIMBURSEMENT,
    }
    negative_roles = {
        FinancialRole.EXPENSE,
        FinancialRole.TRANSFER_OUT,
        FinancialRole.CASH_WITHDRAWAL,
    }
    incompatible = (transaction.amount > 0 and role in negative_roles) or (
        transaction.amount < 0 and role in positive_roles
    )
    if incompatible:
        raise FinancialRoleServiceError(
            FinancialRoleServiceErrorCode.SIGN_INCOMPATIBLE_ROLE,
            "selected financial role is incompatible with transaction direction",
        )


def _assign_role(
    repository: FinancialRoleRepository,
    transaction: VerifiedTransactionRecord,
    role: FinancialRole,
    *,
    changed_at: datetime,
    source: RoleDecisionSource,
    suggestion_id: str | None,
) -> RoleAssignment | None:
    previous = FinancialRole(transaction.financial_role_id)
    _ensure_role_sign(transaction, role)
    if previous is role:
        return None
    transaction.financial_role_id = role.value
    repository.add_audit(
        FinancialRoleAuditRecord(
            verified_transaction_id=transaction.id,
            previous_role_id=previous.value,
            new_role_id=role.value,
            suggestion_id=suggestion_id,
            source=source.value,
            changed_at=changed_at,
        )
    )
    return RoleAssignment(
        transaction_id=transaction.id,
        previous_role=previous,
        new_role=role,
    )


def confirm_financial_role_suggestion(
    factory: sessionmaker[Session],
    *,
    suggestion_id: str,
    reviewed_at: datetime,
) -> RoleDecisionResult:
    """Atomically apply a pending suggestion after explicit user confirmation."""
    received_at = _authoritative_review_time(reviewed_at)
    with session_scope(factory) as session:
        repository = FinancialRoleRepository(session)
        suggestion = _require_pending(repository.get_suggestion(suggestion_id))
        subject = _require_transaction(repository, suggestion.verified_transaction_id)
        counterpart = (
            _require_transaction(repository, suggestion.counterpart_transaction_id)
            if suggestion.counterpart_transaction_id is not None
            else None
        )
        _require_receipt_after_evidence(
            repository,
            subject,
            received_at=received_at,
            suggestion_created_at=suggestion.created_at,
        )
        if counterpart is not None:
            _require_receipt_after_evidence(
                repository,
                counterpart,
                received_at=received_at,
                suggestion_created_at=suggestion.created_at,
            )
        if subject.financial_role_id != FinancialRole.UNKNOWN.value:
            raise FinancialRoleServiceError(
                FinancialRoleServiceErrorCode.STALE_SUGGESTION,
                "transaction role changed after the suggestion was created",
            )
        assignments = [
            cast(
                RoleAssignment,
                _assign_role(
                    repository,
                    subject,
                    FinancialRole(suggestion.suggested_role_id),
                    changed_at=received_at,
                    source=RoleDecisionSource.USER_CONFIRMATION,
                    suggestion_id=suggestion.id,
                ),
            )
        ]

        transaction_ids = [subject.id]
        if counterpart is not None:
            if counterpart.financial_role_id != FinancialRole.UNKNOWN.value:
                raise FinancialRoleServiceError(
                    FinancialRoleServiceErrorCode.STALE_SUGGESTION,
                    "counterpart role changed after the suggestion was created",
                )
            counterpart_role = suggestion.counterpart_role_id
            assignments.append(
                cast(
                    RoleAssignment,
                    _assign_role(
                        repository,
                        counterpart,
                        FinancialRole(cast(str, counterpart_role)),
                        changed_at=received_at,
                        source=RoleDecisionSource.USER_CONFIRMATION,
                        suggestion_id=suggestion.id,
                    ),
                )
            )
            transaction_ids.append(counterpart.id)

        suggestion.status = RoleSuggestionStatus.CONFIRMED.value
        suggestion.reviewed_at = received_at
        repository.reject_pending_for_transactions(
            tuple(transaction_ids),
            reviewed_at=received_at,
            except_suggestion_id=suggestion.id,
        )
        change_type = (
            SourceDataChangeType.TRANSFER_CONFIRMED
            if any(
                item.new_role in {FinancialRole.TRANSFER_IN, FinancialRole.TRANSFER_OUT}
                for item in assignments
            )
            else SourceDataChangeType.FINANCIAL_ROLE_CHANGED
        )
        account_ids = {subject.account_id}
        if counterpart is not None:
            account_ids.add(counterpart.account_id)
        for account_id in sorted(account_ids):
            invalidate_derived_results_in_session(
                session,
                account_id=account_id,
                change_type=change_type,
                changed_at=received_at,
            )
        return RoleDecisionResult(
            suggestion_id=suggestion.id,
            suggestion_status=RoleSuggestionStatus.CONFIRMED,
            assignments=tuple(assignments),
        )


def reject_financial_role_suggestion(
    factory: sessionmaker[Session],
    *,
    suggestion_id: str,
    reviewed_at: datetime,
) -> RoleDecisionResult:
    """Reject a pending suggestion without changing any transaction role."""
    received_at = _authoritative_review_time(reviewed_at)
    with session_scope(factory) as session:
        repository = FinancialRoleRepository(session)
        suggestion = _require_pending(repository.get_suggestion(suggestion_id))
        subject = _require_transaction(repository, suggestion.verified_transaction_id)
        _require_receipt_after_evidence(
            repository,
            subject,
            received_at=received_at,
            suggestion_created_at=suggestion.created_at,
        )
        if suggestion.counterpart_transaction_id is not None:
            counterpart = _require_transaction(
                repository, suggestion.counterpart_transaction_id
            )
            _require_receipt_after_evidence(
                repository,
                counterpart,
                received_at=received_at,
                suggestion_created_at=suggestion.created_at,
            )
        suggestion.status = RoleSuggestionStatus.REJECTED.value
        suggestion.reviewed_at = received_at
        return RoleDecisionResult(
            suggestion_id=suggestion.id,
            suggestion_status=RoleSuggestionStatus.REJECTED,
        )


def _role_for_action(
    action: TransactionReviewAction,
    transaction: VerifiedTransactionRecord,
) -> FinancialRole:
    mapping = {
        TransactionReviewAction.INCOME: FinancialRole.INCOME,
        TransactionReviewAction.EXPENSE: FinancialRole.EXPENSE,
        TransactionReviewAction.REFUND: FinancialRole.REFUND,
        TransactionReviewAction.REIMBURSEMENT: FinancialRole.REIMBURSEMENT,
        TransactionReviewAction.CASH_WITHDRAWAL: FinancialRole.CASH_WITHDRAWAL,
        TransactionReviewAction.IGNORE_FROM_ANALYTICS: FinancialRole.EXCLUDED,
    }
    if action is TransactionReviewAction.INTERNAL_TRANSFER:
        return (
            FinancialRole.TRANSFER_IN
            if transaction.amount > 0
            else FinancialRole.TRANSFER_OUT
        )
    return mapping[action]


def apply_transaction_review_action(
    factory: sessionmaker[Session],
    *,
    transaction_id: str,
    action: TransactionReviewAction,
    changed_at: datetime,
) -> RoleDecisionResult:
    """Apply one explicit role override or structured needs-review flag."""
    received_at = _authoritative_review_time(changed_at)
    with session_scope(factory) as session:
        repository = FinancialRoleRepository(session)
        transaction = _require_transaction(repository, transaction_id)
        _require_receipt_after_evidence(
            repository,
            transaction,
            received_at=received_at,
        )
        if action is TransactionReviewAction.NEEDS_REVIEW:
            added = repository.add_flag_once(
                transaction.id,
                flag="needs_review",
                created_at=received_at,
            )
            return RoleDecisionResult(needs_review_flagged=added)

        role = _role_for_action(action, transaction)
        assignment = _assign_role(
            repository,
            transaction,
            role,
            changed_at=received_at,
            source=RoleDecisionSource.USER_OVERRIDE,
            suggestion_id=None,
        )
        repository.reject_pending_for_transactions(
            (transaction.id,), reviewed_at=received_at
        )
        if assignment is not None:
            change_type = (
                SourceDataChangeType.TRANSFER_CONFIRMED
                if assignment.new_role
                in {FinancialRole.TRANSFER_IN, FinancialRole.TRANSFER_OUT}
                else SourceDataChangeType.FINANCIAL_ROLE_CHANGED
            )
            invalidate_derived_results_in_session(
                session,
                account_id=transaction.account_id,
                change_type=change_type,
                changed_at=received_at,
            )
        return RoleDecisionResult(
            assignments=(assignment,) if assignment is not None else ()
        )


def list_financial_role_audits(
    factory: sessionmaker[Session],
    *,
    transaction_id: str,
) -> tuple[FinancialRoleAudit, ...]:
    """Return immutable role history without exposing raw source rows."""
    with session_scope(factory) as session:
        records = FinancialRoleRepository(session).list_audits_for_transaction(
            transaction_id
        )
        return tuple(_audit_contract(record) for record in records)
