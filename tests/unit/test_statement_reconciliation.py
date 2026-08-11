"""Tests for statement arithmetic and the explicit PDF review boundary."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cashflow_ai.imports import (
    StatementReviewError,
    StatementReviewErrorCode,
    approve_statement_review,
    prepare_statement_review,
    reconcile_statement,
)
from cashflow_ai.schemas import (
    AmountSignConvention,
    CoverageStatus,
    Currency,
    DateFormat,
    DateRange,
    Direction,
    ExtractionMethod,
    ExtractionProvenance,
    FieldConfidence,
    ImportIssue,
    IssueSeverity,
    OcrLineExtraction,
    OcrPageExtraction,
    OcrPdfPreview,
    OcrTransactionCandidate,
    OriginalTransactionValues,
    ParserIdentity,
    PdfExtractionLayout,
    PdfPageExtraction,
    PdfTransactionCandidate,
    ReconciliationStatus,
    ReviewReason,
    RowDecision,
    RowReview,
    SourceFieldValue,
    SourceRecordIdentity,
    SourceType,
    StatementApproval,
    StatementBalanceEvidence,
    StatementBalanceField,
    StatementBalances,
    StatementCoverage,
    StatementReconciliation,
    StatementReview,
    StatementReviewRow,
    TextPdfPreview,
    TransactionDraft,
    TransactionField,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
APPROVED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
PARSER = ParserIdentity(name="synthetic_review_parser", version="1.0")
SYNTHETIC_COVERAGE = StatementCoverage(
    statement_start_date=date(2026, 7, 1),
    statement_end_date=date(2026, 7, 31),
    status=CoverageStatus.UNKNOWN,
)


def _original(
    *,
    date_text: str = "2026-07-01",
    amount_text: str | None = "-10.00",
    debit_text: str | None = None,
    credit_text: str | None = None,
) -> OriginalTransactionValues:
    return OriginalTransactionValues(
        transaction_date_text=date_text,
        description_text="SYNTHETIC ITEM",
        signed_amount_text=amount_text,
        debit_amount_text=debit_text,
        credit_amount_text=credit_text,
        raw_fields=(
            SourceFieldValue(column="Date", value=date_text),
            SourceFieldValue(column="Description", value="SYNTHETIC ITEM"),
            SourceFieldValue(
                column="Amount",
                value=amount_text or debit_text or credit_text or "",
            ),
        ),
    )


def _draft(amount: Decimal | None = Decimal("-10.00")) -> TransactionDraft:
    return TransactionDraft(
        transaction_date=date(2026, 7, 1) if amount is not None else None,
        description="SYNTHETIC ITEM" if amount is not None else None,
        amount=amount,
        currency=Currency.GBP,
        account_id="account-1",
        direction=(Direction.OUTFLOW if amount is not None else None),
    )


def _pdf_candidate(
    *,
    fingerprint: str = HASH_B,
    original: OriginalTransactionValues | None = None,
    draft: TransactionDraft | None = None,
    issue: ImportIssue | None = None,
) -> PdfTransactionCandidate:
    current_draft = draft or _draft()
    return PdfTransactionCandidate(
        original=original or _original(),
        draft=current_draft,
        source_identity=SourceRecordIdentity(
            source_type=SourceType.DIGITAL_PDF,
            source_document_hash=HASH_A,
            page_number=1,
            page_record_number=1 if fingerprint == HASH_B else 2,
        ),
        source_fingerprint=fingerprint,
        canonical_fingerprint=(None if issue is not None else HASH_C),
        provenance=ExtractionProvenance(
            source_type=SourceType.DIGITAL_PDF,
            method=ExtractionMethod.PDF_TEXT,
            page_number=1,
            parser=PARSER,
        ),
        issues=(() if issue is None else (issue,)),
    )


def _text_preview(
    *candidates: PdfTransactionCandidate,
    balances: StatementBalances | None = None,
) -> TextPdfPreview:
    page_lines = ["Synthetic statement content"]
    if balances is not None and balances.opening_balance is not None:
        page_lines.append(f"Opening balance: {balances.opening_balance}")
    if balances is not None and balances.closing_balance is not None:
        page_lines.append(f"Closing balance: {balances.closing_balance}")
    return TextPdfPreview(
        source_filename="synthetic-review.pdf",
        byte_size=100,
        file_hash=HASH_A,
        page_count=1,
        pages=(
            PdfPageExtraction(
                page_number=1,
                raw_text="\n".join(page_lines),
                embedded_character_count=25,
                tables_found=0,
            ),
        ),
        layouts=frozenset({PdfExtractionLayout.GENERIC_TEXT}),
        statement_coverage=(SYNTHETIC_COVERAGE if balances is not None else None),
        statement_balances=balances,
        candidates=candidates,
    )


def _ocr_preview(confidence: float) -> OcrPdfPreview:
    original = _original()
    candidate = OcrTransactionCandidate(
        original=original,
        draft=_draft(),
        source_identity=SourceRecordIdentity(
            source_type=SourceType.OCR_PDF,
            source_document_hash=HASH_A,
            page_number=1,
            page_record_number=1,
        ),
        source_fingerprint=HASH_B,
        canonical_fingerprint=HASH_C,
        provenance=ExtractionProvenance(
            source_type=SourceType.OCR_PDF,
            method=ExtractionMethod.OCR,
            page_number=1,
            confidence=confidence,
            parser=PARSER,
        ),
        line_numbers=(2,),
        field_confidences=(
            FieldConfidence(
                field=TransactionField.AMOUNT,
                confidence=confidence,
                raw_value="-10.00",
            ),
        ),
    )
    opening_line = OcrLineExtraction(
        line_number=1,
        raw_text="Opening balance: 100.00",
        confidence=confidence,
        word_count=3,
    )
    transaction_line = OcrLineExtraction(
        line_number=2,
        raw_text="01/07/2026 SYNTHETIC ITEM -10.00",
        confidence=confidence,
        word_count=3,
    )
    closing_line = OcrLineExtraction(
        line_number=3,
        raw_text="Closing balance: 90.00",
        confidence=confidence,
        word_count=3,
    )
    return OcrPdfPreview(
        source_filename="synthetic-ocr.pdf",
        byte_size=100,
        file_hash=HASH_A,
        page_count=1,
        pages=(
            OcrPageExtraction(
                page_number=1,
                pixel_width=100,
                pixel_height=100,
                render_dpi=300,
                rotation_applied_degrees=0,
                threshold_applied=False,
                raw_text="\n".join(
                    (
                        opening_line.raw_text,
                        transaction_line.raw_text,
                        closing_line.raw_text,
                    )
                ),
                confidence=confidence,
                lines=(opening_line, transaction_line, closing_line),
            ),
        ),
        statement_coverage=SYNTHETIC_COVERAGE,
        statement_balances=StatementBalances(
            opening_balance=Decimal("100.00"),
            closing_balance=Decimal("90.00"),
        ),
        candidates=(candidate,),
    )


def _approval(
    **changes: object,
) -> StatementApproval:
    payload: dict[str, object] = {
        "file_hash": HASH_A,
        "approved_at": APPROVED_AT,
        "statement_approved": True,
    }
    payload.update(changes)
    if (
        payload.get("confirmed_balances") is not None
        and "confirmed_statement_coverage" not in changes
    ):
        payload["confirmed_statement_coverage"] = SYNTHETIC_COVERAGE
    return StatementApproval.model_validate(payload)


def test_reconciliation_reports_exact_tolerated_mismatch_and_unavailable() -> None:
    exact = reconcile_statement(
        StatementBalances(
            opening_balance=Decimal("100.00"),
            closing_balance=Decimal("90.00"),
        ),
        (Decimal("-10.00"),),
    )
    assert exact.status is ReconciliationStatus.RECONCILED
    assert exact.signed_transaction_total == Decimal("-10.00")
    assert exact.expected_closing_balance == Decimal("90.00")
    assert exact.unexplained_difference == Decimal("0.00")

    tolerated = reconcile_statement(
        StatementBalances(
            opening_balance=Decimal("100.00"),
            closing_balance=Decimal("90.02"),
        ),
        (Decimal("-10.00"),),
        tolerance=Decimal("0.02"),
    )
    assert tolerated.status is ReconciliationStatus.RECONCILED

    mismatch = reconcile_statement(
        StatementBalances(
            opening_balance=Decimal("100.00"),
            closing_balance=Decimal("90.02"),
        ),
        (Decimal("-10.00"),),
    )
    assert mismatch.status is ReconciliationStatus.MISMATCH
    assert mismatch.unexplained_difference == Decimal("0.02")

    unavailable = reconcile_statement(None, (Decimal("-1.00"), None))
    assert unavailable.status is ReconciliationStatus.UNAVAILABLE
    assert unavailable.signed_transaction_total == Decimal("-1.00")
    assert unavailable.unusable_transaction_count == 1

    with pytest.raises(ValueError, match="cannot be negative"):
        reconcile_statement(None, (), tolerance=Decimal("-0.01"))


def test_reconciliation_contract_rejects_inconsistent_status_fields() -> None:
    with pytest.raises(ValidationError, match="cannot claim balance arithmetic"):
        StatementReconciliation(
            status=ReconciliationStatus.UNAVAILABLE,
            opening_balance=Decimal("1.00"),
            signed_transaction_total=Decimal("0.00"),
            expected_closing_balance=None,
            closing_balance=None,
            unexplained_difference=None,
            unusable_transaction_count=0,
        )
    with pytest.raises(ValidationError, match="requires complete"):
        StatementReconciliation(
            status=ReconciliationStatus.RECONCILED,
            opening_balance=None,
            signed_transaction_total=Decimal("0.00"),
            expected_closing_balance=None,
            closing_balance=None,
            unexplained_difference=None,
            unusable_transaction_count=0,
        )


def test_prepare_review_targets_extraction_errors_and_confirmation_questions() -> None:
    extraction_issue = ImportIssue(
        code="invalid_date",
        message="Synthetic date requires correction",
        severity=IssueSeverity.ERROR,
        field=TransactionField.TRANSACTION_DATE,
    )
    ambiguous_debit = _original(
        date_text="01/02/2026",
        amount_text=None,
        debit_text="10.00",
    )
    invalid = _pdf_candidate(
        fingerprint=HASH_C,
        draft=_draft(None),
        issue=extraction_issue,
    )
    review = prepare_statement_review(
        _text_preview(
            _pdf_candidate(original=ambiguous_debit),
            invalid,
            balances=StatementBalances(
                opening_balance=Decimal("100.00"),
                closing_balance=Decimal("90.00"),
            ),
        )
    )

    assert review.source_type is SourceType.DIGITAL_PDF
    assert review.requires_date_format_confirmation is True
    assert review.requires_debit_credit_sign_confirmation is True
    assert review.reconciliation.status is ReconciliationStatus.UNAVAILABLE
    assert review.uncertain_rows == (review.rows[1],)
    assert review.rows[1].review_reasons == {ReviewReason.EXTRACTION_ERROR}
    assert review.rows[0].requires_review is False
    assert review.rows[0].was_edited is False


@pytest.mark.parametrize("threshold", [0.0, 1.01])
def test_prepare_review_rejects_invalid_confidence_threshold(threshold: float) -> None:
    with pytest.raises(StatementReviewError) as error:
        prepare_statement_review(
            _text_preview(_pdf_candidate()),
            ocr_confidence_threshold=threshold,
        )
    assert error.value.code is StatementReviewErrorCode.INVALID_THRESHOLD


def test_low_ocr_confidence_enters_targeted_queue() -> None:
    review = prepare_statement_review(_ocr_preview(0.84))

    assert review.source_type is SourceType.OCR_PDF
    assert review.reconciliation.status is ReconciliationStatus.RECONCILED
    assert review.uncertain_rows[0].review_reasons == {ReviewReason.LOW_OCR_CONFIDENCE}
    assert review.rows[0].field_confidences[0].confidence == 0.84

    high_confidence = prepare_statement_review(_ocr_preview(0.9))
    assert high_confidence.uncertain_rows == ()


def test_confirmed_month_first_format_reparses_transaction_and_posting_dates() -> None:
    original = _original(date_text="01/02/26").model_copy(
        update={"posting_date_text": "02/03/26"}
    )
    extracted = _draft().model_copy(
        update={
            "transaction_date": date(2026, 2, 1),
            "posting_date": date(2026, 3, 2),
        }
    )
    review = prepare_statement_review(
        _text_preview(_pdf_candidate(original=original, draft=extracted))
    )

    approved = approve_statement_review(
        review,
        _approval(date_format=DateFormat.MONTH_FIRST),
    )

    assert approved.rows[0].transaction.transaction_date == date(2026, 1, 2)
    assert approved.rows[0].transaction.posting_date == date(2026, 2, 3)
    assert approved.rows[0].was_edited is True
    assert approved.date_format is DateFormat.MONTH_FIRST


def test_explicit_date_correction_takes_precedence_over_statement_format() -> None:
    extracted = _draft().model_copy(update={"transaction_date": date(2026, 2, 1)})
    corrected = extracted.model_copy(update={"transaction_date": date(2026, 2, 3)})
    review = prepare_statement_review(
        _text_preview(
            _pdf_candidate(
                original=_original(date_text="01/02/2026"),
                draft=extracted,
            )
        )
    )

    approved = approve_statement_review(
        review,
        _approval(
            date_format=DateFormat.DAY_FIRST,
            row_reviews=(
                RowReview(
                    source_fingerprint=HASH_B,
                    decision=RowDecision.CONFIRM,
                    corrected_draft=corrected,
                ),
            ),
        ),
    )

    assert approved.rows[0].transaction.transaction_date == date(2026, 2, 3)


@pytest.mark.parametrize("raw_date", ["01/02/2026", "01/02/0000"])
def test_incompatible_confirmed_date_format_is_rejected(raw_date: str) -> None:
    review = prepare_statement_review(
        _text_preview(_pdf_candidate(original=_original(date_text=raw_date)))
    )
    selected_format = (
        DateFormat.ISO if raw_date.endswith("2026") else DateFormat.DAY_FIRST
    )

    with pytest.raises(StatementReviewError) as error:
        approve_statement_review(
            review,
            _approval(date_format=selected_format),
        )

    assert error.value.code is StatementReviewErrorCode.DATE_FORMAT_CONFLICT


def test_approved_ocr_row_retains_confidence_lines_provenance_and_decision() -> None:
    review = prepare_statement_review(_ocr_preview(0.84))
    approved = approve_statement_review(
        review,
        _approval(
            confirmed_balances=review.balances,
            row_reviews=(
                RowReview(
                    source_fingerprint=HASH_B,
                    decision=RowDecision.CONFIRM,
                ),
            ),
        ),
    )

    row = approved.rows[0]
    assert row.extracted_draft == review.rows[0].extracted_draft
    assert row.provenance.method is ExtractionMethod.OCR
    assert row.source_line_numbers == (2,)
    assert row.field_confidences[0].raw_value == "-10.00"
    assert row.review_reasons == {ReviewReason.LOW_OCR_CONFIDENCE}
    assert row.row_decision is RowDecision.CONFIRM
    assert approved.balance_evidence[0].raw_amount_text == "100.00"
    assert approved.balance_evidence[0].confidence == 0.84
    assert approved.balance_was_edited is False


def test_approval_corrects_row_preserves_source_and_reconciles() -> None:
    issue = ImportIssue(
        code="invalid_date",
        message="Synthetic correction required",
        severity=IssueSeverity.ERROR,
    )
    source = _original(date_text="01/02/2026", amount_text=None, debit_text="9.00")
    candidate = _pdf_candidate(draft=_draft(None), original=source, issue=issue)
    review = prepare_statement_review(
        _text_preview(
            candidate,
            balances=StatementBalances(
                opening_balance=Decimal("100.00"),
                closing_balance=Decimal("90.00"),
            ),
        )
    )
    corrected = _draft(Decimal("-10.00"))
    approved = approve_statement_review(
        review,
        _approval(
            date_format=DateFormat.DAY_FIRST,
            sign_convention=AmountSignConvention.DEBIT_NEGATIVE_CREDIT_POSITIVE,
            confirmed_balances=review.balances,
            row_reviews=(
                RowReview(
                    source_fingerprint=HASH_B,
                    decision=RowDecision.CONFIRM,
                    corrected_draft=corrected,
                ),
            ),
        ),
    )

    assert approved.reconciliation.status is ReconciliationStatus.RECONCILED
    assert approved.rows[0].transaction.amount == Decimal("-10.00")
    assert approved.rows[0].original.debit_amount_text == "9.00"
    assert approved.rows[0].was_edited is True
    assert approved.rows[0].issues == (issue,)
    assert approved.rows[0].provenance.method is ExtractionMethod.PDF_TEXT
    assert approved.rows[0].row_decision is RowDecision.CONFIRM
    assert approved.balances == review.balances
    assert approved.balance_was_edited is False
    assert approved.rejected_source_fingerprints == ()


def test_balance_evidence_requires_confirmation_and_allows_audited_correction() -> None:
    review = prepare_statement_review(
        _text_preview(
            _pdf_candidate(),
            balances=StatementBalances(
                opening_balance=Decimal("100.00"),
                closing_balance=Decimal("90.00"),
            ),
        )
    )
    assert [item.field for item in review.balance_evidence] == [
        StatementBalanceField.OPENING,
        StatementBalanceField.CLOSING,
    ]
    assert review.balance_evidence[0].source_identity.source_document_hash == HASH_A

    with pytest.raises(StatementReviewError) as missing:
        approve_statement_review(
            review,
            _approval(confirmed_statement_coverage=SYNTHETIC_COVERAGE),
        )
    assert missing.value.code is StatementReviewErrorCode.BALANCES_UNCONFIRMED

    with pytest.raises(StatementReviewError) as changed_fields:
        approve_statement_review(
            review,
            _approval(
                confirmed_balances=StatementBalances(opening_balance=Decimal("100.00"))
            ),
        )
    assert changed_fields.value.code is StatementReviewErrorCode.BALANCE_FIELDS_CHANGED

    corrected = StatementBalances(
        opening_balance=Decimal("100.00"),
        closing_balance=Decimal("91.00"),
    )
    approved = approve_statement_review(
        review,
        _approval(
            confirmed_balances=corrected,
            acknowledge_balance_mismatch=True,
        ),
    )
    assert approved.balances == corrected
    assert approved.balance_was_edited is True
    assert approved.reconciliation.unexplained_difference == Decimal("1.00")


def test_statement_coverage_is_confirmed_retained_and_bounds_transactions() -> None:
    review = prepare_statement_review(
        _text_preview(
            _pdf_candidate(),
            balances=StatementBalances(closing_balance=Decimal("90.00")),
        )
    )
    with pytest.raises(StatementReviewError) as missing:
        approve_statement_review(
            review,
            _approval(
                confirmed_statement_coverage=None,
                confirmed_balances=review.balances,
            ),
        )
    assert missing.value.code is StatementReviewErrorCode.COVERAGE_UNCONFIRMED

    approved = approve_statement_review(
        review,
        _approval(confirmed_balances=review.balances),
    )
    assert approved.statement_coverage == SYNTHETIC_COVERAGE
    assert approved.coverage_was_edited is False

    corrected_coverage = StatementCoverage(
        statement_start_date=date(2026, 6, 30),
        statement_end_date=date(2026, 7, 31),
        status=CoverageStatus.UNKNOWN,
    )
    corrected = approve_statement_review(
        review,
        _approval(
            confirmed_statement_coverage=corrected_coverage,
            confirmed_balances=review.balances,
        ),
    )
    assert corrected.statement_coverage == corrected_coverage
    assert corrected.coverage_was_edited is True

    outside = StatementCoverage(
        statement_start_date=date(2026, 8, 1),
        statement_end_date=date(2026, 8, 31),
        status=CoverageStatus.UNKNOWN,
    )
    with pytest.raises(StatementReviewError) as outside_error:
        approve_statement_review(
            review,
            _approval(
                confirmed_statement_coverage=outside,
                confirmed_balances=review.balances,
            ),
        )
    assert (
        outside_error.value.code
        is StatementReviewErrorCode.TRANSACTION_OUTSIDE_COVERAGE
    )

    gapped = StatementCoverage(
        statement_start_date=date(2026, 7, 1),
        statement_end_date=date(2026, 7, 31),
        status=CoverageStatus.GAPPED,
        missing_periods=(
            DateRange(start_date=date(2026, 7, 1), end_date=date(2026, 7, 1)),
        ),
    )
    with pytest.raises(StatementReviewError) as gap_error:
        approve_statement_review(
            review,
            _approval(
                confirmed_statement_coverage=gapped,
                confirmed_balances=review.balances,
            ),
        )
    assert gap_error.value.code is StatementReviewErrorCode.TRANSACTION_OUTSIDE_COVERAGE


def test_invalid_raw_balance_can_be_corrected_without_losing_ocr_evidence() -> None:
    preview = _ocr_preview(0.9)
    opening = (
        preview.pages[0]
        .lines[0]
        .model_copy(update={"raw_text": "Opening balance: NOT_A_NUMBER"})
    )
    page = preview.pages[0].model_copy(
        update={
            "raw_text": "\n".join(
                (
                    opening.raw_text,
                    preview.pages[0].lines[1].raw_text,
                    preview.pages[0].lines[2].raw_text,
                )
            ),
            "lines": (opening, *preview.pages[0].lines[1:]),
        }
    )
    invalid_preview = preview.model_copy(
        update={"pages": (page,), "statement_balances": None}
    )
    review = prepare_statement_review(invalid_preview)

    assert review.balance_evidence[0].amount is None
    assert review.balance_evidence[0].raw_amount_text == "NOT_A_NUMBER"
    assert review.balance_evidence[0].issues[0].code == "invalid_opening_balance"

    corrected = StatementBalances(
        opening_balance=Decimal("100.00"),
        closing_balance=Decimal("90.00"),
    )
    approved = approve_statement_review(
        review,
        _approval(confirmed_balances=corrected),
    )
    assert approved.balances == corrected
    assert approved.balance_was_edited is True
    assert approved.reconciliation.status is ReconciliationStatus.RECONCILED


def test_partial_closing_balance_survives_when_reconciliation_is_unavailable() -> None:
    review = prepare_statement_review(
        _text_preview(
            _pdf_candidate(),
            balances=StatementBalances(closing_balance=Decimal("90.00")),
        )
    )
    approved = approve_statement_review(
        review,
        _approval(confirmed_balances=review.balances),
    )

    assert approved.balances is not None
    assert approved.balances.closing_balance == Decimal("90.00")
    assert approved.reconciliation.status is ReconciliationStatus.UNAVAILABLE


def test_balance_confirmation_without_source_evidence_is_rejected() -> None:
    review = prepare_statement_review(_text_preview(_pdf_candidate()))

    with pytest.raises(StatementReviewError) as error:
        approve_statement_review(
            review,
            _approval(
                confirmed_balances=StatementBalances(closing_balance=Decimal("90.00"))
            ),
        )

    assert error.value.code is StatementReviewErrorCode.UNEXPECTED_BALANCE_CONFIRMATION


def test_row_correction_cannot_change_protected_financial_identity_fields() -> None:
    review = prepare_statement_review(_text_preview(_pdf_candidate()))
    changed_account = _draft().model_copy(update={"account_id": "account-2"})

    with pytest.raises(StatementReviewError) as error:
        approve_statement_review(
            review,
            _approval(
                row_reviews=(
                    RowReview(
                        source_fingerprint=HASH_B,
                        decision=RowDecision.CONFIRM,
                        corrected_draft=changed_account,
                    ),
                )
            ),
        )

    assert error.value.code is StatementReviewErrorCode.PROTECTED_FIELD_CHANGED


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"file_hash": "f" * 64}, StatementReviewErrorCode.FILE_CHANGED),
        ({}, StatementReviewErrorCode.DATE_FORMAT_UNCONFIRMED),
        (
            {"date_format": DateFormat.DAY_FIRST},
            StatementReviewErrorCode.SIGN_CONVENTION_UNCONFIRMED,
        ),
    ],
)
def test_statement_confirmation_prerequisites(
    changes: dict[str, object],
    code: StatementReviewErrorCode,
) -> None:
    original = _original(
        date_text="01/02/2026",
        amount_text=None,
        debit_text="10.00",
    )
    review = prepare_statement_review(_text_preview(_pdf_candidate(original=original)))
    with pytest.raises(StatementReviewError) as error:
        approve_statement_review(review, _approval(**changes))
    assert error.value.code is code


def test_uncertain_invalid_and_unacknowledged_mismatch_cannot_be_trusted() -> None:
    issue = ImportIssue(
        code="invalid_amount",
        message="Synthetic amount requires correction",
        severity=IssueSeverity.ERROR,
    )
    uncertain = prepare_statement_review(
        _text_preview(_pdf_candidate(draft=_draft(None), issue=issue))
    )
    with pytest.raises(StatementReviewError) as unresolved:
        approve_statement_review(uncertain, _approval())
    assert unresolved.value.code is StatementReviewErrorCode.UNCERTAIN_ROW_UNRESOLVED

    with pytest.raises(StatementReviewError) as invalid:
        approve_statement_review(
            uncertain,
            _approval(
                row_reviews=(
                    RowReview(
                        source_fingerprint=HASH_B,
                        decision=RowDecision.CONFIRM,
                    ),
                )
            ),
        )
    assert invalid.value.code is StatementReviewErrorCode.INVALID_CONFIRMED_ROW

    mismatch = prepare_statement_review(
        _text_preview(
            _pdf_candidate(),
            balances=StatementBalances(
                opening_balance=Decimal("100.00"),
                closing_balance=Decimal("89.00"),
            ),
        )
    )
    with pytest.raises(StatementReviewError) as unacknowledged:
        approve_statement_review(
            mismatch,
            _approval(confirmed_balances=mismatch.balances),
        )
    assert (
        unacknowledged.value.code
        is StatementReviewErrorCode.BALANCE_MISMATCH_UNACKNOWLEDGED
    )
    approved = approve_statement_review(
        mismatch,
        _approval(
            confirmed_balances=mismatch.balances,
            acknowledge_balance_mismatch=True,
        ),
    )
    assert approved.reconciliation.unexplained_difference == Decimal("-1.00")


def test_decisions_must_be_unique_known_and_rejections_keep_no_transaction() -> None:
    review = prepare_statement_review(_text_preview(_pdf_candidate()))
    duplicate = RowReview(source_fingerprint=HASH_B, decision=RowDecision.CONFIRM)
    with pytest.raises(StatementReviewError) as duplicate_error:
        approve_statement_review(
            review,
            _approval(row_reviews=(duplicate, duplicate)),
        )
    assert duplicate_error.value.code is StatementReviewErrorCode.DUPLICATE_DECISION

    with pytest.raises(StatementReviewError) as unknown_error:
        approve_statement_review(
            review,
            _approval(
                row_reviews=(
                    RowReview(
                        source_fingerprint="f" * 64,
                        decision=RowDecision.REJECT,
                    ),
                )
            ),
        )
    assert unknown_error.value.code is StatementReviewErrorCode.UNKNOWN_ROW

    rejected = approve_statement_review(
        review,
        _approval(
            row_reviews=(
                RowReview(
                    source_fingerprint=HASH_B,
                    decision=RowDecision.REJECT,
                ),
            )
        ),
    )
    assert rejected.rows == ()
    assert rejected.rejected_rows == (review.rows[0],)
    assert rejected.rejected_rows[0].original.description_text == "SYNTHETIC ITEM"
    assert rejected.rejected_source_fingerprints == (HASH_B,)


def test_review_contracts_reject_inert_or_unauditable_decisions() -> None:
    with pytest.raises(ValidationError, match="rejected rows cannot"):
        RowReview(
            source_fingerprint=HASH_B,
            decision=RowDecision.REJECT,
            corrected_draft=_draft(),
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        StatementApproval(
            file_hash=HASH_A,
            approved_at=datetime(2026, 8, 10, 12, 0),
            statement_approved=True,
        )


def test_prepare_review_rejects_parsed_balances_without_matching_raw_evidence() -> None:
    balances = StatementBalances(
        opening_balance=Decimal("100.00"),
        closing_balance=Decimal("90.00"),
    )
    preview = _text_preview(_pdf_candidate(), balances=balances)
    page = preview.pages[0].model_copy(
        update={"raw_text": preview.pages[0].raw_text.replace("100.00", "101.00")}
    )

    with pytest.raises(StatementReviewError) as error:
        prepare_statement_review(preview.model_copy(update={"pages": (page,)}))

    assert error.value.code is StatementReviewErrorCode.BALANCE_EVIDENCE_MISSING


def test_statement_review_contract_binds_hash_account_currency_and_pdf_source() -> None:
    review = prepare_statement_review(_text_preview(_pdf_candidate()))

    csv_payload = review.model_dump(mode="python")
    csv_payload["source_type"] = SourceType.CSV
    with pytest.raises(ValidationError, match="requires PDF source"):
        StatementReview.model_validate(csv_payload)

    hash_payload = review.model_dump(mode="python")
    hash_payload["file_hash"] = HASH_C
    with pytest.raises(ValidationError, match="exact statement source"):
        StatementReview.model_validate(hash_payload)

    account_payload = review.model_dump(mode="python")
    account_payload["rows"][0]["extracted_draft"]["account_id"] = None
    with pytest.raises(ValidationError, match="exactly one account"):
        StatementReview.model_validate(account_payload)

    currency_payload = review.model_dump(mode="python")
    currency_payload["rows"][0]["extracted_draft"]["currency"] = None
    with pytest.raises(ValidationError, match="exactly one currency"):
        StatementReview.model_validate(currency_payload)


def test_review_row_contract_rejects_incoherent_pdf_and_ocr_lineage() -> None:
    digital_row = prepare_statement_review(_text_preview(_pdf_candidate())).rows[0]

    source_payload = digital_row.model_dump(mode="python")
    source_payload["provenance"] = ExtractionProvenance(
        source_type=SourceType.OCR_PDF,
        method=ExtractionMethod.OCR,
        page_number=1,
        confidence=0.9,
        parser=PARSER,
    )
    with pytest.raises(ValidationError, match="match its source identity"):
        StatementReviewRow.model_validate(source_payload)

    page_payload = digital_row.model_dump(mode="python")
    page_payload["provenance"] = ExtractionProvenance(
        source_type=SourceType.DIGITAL_PDF,
        method=ExtractionMethod.PDF_TEXT,
        page_number=2,
        parser=PARSER,
    )
    with pytest.raises(ValidationError, match="match its source page"):
        StatementReviewRow.model_validate(page_payload)

    digital_lines = digital_row.model_dump(mode="python")
    digital_lines["source_line_numbers"] = (1,)
    with pytest.raises(ValidationError, match="cannot claim OCR"):
        StatementReviewRow.model_validate(digital_lines)

    ocr_row = prepare_statement_review(_ocr_preview(0.9)).rows[0]
    ocr_payload = ocr_row.model_dump(mode="python")
    ocr_payload["source_line_numbers"] = ()
    with pytest.raises(ValidationError, match="require source line"):
        StatementReviewRow.model_validate(ocr_payload)


def test_balance_evidence_contract_rejects_incoherent_or_unexplained_lineage() -> None:
    review = prepare_statement_review(
        _text_preview(
            _pdf_candidate(),
            balances=StatementBalances(opening_balance=Decimal("100.00")),
        )
    )
    evidence = review.balance_evidence[0]

    csv_payload = evidence.model_dump(mode="python")
    csv_payload["source_identity"] = SourceRecordIdentity(
        source_type=SourceType.CSV,
        source_document_hash=HASH_A,
        source_row_number=1,
    )
    csv_payload["provenance"] = ExtractionProvenance(
        source_type=SourceType.CSV,
        method=ExtractionMethod.CSV_ROW,
    )
    with pytest.raises(ValidationError, match="requires PDF lineage"):
        StatementBalanceEvidence.model_validate(csv_payload)

    source_payload = evidence.model_dump(mode="python")
    source_payload["provenance"] = ExtractionProvenance(
        source_type=SourceType.OCR_PDF,
        method=ExtractionMethod.OCR,
        page_number=1,
        confidence=0.9,
        parser=PARSER,
    )
    with pytest.raises(ValidationError, match="match its source identity"):
        StatementBalanceEvidence.model_validate(source_payload)

    page_payload = evidence.model_dump(mode="python")
    page_payload["provenance"] = evidence.provenance.model_copy(
        update={"page_number": 2}
    )
    with pytest.raises(ValidationError, match="match its source page"):
        StatementBalanceEvidence.model_validate(page_payload)

    line_payload = evidence.model_dump(mode="python")
    line_payload["line_number"] = evidence.line_number + 1
    with pytest.raises(ValidationError, match="match its source line"):
        StatementBalanceEvidence.model_validate(line_payload)

    issue_payload = evidence.model_dump(mode="python")
    issue_payload["amount"] = None
    with pytest.raises(ValidationError, match="requires a structured issue"):
        StatementBalanceEvidence.model_validate(issue_payload)
