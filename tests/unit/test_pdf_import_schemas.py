"""Tests for digital-PDF preview and candidate contracts."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cashflow_ai.schemas import (
    Currency,
    Direction,
    ExtractionMethod,
    ExtractionProvenance,
    ImportIssue,
    IssueSeverity,
    OriginalTransactionValues,
    ParserIdentity,
    PdfExtractionLayout,
    PdfPageExtraction,
    PdfTransactionCandidate,
    SourceFieldValue,
    SourceRecordIdentity,
    SourceType,
    TextPdfPreview,
    TransactionDraft,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
PARSER = ParserIdentity(name="synthetic_pdf_parser", version="1.0")


def candidate(
    *,
    page_number: int = 1,
    canonical_fingerprint: str | None = HASH_B,
    issues: tuple[ImportIssue, ...] = (),
) -> PdfTransactionCandidate:
    return PdfTransactionCandidate(
        original=OriginalTransactionValues(
            transaction_date_text="2026-07-01",
            description_text="Synthetic item",
            signed_amount_text="-1.00",
            raw_fields=(
                SourceFieldValue(column="Date", value="2026-07-01"),
                SourceFieldValue(column="Description", value="Synthetic item"),
                SourceFieldValue(column="Amount", value="-1.00"),
            ),
        ),
        draft=TransactionDraft(
            transaction_date=date(2026, 7, 1),
            description="Synthetic item",
            amount=Decimal("-1.00"),
            currency=Currency.GBP,
            account_id="account-1",
            direction=Direction.OUTFLOW,
        ),
        source_identity=SourceRecordIdentity(
            source_type=SourceType.DIGITAL_PDF,
            source_document_hash=HASH_A,
            page_number=page_number,
            page_record_number=1,
        ),
        source_fingerprint=HASH_A,
        canonical_fingerprint=canonical_fingerprint,
        provenance=ExtractionProvenance(
            source_type=SourceType.DIGITAL_PDF,
            method=ExtractionMethod.PDF_TEXT,
            page_number=page_number,
            parser=PARSER,
        ),
        issues=issues,
    )


def page(page_number: int = 1) -> PdfPageExtraction:
    return PdfPageExtraction(
        page_number=page_number,
        raw_text="Synthetic embedded statement text",
        embedded_character_count=30,
        tables_found=0,
    )


def test_valid_candidate_and_preview_require_review() -> None:
    extracted = candidate()
    preview = TextPdfPreview(
        source_filename="synthetic.pdf",
        byte_size=100,
        file_hash=HASH_A,
        page_count=1,
        pages=(page(),),
        layouts=frozenset({PdfExtractionLayout.GENERIC_TEXT}),
        candidates=(extracted,),
    )

    assert preview.candidates == (extracted,)
    assert preview.requires_user_confirmation is True


def test_noncanonical_candidate_requires_an_issue() -> None:
    with pytest.raises(ValidationError, match="require a review issue"):
        candidate(canonical_fingerprint=None)

    issue = ImportIssue(
        code="invalid_date",
        message="Synthetic invalid date",
        severity=IssueSeverity.ERROR,
    )
    extracted = candidate(canonical_fingerprint=None, issues=(issue,))
    assert extracted.issues == (issue,)


def test_candidate_rejects_non_digital_lineage_and_page_mismatch() -> None:
    valid = candidate()
    ocr_identity = SourceRecordIdentity(
        source_type=SourceType.OCR_PDF,
        source_document_hash=HASH_A,
        page_number=1,
        page_record_number=1,
    )
    with pytest.raises(ValidationError, match="digital-PDF lineage"):
        PdfTransactionCandidate.model_validate(
            {**valid.model_dump(), "source_identity": ocr_identity}
        )

    ocr_provenance = ExtractionProvenance(
        source_type=SourceType.OCR_PDF,
        method=ExtractionMethod.OCR,
        page_number=1,
        confidence=0.9,
        parser=PARSER,
    )
    with pytest.raises(ValidationError, match="digital-PDF lineage"):
        PdfTransactionCandidate.model_validate(
            {**valid.model_dump(), "provenance": ocr_provenance}
        )

    other_page = ExtractionProvenance(
        source_type=SourceType.DIGITAL_PDF,
        method=ExtractionMethod.PDF_TABLE,
        page_number=2,
        parser=PARSER,
    )
    with pytest.raises(ValidationError, match="match its source page"):
        PdfTransactionCandidate.model_validate(
            {**valid.model_dump(), "provenance": other_page}
        )


def test_preview_requires_complete_pages_and_in_range_candidates() -> None:
    with pytest.raises(ValidationError, match="complete document"):
        TextPdfPreview(
            source_filename="synthetic.pdf",
            byte_size=100,
            file_hash=HASH_A,
            page_count=2,
            pages=(page(),),
            layouts=frozenset({PdfExtractionLayout.TABLE}),
            candidates=(candidate(),),
        )

    with pytest.raises(ValidationError, match="outside the preview"):
        TextPdfPreview(
            source_filename="synthetic.pdf",
            byte_size=100,
            file_hash=HASH_A,
            page_count=1,
            pages=(page(),),
            layouts=frozenset({PdfExtractionLayout.TABLE}),
            candidates=(candidate(page_number=2),),
        )
