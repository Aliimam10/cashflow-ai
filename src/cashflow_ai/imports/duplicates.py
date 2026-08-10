"""Conservative repeated-file, transaction-duplicate, and overlap detection."""

from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher

from cashflow_ai.schemas.duplicates import (
    DuplicateAction,
    DuplicateAssessment,
    DuplicateFacts,
    DuplicateReason,
    DuplicateStatus,
    RepeatedFileAssessment,
    StatementOverlapAssessment,
    StatementOverlapStatus,
    StatementRecord,
)
from cashflow_ai.schemas.normalisation import NormalisedTransaction
from cashflow_ai.schemas.statements import DateRange

PROBABLE_DUPLICATE_THRESHOLD = 0.75


def assess_repeated_file(
    file_hash: str,
    known_file_hashes: Iterable[str],
) -> RepeatedFileAssessment:
    """Flag an upload when its exact byte hash has already been imported."""
    return RepeatedFileAssessment(
        file_hash=file_hash,
        repeated=file_hash in set(known_file_hashes),
    )


def duplicate_facts_from_normalised(
    transaction: NormalisedTransaction,
) -> DuplicateFacts:
    """Project a normalised transaction onto the duplicate-matching contract."""
    draft = transaction.draft
    assert draft.account_id is not None
    assert draft.transaction_date is not None
    assert draft.amount is not None
    assert draft.description is not None
    return DuplicateFacts(
        source_fingerprint=transaction.source_fingerprint,
        canonical_fingerprint=transaction.canonical_fingerprint,
        account_id=draft.account_id,
        transaction_date=draft.transaction_date,
        amount=draft.amount,
        description=draft.description,
        merchant=draft.merchant,
        external_id=draft.external_id,
    )


def _matching_description(transaction: DuplicateFacts) -> str:
    value = transaction.merchant or transaction.description
    return " ".join(value.casefold().split())


def _unique_assessment(
    incoming: DuplicateFacts,
    existing: DuplicateFacts,
    reason: DuplicateReason,
) -> DuplicateAssessment:
    return DuplicateAssessment(
        incoming_source_fingerprint=incoming.source_fingerprint,
        existing_source_fingerprint=existing.source_fingerprint,
        status=DuplicateStatus.UNIQUE,
        action=DuplicateAction.KEEP,
        score=0,
        reasons=(reason,),
    )


def assess_duplicate_facts(
    incoming: DuplicateFacts,
    existing: DuplicateFacts,
) -> DuplicateAssessment:
    """Compare stored matching facts without auto-skipping ambiguous matches."""
    if incoming.source_fingerprint == existing.source_fingerprint:
        return DuplicateAssessment(
            incoming_source_fingerprint=incoming.source_fingerprint,
            existing_source_fingerprint=existing.source_fingerprint,
            status=DuplicateStatus.EXACT,
            action=DuplicateAction.SKIP,
            score=1,
            reasons=(DuplicateReason.SAME_SOURCE_RECORD,),
        )
    if incoming.account_id != existing.account_id:
        return _unique_assessment(
            incoming,
            existing,
            DuplicateReason.DIFFERENT_ACCOUNT,
        )

    incoming_external_id = incoming.external_id
    existing_external_id = existing.external_id
    if (
        incoming_external_id is not None
        and existing_external_id is not None
        and incoming_external_id.casefold() == existing_external_id.casefold()
    ):
        return DuplicateAssessment(
            incoming_source_fingerprint=incoming.source_fingerprint,
            existing_source_fingerprint=existing.source_fingerprint,
            status=DuplicateStatus.EXACT,
            action=DuplicateAction.SKIP,
            score=1,
            reasons=(DuplicateReason.SAME_EXTERNAL_ID,),
        )

    score = 0.0
    reasons: list[DuplicateReason] = []
    if incoming.amount == existing.amount:
        score += 0.4
        reasons.append(DuplicateReason.SAME_AMOUNT)

    date_distance = abs((incoming.transaction_date - existing.transaction_date).days)
    date_scores = {0: 0.25, 1: 0.2, 2: 0.1}
    if date_distance in date_scores:
        score += date_scores[date_distance]
        reasons.append(DuplicateReason.CLOSE_DATE)

    description_similarity = SequenceMatcher(
        None,
        _matching_description(incoming),
        _matching_description(existing),
        autojunk=False,
    ).ratio()
    if description_similarity >= 0.6:
        score += 0.25 * description_similarity
        reasons.append(DuplicateReason.SIMILAR_DESCRIPTION)
    if incoming.canonical_fingerprint == existing.canonical_fingerprint:
        reasons.append(DuplicateReason.SAME_CANONICAL_FINGERPRINT)

    if (
        incoming_external_id is not None
        and existing_external_id is not None
        and incoming_external_id.casefold() != existing_external_id.casefold()
    ):
        score = min(score, 0.7)
        reasons.append(DuplicateReason.DIFFERENT_EXTERNAL_ID)

    score = round(min(score, 1), 3)
    if not reasons:
        reasons.append(DuplicateReason.INSUFFICIENT_MATCH)
    status = (
        DuplicateStatus.PROBABLE
        if score >= PROBABLE_DUPLICATE_THRESHOLD
        else DuplicateStatus.UNIQUE
    )
    action = (
        DuplicateAction.REVIEW
        if status is DuplicateStatus.PROBABLE
        else DuplicateAction.KEEP
    )
    return DuplicateAssessment(
        incoming_source_fingerprint=incoming.source_fingerprint,
        existing_source_fingerprint=existing.source_fingerprint,
        status=status,
        action=action,
        score=score,
        reasons=tuple(reasons),
    )


