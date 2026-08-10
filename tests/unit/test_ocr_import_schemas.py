"""Tests for scanned-PDF OCR preview contracts."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cashflow_ai.schemas import (
    Currency,
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
    SourceFieldValue,
    SourceRecordIdentity,
    SourceType,
    TransactionDraft,
    TransactionField,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
PARSER = ParserIdentity(name="synthetic_ocr_parser", version="1.0")


def line(line_number: int = 1, text: str = "Synthetic OCR line") -> OcrLineExtraction:
    return OcrLineExtraction(
        line_number=line_number,
        raw_text=text,
        confidence=0.9,
        word_count=3,
    )


def page(page_number: int = 1) -> OcrPageExtraction:
    extracted_line = line()
    return OcrPageExtraction(
        page_number=page_number,
        pixel_width=600,
        pixel_height=800,
        render_dpi=300,
        rotation_applied_degrees=0,
        threshold_applied=False,
        raw_text=extracted_line.raw_text,
        confidence=0.9,
        lines=(extracted_line,),
    )


def candidate(
    *,
    page_number: int = 1,
    line_numbers: tuple[int, ...] = (1,),
    canonical_fingerprint: str | None = HASH_B,
    issues: tuple[ImportIssue, ...] = (),
) -> OcrTransactionCandidate:
    return OcrTransactionCandidate(
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
            source_type=SourceType.OCR_PDF,
            source_document_hash=HASH_A,
            page_number=page_number,
            page_record_number=1,
        ),
        source_fingerprint=HASH_A,
        canonical_fingerprint=canonical_fingerprint,
        provenance=ExtractionProvenance(
            source_type=SourceType.OCR_PDF,
            method=ExtractionMethod.OCR,
            page_number=page_number,
            confidence=0.9,
            parser=PARSER,
        ),
        line_numbers=line_numbers,
        field_confidences=(
            FieldConfidence(
                field=TransactionField.AMOUNT,
                confidence=0.9,
                raw_value="-1.00",
            ),
        ),
        issues=issues,
    )


def test_valid_ocr_contracts_preserve_raw_text_and_require_review() -> None:
    extracted = candidate()
    preview = OcrPdfPreview(
        source_filename="synthetic-scan.pdf",
        byte_size=100,
        file_hash=HASH_A,
        page_count=1,
        pages=(page(),),
        candidates=(extracted,),
    )

    assert preview.candidates == (extracted,)
    assert preview.requires_user_confirmation is True
    assert preview.temporary_artifacts_retained is False


def test_page_requires_consecutive_lines_and_matching_raw_text() -> None:
    valid = page().model_dump()
    with pytest.raises(ValidationError, match="numbered consecutively"):
        OcrPageExtraction.model_validate(
            {**valid, "lines": (line(2),), "raw_text": "Synthetic OCR line"}
        )
    with pytest.raises(ValidationError, match="preserve its ordered lines"):
        OcrPageExtraction.model_validate({**valid, "raw_text": "changed"})


def test_candidate_requires_ocr_lineage_and_matching_page() -> None:
    valid = candidate()
    digital_identity = SourceRecordIdentity(
        source_type=SourceType.DIGITAL_PDF,
        source_document_hash=HASH_A,
        page_number=1,
        page_record_number=1,
    )
    with pytest.raises(ValidationError, match="OCR-PDF lineage"):
        OcrTransactionCandidate.model_validate(
            {**valid.model_dump(), "source_identity": digital_identity}
        )

    other_page = ExtractionProvenance(
        source_type=SourceType.OCR_PDF,
        method=ExtractionMethod.OCR,
        page_number=2,
        confidence=0.8,
        parser=PARSER,
    )
    with pytest.raises(ValidationError, match="match its source page"):
        OcrTransactionCandidate.model_validate(
            {**valid.model_dump(), "provenance": other_page}
        )


def test_candidate_requires_ordered_lines_unique_fields_and_explained_failure() -> None:
    valid = candidate()
    with pytest.raises(ValidationError, match="unique and ordered"):
        OcrTransactionCandidate.model_validate(
            {**valid.model_dump(), "line_numbers": (2, 1)}
        )
    duplicated = valid.field_confidences * 2
    with pytest.raises(ValidationError, match="entries must be unique"):
        OcrTransactionCandidate.model_validate(
            {**valid.model_dump(), "field_confidences": duplicated}
        )
    with pytest.raises(ValidationError, match="require a review issue"):
        candidate(canonical_fingerprint=None)

    issue = ImportIssue(
        code="invalid_date",
        message="Synthetic invalid date",
        severity=IssueSeverity.ERROR,
    )
    assert candidate(canonical_fingerprint=None, issues=(issue,)).issues == (issue,)


def test_preview_requires_complete_pages_and_in_range_candidate_lines() -> None:
    with pytest.raises(ValidationError, match="complete document"):
        OcrPdfPreview(
            source_filename="synthetic.pdf",
            byte_size=100,
            file_hash=HASH_A,
            page_count=2,
            pages=(page(),),
            candidates=(candidate(),),
        )
    with pytest.raises(ValidationError, match="outside the preview"):
        OcrPdfPreview(
            source_filename="synthetic.pdf",
            byte_size=100,
            file_hash=HASH_A,
            page_count=1,
            pages=(page(),),
            candidates=(candidate(line_numbers=(2,)),),
        )
