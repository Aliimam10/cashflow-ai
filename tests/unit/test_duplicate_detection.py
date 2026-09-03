"""Tests for conservative duplicate and statement-overlap detection."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cashflow_ai.imports import (
    assess_repeated_file,
    assess_statement_overlap,
    assess_transaction_duplicate,
    calculate_file_hash,
    find_duplicate_assessments,
    normalise_transaction,
)
from cashflow_ai.schemas import (
    CoverageStatus,
    Currency,
    DateRange,
    DuplicateAction,
    DuplicateAssessment,
    DuplicateReason,
    DuplicateReviewDecision,
    DuplicateReviewRequest,
    DuplicateReviewResult,
    DuplicateStatus,
    DuplicateTransactionSummary,
    NormalisedTransaction,
    OriginalTransactionValues,
    SourceFieldValue,
    SourceRecordIdentity,
    SourceType,
    StatementCoverage,
    StatementOverlapAssessment,
    StatementOverlapStatus,
    StatementRecord,
    TransactionDraft,
)
from cashflow_ai.schemas.duplicates import ProbableDuplicateReviewItem
from cashflow_ai.schemas.imports import ReviewStatus, VerificationStatus
from cashflow_ai.schemas.transactions import Direction

HASH_A = "a" * 64
HASH_B = "b" * 64


def transaction(
    *,
    transaction_date: str = "2026-07-04",
    description: str = "Example Shop",
    amount: str = "-12.50",
    account_id: str = "account-1",
    external_id: str | None = None,
    source_hash: str = HASH_A,
    row_number: int = 2,
    source_type: SourceType = SourceType.CSV,
) -> NormalisedTransaction:
    original = OriginalTransactionValues(
        transaction_date_text=transaction_date,
        description_text=description,
        signed_amount_text=amount,
        external_id_text=external_id,
        raw_fields=(
            SourceFieldValue(column="Date", value=transaction_date),
            SourceFieldValue(column="Description", value=description),
            SourceFieldValue(column="Amount", value=amount),
        ),
    )
    identity_values: dict[str, object] = {
        "source_type": source_type,
        "source_document_hash": source_hash,
    }
    if source_type is SourceType.CSV:
        identity_values["source_row_number"] = row_number
    else:
        identity_values.update(page_number=1, page_record_number=row_number)
    return normalise_transaction(
        original,
        account_id=account_id,
        account_currency=Currency.GBP,
        source_identity=SourceRecordIdentity.model_validate(identity_values),
    )


def statement(
    source_hash: str,
    start: date,
    end: date,
    *,
    account_id: str = "account-1",
) -> StatementRecord:
    return StatementRecord(
        source_document_hash=source_hash,
        account_id=account_id,
        coverage=StatementCoverage(
            statement_start_date=start,
            statement_end_date=end,
            status=CoverageStatus.COMPLETE,
        ),
    )


def test_same_source_row_is_the_only_unconditional_exact_duplicate() -> None:
    incoming = transaction()
    existing = transaction()

    assessment = assess_transaction_duplicate(incoming, existing)

    assert assessment.status is DuplicateStatus.EXACT
    assert assessment.action is DuplicateAction.SKIP
    assert assessment.score == 1
    assert assessment.reasons == (DuplicateReason.SAME_SOURCE_RECORD,)


def test_same_account_external_id_is_an_exact_duplicate_across_files() -> None:
    incoming = transaction(external_id="bank-id-1", source_hash=HASH_A)
    existing = transaction(external_id="BANK-ID-1", source_hash=HASH_B)

    assessment = assess_transaction_duplicate(incoming, existing)

    assert assessment.status is DuplicateStatus.EXACT
    assert assessment.action is DuplicateAction.SKIP
    assert assessment.reasons == (DuplicateReason.SAME_EXTERNAL_ID,)


def test_transactions_from_different_accounts_are_not_duplicates() -> None:
    assessment = assess_transaction_duplicate(
        transaction(account_id="account-1"),
        transaction(account_id="account-2", source_hash=HASH_B),
    )

    assert assessment.status is DuplicateStatus.UNIQUE
    assert assessment.action is DuplicateAction.KEEP
    assert assessment.reasons == (DuplicateReason.DIFFERENT_ACCOUNT,)


def test_csv_and_pdf_versions_of_the_same_transaction_require_review() -> None:
    csv_transaction = transaction(source_hash=HASH_A)
    pdf_transaction = transaction(
        source_hash=HASH_B,
        source_type=SourceType.DIGITAL_PDF,
    )

    assessment = assess_transaction_duplicate(csv_transaction, pdf_transaction)

    assert assessment.status is DuplicateStatus.PROBABLE
    assert assessment.action is DuplicateAction.REVIEW
    assert assessment.score == 0.9
    assert DuplicateReason.SAME_CANONICAL_FINGERPRINT in assessment.reasons


@pytest.mark.parametrize(
    ("other_date", "expected_score", "expected_status"),
    [
        ("2026-07-05", 0.85, DuplicateStatus.PROBABLE),
        ("2026-07-06", 0.75, DuplicateStatus.PROBABLE),
        ("2026-07-07", 0.65, DuplicateStatus.UNIQUE),
    ],
)
def test_close_posting_dates_are_scored_without_becoming_exact(
    other_date: str,
    expected_score: float,
    expected_status: DuplicateStatus,
) -> None:
    assessment = assess_transaction_duplicate(
        transaction(source_hash=HASH_A),
        transaction(transaction_date=other_date, source_hash=HASH_B),
    )

    assert assessment.score == expected_score
    assert assessment.status is expected_status
    assert assessment.action is (
        DuplicateAction.REVIEW
        if expected_status is DuplicateStatus.PROBABLE
        else DuplicateAction.KEEP
    )


def test_legitimate_same_merchant_and_amount_with_distinct_ids_is_kept() -> None:
    first = transaction(external_id="purchase-1", source_hash=HASH_A)
    second = transaction(external_id="purchase-2", source_hash=HASH_B)

    assessment = assess_transaction_duplicate(first, second)

    assert assessment.status is DuplicateStatus.UNIQUE
    assert assessment.action is DuplicateAction.KEEP
    assert assessment.score == 0.7
    assert DuplicateReason.DIFFERENT_EXTERNAL_ID in assessment.reasons


def test_unrelated_transactions_have_insufficient_evidence() -> None:
    assessment = assess_transaction_duplicate(
        transaction(source_hash=HASH_A),
        transaction(
            transaction_date="2026-08-10",
            description="Completely Different",
            amount="-99.99",
            source_hash=HASH_B,
        ),
    )

    assert assessment.status is DuplicateStatus.UNIQUE
    assert assessment.score == 0
    assert assessment.reasons == (DuplicateReason.INSUFFICIENT_MATCH,)


def test_find_duplicate_assessments_filters_unique_comparisons() -> None:
    incoming = transaction(source_hash=HASH_A)
    probable = transaction(source_hash=HASH_B, source_type=SourceType.OCR_PDF)
    unrelated = transaction(
        transaction_date="2026-08-10",
        description="Different",
        amount="-99.99",
        source_hash="c" * 64,
    )

    matches = find_duplicate_assessments(incoming, [probable, unrelated])

    assert len(matches) == 1
    assert matches[0].status is DuplicateStatus.PROBABLE


def test_exact_file_hash_prevents_repeated_file_import() -> None:
    file_hash = calculate_file_hash(b"same fictional statement bytes")

    first = assess_repeated_file(file_hash, [])
    repeated = assess_repeated_file(file_hash, [HASH_A, file_hash])

    assert first.repeated is False
    assert repeated.repeated is True
    assert repeated.file_hash == file_hash


def test_statement_overlap_detects_partial_and_exact_ranges() -> None:
    july = statement(HASH_A, date(2026, 7, 1), date(2026, 7, 31))
    overlap = statement(HASH_B, date(2026, 7, 20), date(2026, 8, 19))
    exact = statement("c" * 64, date(2026, 7, 1), date(2026, 7, 31))

    partial_assessment = assess_statement_overlap(july, overlap)
    exact_assessment = assess_statement_overlap(july, exact)

    assert partial_assessment.status is StatementOverlapStatus.PARTIAL
    assert partial_assessment.overlap_range == DateRange(
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 31),
    )
    assert partial_assessment.overlap_days == 12
    assert exact_assessment.status is StatementOverlapStatus.EXACT
    assert exact_assessment.overlap_days == 31


@pytest.mark.parametrize(
    "other",
    [
        statement(HASH_B, date(2026, 8, 1), date(2026, 8, 31)),
        statement(
            HASH_B,
            date(2026, 7, 1),
            date(2026, 7, 31),
            account_id="account-2",
        ),
    ],
)
def test_non_overlapping_or_different_account_statements_are_clear(
    other: StatementRecord,
) -> None:
    july = statement(HASH_A, date(2026, 7, 1), date(2026, 7, 31))

    assessment = assess_statement_overlap(july, other)

    assert assessment.status is StatementOverlapStatus.NONE
    assert assessment.overlap_range is None
    assert assessment.overlap_days == 0


def test_duplicate_contract_forbids_unsafe_action_status_combinations() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        DuplicateAssessment(
            incoming_source_fingerprint=HASH_A,
            existing_source_fingerprint=HASH_B,
            status=DuplicateStatus.PROBABLE,
            action=DuplicateAction.SKIP,
            score=0.9,
            reasons=(DuplicateReason.SAME_AMOUNT,),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": StatementOverlapStatus.NONE,
            "overlap_range": DateRange(
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 2),
            ),
            "overlap_days": 2,
        },
        {
            "status": StatementOverlapStatus.PARTIAL,
            "overlap_days": 0,
        },
    ],
)
def test_overlap_contract_requires_coherent_range_and_duration(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        StatementOverlapAssessment.model_validate(
            {
                "incoming_document_hash": HASH_A,
                "existing_document_hash": HASH_B,
                **payload,
            }
        )


def test_duplicate_review_contracts_require_complete_coherent_decisions() -> None:
    transaction = DuplicateTransactionSummary(
        account_id="account-1",
        transaction_date=date(2026, 8, 1),
        description="Synthetic shop",
        amount=Decimal("-1.00"),
        currency=Currency.GBP,
    )
    with pytest.raises(ValidationError, match="keep readiness"):
        ProbableDuplicateReviewItem(
            raw_transaction_id="raw-1",
            import_batch_id="batch-1",
            account_id="account-1",
            original_date_text="2026-08-01",
            original_description="Synthetic shop",
            original_amount_text="-1.00",
            candidate=transaction,
            existing_transaction=transaction,
            score=0.9,
            reasons=(DuplicateReason.SAME_AMOUNT,),
            can_keep=False,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        DuplicateReviewRequest(
            decision=DuplicateReviewDecision.REJECT,
            decided_at=datetime(2026, 8, 2),
        )
    valid = DuplicateReviewResult(
        raw_transaction_id="raw-1",
        decision=DuplicateReviewDecision.KEEP,
        review_status=ReviewStatus.CONFIRMED,
        kept_transaction_id="transaction-1",
        import_verification_status=VerificationStatus.VERIFIED,
    )
    assert valid.kept_transaction_id == "transaction-1"
    for payload in (
        {
            "decision": DuplicateReviewDecision.KEEP,
            "review_status": ReviewStatus.CONFIRMED,
            "kept_transaction_id": None,
        },
        {
            "decision": DuplicateReviewDecision.REJECT,
            "review_status": ReviewStatus.CONFIRMED,
            "kept_transaction_id": None,
        },
    ):
        with pytest.raises(ValidationError):
            DuplicateReviewResult.model_validate(
                {
                    "raw_transaction_id": "raw-1",
                    "import_verification_status": VerificationStatus.VERIFIED,
                    **payload,
                }
            )


def test_duplicate_candidate_snapshot_retains_only_a_canonical_draft() -> None:
    request = DuplicateReviewRequest(
        decision=DuplicateReviewDecision.REJECT,
        decided_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    draft = TransactionDraft(
        transaction_date=date(2026, 8, 1),
        description="Synthetic shop",
        amount=Decimal("-1.00"),
        currency=Currency.GBP,
        account_id="account-1",
        direction=Direction.OUTFLOW,
    )

    assert request.decision is DuplicateReviewDecision.REJECT
    assert draft.description == "Synthetic shop"
