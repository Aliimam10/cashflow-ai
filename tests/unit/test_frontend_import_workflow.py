"""Tests for pure statement-import form adapters using fictional values only."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cashflow_ai.frontend.import_workflow import (
    UploadKind,
    balances_confirmed_from_review,
    build_import_context,
    build_statement_balances,
    build_statement_coverage,
    corrected_row_review,
    csv_preview_rows,
    optional_iso_date,
    optional_money,
    optional_text,
    parse_gap_ranges,
    pdf_review_rows,
    suggested_column_index,
)
from cashflow_ai.schemas.api import PdfSourceType
from cashflow_ai.schemas.csv_imports import (
    CsvColumnSuggestions,
    CsvEncoding,
    CsvPreview,
    CsvPreviewRow,
)
from cashflow_ai.schemas.imports import (
    ExtractionMethod,
    ExtractionProvenance,
    FieldConfidence,
    ImportIssue,
    IssueSeverity,
    ParserIdentity,
    SourceType,
    TransactionField,
)
from cashflow_ai.schemas.normalisation import (
    OriginalTransactionValues,
    SourceFieldValue,
    SourceRecordIdentity,
)
from cashflow_ai.schemas.reconciliation import (
    ReconciliationStatus,
    ReviewReason,
    RowDecision,
    StatementBalanceEvidence,
    StatementBalanceField,
    StatementReconciliation,
    StatementReview,
    StatementReviewRow,
)
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    DateRange,
    StatementBalances,
    StatementCoverage,
    StatementFlag,
)
from cashflow_ai.schemas.transactions import Currency, Direction, TransactionDraft

HASH_A = "a" * 64
HASH_B = "b" * 64
PARSER = ParserIdentity(name="synthetic_frontend", version="1.0")


def _row(*, ocr: bool = False, low_confidence: bool = False) -> StatementReviewRow:
    source_type = SourceType.OCR_PDF if ocr else SourceType.DIGITAL_PDF
    draft = TransactionDraft(
        transaction_date=date(2026, 8, 1),
        description="SYNTHETIC SHOP",
        amount=Decimal("-10.00"),
        balance_after=Decimal("90.00"),
        currency=Currency.GBP,
        account_id="synthetic-account",
        direction=Direction.OUTFLOW,
    )
    return StatementReviewRow(
        source_identity=SourceRecordIdentity(
            source_type=source_type,
            source_document_hash=HASH_A,
            page_number=1,
            page_record_number=1,
        ),
        source_fingerprint=HASH_B,
        original=OriginalTransactionValues(
            transaction_date_text="2026-08-01",
            description_text="SYNTHETIC SHOP",
            signed_amount_text="-10.00",
            running_balance_text="90.00",
            raw_fields=(SourceFieldValue(column="Date", value="2026-08-01"),),
        ),
        extracted_draft=draft,
        working_draft=draft,
        provenance=ExtractionProvenance(
            source_type=source_type,
            method=(ExtractionMethod.OCR if ocr else ExtractionMethod.PDF_TEXT),
            page_number=1,
            confidence=0.70 if ocr else None,
            parser=PARSER,
        ),
        source_line_numbers=(1,) if ocr else (),
        field_confidences=(
            (
                FieldConfidence(
                    field=TransactionField.AMOUNT,
                    confidence=0.65,
                    raw_value="-10.00",
                ),
            )
            if ocr
            else ()
        ),
        issues=(
            ImportIssue(
                code="low_confidence",
                message="Synthetic amount needs review",
                severity=IssueSeverity.WARNING,
                field=TransactionField.AMOUNT,
            ),
        )
        if low_confidence
        else (),
        review_reasons=(
            frozenset({ReviewReason.LOW_OCR_CONFIDENCE})
            if low_confidence
            else frozenset()
        ),
    )


def _balance_evidence(
    field: StatementBalanceField,
    amount: Decimal,
) -> StatementBalanceEvidence:
    record_number = 2 if field is StatementBalanceField.OPENING else 3
    return StatementBalanceEvidence(
        field=field,
        raw_amount_text=str(amount),
        amount=amount,
        source_identity=SourceRecordIdentity(
            source_type=SourceType.DIGITAL_PDF,
            source_document_hash=HASH_A,
            page_number=1,
            page_record_number=record_number,
        ),
        provenance=ExtractionProvenance(
            source_type=SourceType.DIGITAL_PDF,
            method=ExtractionMethod.PDF_TEXT,
            page_number=1,
            parser=PARSER,
        ),
        line_number=record_number,
    )


def _review(
    *,
    ocr: bool = False,
    low_confidence: bool = False,
    balance_fields: tuple[StatementBalanceField, ...] = (),
) -> StatementReview:
    row = _row(ocr=ocr, low_confidence=low_confidence)
    evidence = tuple(
        _balance_evidence(
            field,
            Decimal("100.00")
            if field is StatementBalanceField.OPENING
            else Decimal("90.00"),
        )
        for field in balance_fields
    )
    balances = (
        StatementBalances(
            opening_balance=(
                Decimal("100.00")
                if StatementBalanceField.OPENING in balance_fields
                else None
            ),
            closing_balance=(
                Decimal("90.00")
                if StatementBalanceField.CLOSING in balance_fields
                else None
            ),
        )
        if evidence
        else None
    )
    reconciliation = (
        StatementReconciliation(
            status=ReconciliationStatus.RECONCILED,
            opening_balance=Decimal("100.00"),
            signed_transaction_total=Decimal("-10.00"),
            expected_closing_balance=Decimal("90.00"),
            closing_balance=Decimal("90.00"),
            unexplained_difference=Decimal("0.00"),
            unusable_transaction_count=0,
        )
        if len(balance_fields) == 2
        else StatementReconciliation(
            status=ReconciliationStatus.UNAVAILABLE,
            opening_balance=None,
            signed_transaction_total=Decimal("-10.00"),
            expected_closing_balance=None,
            closing_balance=None,
            unexplained_difference=None,
            unusable_transaction_count=0,
        )
    )
    return StatementReview(
        file_hash=HASH_A,
        source_type=row.source_identity.source_type,
        statement_coverage=(
            StatementCoverage(
                statement_start_date=date(2026, 8, 1),
                statement_end_date=date(2026, 8, 31),
                status=CoverageStatus.COMPLETE,
            )
            if evidence
            else None
        ),
        balances=balances,
        balance_evidence=evidence,
        rows=(row,),
        reconciliation=reconciliation,
        ocr_confidence_threshold=0.85,
        requires_date_format_confirmation=False,
        requires_debit_credit_sign_confirmation=False,
    )


def _csv_preview() -> CsvPreview:
    return CsvPreview(
        source_filename="synthetic.csv",
        byte_size=50,
        file_hash=HASH_A,
        encoding=CsvEncoding.UTF_8,
        delimiter=",",
        columns=("Date", "Description", "Amount"),
        rows=(
            CsvPreviewRow(
                source_row_number=2,
                values=("2026-08-01", "SYNTHETIC SHOP", "-10.00"),
            ),
        ),
        total_data_rows=1,
        truncated=False,
        suggestions=CsvColumnSuggestions(
            transaction_date=("Date",),
            description=("Description",),
            signed_amount=("Amount",),
        ),
    )


def test_upload_kind_routes_only_pdfs_to_pdf_adapters() -> None:
    assert UploadKind.CSV.extensions == ("csv",)
    assert UploadKind.CSV.mime_type == "text/csv"
    assert UploadKind.DIGITAL_PDF.extensions == ("pdf",)
    assert UploadKind.DIGITAL_PDF.mime_type == "application/pdf"
    assert UploadKind.DIGITAL_PDF.pdf_source_type is PdfSourceType.DIGITAL_PDF
    assert UploadKind.OCR_PDF.pdf_source_type is PdfSourceType.OCR_PDF
    with pytest.raises(ValueError, match="do not have"):
        _ = UploadKind.CSV.pdf_source_type


def test_optional_form_values_are_parsed_without_changing_source_data() -> None:
    assert optional_text("  Fictional note  ") == "Fictional note"
    assert optional_text("   ") is None
    assert optional_money(" 10.25 ") == Decimal("10.25")
    assert optional_money("") is None
    assert optional_iso_date("2026-08-03") == date(2026, 8, 3)
    assert optional_iso_date(" ") is None
    with pytest.raises(ValueError, match="decimal"):
        optional_money("not-money")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        optional_iso_date("03/08/2026")


def test_missing_period_parser_requires_complete_ordered_iso_ranges() -> None:
    assert parse_gap_ranges("\n2026-08-03,2026-08-04\n") == (
        DateRange(start_date=date(2026, 8, 3), end_date=date(2026, 8, 4)),
    )
    with pytest.raises(ValueError, match="start-date,end-date"):
        parse_gap_ranges("2026-08-03")
    with pytest.raises(ValueError, match="cannot be blank"):
        parse_gap_ranges("2026-08-03,")
    with pytest.raises(ValidationError, match="end must not precede"):
        parse_gap_ranges("2026-08-04,2026-08-03")


def test_context_builders_preserve_explicit_coverage_balances_flags_and_note() -> None:
    coverage = build_statement_coverage(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        status=CoverageStatus.GAPPED,
        missing_periods_text="2026-08-10,2026-08-11",
    )
    balances = build_statement_balances(
        currency=Currency.GBP,
        opening_balance_text="100.00",
        closing_balance_text="90.00",
    )
    context = build_import_context(
        account_id="synthetic-account",
        coverage=coverage,
        balances=balances,
        flags=(StatementFlag.MAY_CONTAIN_MISSING_DATES_OR_PAGES,),
        note=" Synthetic reference only ",
    )

    assert context.coverage.missing_periods[0].start_date == date(2026, 8, 10)
    assert context.balances == balances
    assert context.note == "Synthetic reference only"
    assert context.flags == {StatementFlag.MAY_CONTAIN_MISSING_DATES_OR_PAGES}
    assert (
        build_statement_balances(
            currency=Currency.GBP,
            opening_balance_text="",
            closing_balance_text="",
        )
        is None
    )


def test_csv_preview_table_and_suggestion_indices_are_deterministic() -> None:
    preview = _csv_preview()
    assert (
        suggested_column_index(preview.columns, ("Missing", "Amount"), optional=False)
        == 2
    )
    assert suggested_column_index(preview.columns, ("Amount",), optional=True) == 3
    assert suggested_column_index(preview.columns, ("Missing",), optional=True) == 0
    assert csv_preview_rows(preview) == (
        {
            "source row": 2,
            "Date": "2026-08-01",
            "Description": "SYNTHETIC SHOP",
            "Amount": "-10.00",
        },
    )


def test_pdf_table_uses_field_or_provenance_confidence() -> None:
    text_row = pdf_review_rows(_review())[0]
    ocr_row = pdf_review_rows(_review(ocr=True, low_confidence=True))[0]

    assert text_row["minimum confidence"] is None
    assert ocr_row["minimum confidence"] == 0.65
    assert ocr_row["review reasons"] == "low_ocr_confidence"


@pytest.mark.parametrize(
    ("amount", "direction"),
    [
        ("15.00", Direction.INFLOW),
        ("-15.00", Direction.OUTFLOW),
        ("0.00", None),
        ("", None),
    ],
)
def test_row_review_corrections_derive_direction_and_keep_protected_fields(
    amount: str,
    direction: Direction | None,
) -> None:
    row = _row(ocr=True, low_confidence=True)
    correction = corrected_row_review(
        row,
        decision=RowDecision.CONFIRM,
        transaction_date_text="2026-08-02",
        posting_date_text="",
        description="SYNTHETIC CORRECTED",
        amount_text=amount,
        balance_after_text="95.00",
    )

    assert correction.corrected_draft is not None
    assert correction.corrected_draft.direction is direction
    assert correction.corrected_draft.account_id == row.extracted_draft.account_id
    assert correction.corrected_draft.currency == row.extracted_draft.currency
    rejected = corrected_row_review(
        row,
        decision=RowDecision.REJECT,
        transaction_date_text="",
        posting_date_text="",
        description="",
        amount_text="",
        balance_after_text="",
    )
    assert rejected.corrected_draft is None


@pytest.mark.parametrize(
    "fields",
    [
        (),
        (StatementBalanceField.OPENING,),
        (StatementBalanceField.CLOSING,),
        (StatementBalanceField.OPENING, StatementBalanceField.CLOSING),
    ],
)
def test_pdf_balance_confirmation_matches_source_evidence(
    fields: tuple[StatementBalanceField, ...],
) -> None:
    result = balances_confirmed_from_review(
        _review(balance_fields=fields),
        opening_balance_text="101.00",
        closing_balance_text="91.00",
    )

    if not fields:
        assert result is None
        return
    assert result is not None
    assert result.opening_balance == (
        Decimal("101.00") if StatementBalanceField.OPENING in fields else None
    )
    assert result.closing_balance == (
        Decimal("91.00") if StatementBalanceField.CLOSING in fields else None
    )