def assess_transaction_duplicate(
    incoming: NormalisedTransaction,
    existing: NormalisedTransaction,
) -> DuplicateAssessment:
    """Compare two normalised transactions through their stable match facts."""
    return assess_duplicate_facts(
        duplicate_facts_from_normalised(incoming),
        duplicate_facts_from_normalised(existing),
    )


def find_duplicate_assessments(
    incoming: NormalisedTransaction,
    existing_transactions: Iterable[NormalisedTransaction],
) -> tuple[DuplicateAssessment, ...]:
    """Return only exact or probable matches from an existing collection."""
    assessments = (
        assess_transaction_duplicate(incoming, existing)
        for existing in existing_transactions
    )
    return tuple(
        assessment
        for assessment in assessments
        if assessment.status is not DuplicateStatus.UNIQUE
    )


def assess_statement_overlap(
    incoming: StatementRecord,
    existing: StatementRecord,
) -> StatementOverlapAssessment:
    """Measure inclusive date overlap for statements belonging to one account."""
    if incoming.account_id != existing.account_id:
        return StatementOverlapAssessment(
            incoming_document_hash=incoming.source_document_hash,
            existing_document_hash=existing.source_document_hash,
            status=StatementOverlapStatus.NONE,
            overlap_days=0,
        )

    overlap_start = max(
        incoming.coverage.statement_start_date,
        existing.coverage.statement_start_date,
    )
    overlap_end = min(
        incoming.coverage.statement_end_date,
        existing.coverage.statement_end_date,
    )
    if overlap_end < overlap_start:
        return StatementOverlapAssessment(
            incoming_document_hash=incoming.source_document_hash,
            existing_document_hash=existing.source_document_hash,
            status=StatementOverlapStatus.NONE,
            overlap_days=0,
        )

    is_exact = (
        incoming.coverage.statement_start_date == existing.coverage.statement_start_date
        and incoming.coverage.statement_end_date == existing.coverage.statement_end_date
    )
    return StatementOverlapAssessment(
        incoming_document_hash=incoming.source_document_hash,
        existing_document_hash=existing.source_document_hash,
        status=(
            StatementOverlapStatus.EXACT if is_exact else StatementOverlapStatus.PARTIAL
        ),
        overlap_range=DateRange(start_date=overlap_start, end_date=overlap_end),
        overlap_days=(overlap_end - overlap_start).days + 1,
    )
