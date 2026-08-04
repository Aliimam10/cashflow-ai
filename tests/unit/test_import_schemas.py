"""Tests for uploaded-document and extraction-review contracts."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from cashflow_ai.schemas import (
    ExtractionMethod,
    ExtractionProvenance,
    FieldConfidence,
    ImportCandidate,
    ImportDocument,
    ImportIssue,
    IssueSeverity,
    ReviewStatus,
    SourceRegion,
    SourceType,
    TransactionDraft,
    TransactionField,
)

DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000001")
BATCH_ID = UUID("00000000-0000-0000-0000-000000000002")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000003")
HASH = "a" * 64
UPLOADED_AT = datetime(2026, 8, 4, 10, 30, tzinfo=UTC)


def csv_provenance() -> ExtractionProvenance:
    return ExtractionProvenance(
        source_type=SourceType.CSV,
        method=ExtractionMethod.CSV_ROW,
    )


def valid_candidate_payload() -> dict[str, object]:
    return {
        "candidate_id": CANDIDATE_ID,
        "source_document_id": DOCUMENT_ID,
        "import_batch_id": BATCH_ID,
        "source_row_number": 2,
        "raw_payload": {"Date": "04/08/2026", "Amount": "-12.50"},
        "provenance": csv_provenance(),
        "draft": TransactionDraft.model_validate(
            {
                "transaction_date": "2026-08-04",
                "description": "EXAMPLE CAFE",
                "amount": "-12.50",
            }
        ),
    }


def test_csv_document_and_candidate_are_valid() -> None:
    document = ImportDocument(
        source_document_id=DOCUMENT_ID,
        import_batch_id=BATCH_ID,
        source_type=SourceType.CSV,
        source_filename="statement.csv",
        file_hash=HASH,
        mime_type="text/csv",
        byte_size=512,
        uploaded_at=UPLOADED_AT,
    )
    candidate = ImportCandidate.model_validate(valid_candidate_payload())

    assert document.source_type is SourceType.CSV
    assert candidate.review_status is ReviewStatus.PENDING
    assert candidate.source_row_number == 2


@pytest.mark.parametrize(
    "method", [ExtractionMethod.PDF_TEXT, ExtractionMethod.PDF_TABLE]
)
def test_digital_pdf_provenance_supports_text_and_tables(
    method: ExtractionMethod,
) -> None:
    provenance = ExtractionProvenance(
        source_type=SourceType.DIGITAL_PDF,
        method=method,
        page_number=1,
        region=SourceRegion(x=10, y=20, width=300, height=18),
        confidence=0.98,
    )

    assert provenance.page_number == 1
    assert provenance.region is not None


def test_ocr_provenance_requires_and_accepts_confidence() -> None:
    provenance = ExtractionProvenance(
        source_type=SourceType.OCR_PDF,
        method=ExtractionMethod.OCR,
        page_number=2,
        confidence=0.72,
    )

    assert provenance.confidence == 0.72


@pytest.mark.parametrize(
    "payload",
    [
        {
            "source_type": "csv",
            "method": "pdf_text",
        },
        {
            "source_type": "csv",
            "method": "csv_row",
            "page_number": 1,
        },
        {
            "source_type": "digital_pdf",
            "method": "pdf_text",
        },
        {
            "source_type": "ocr_pdf",
            "method": "ocr",
            "page_number": 1,
        },
    ],
)
def test_invalid_provenance_is_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExtractionProvenance.model_validate(payload)


def test_source_region_and_confidence_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        SourceRegion(x=-1, y=0, width=0, height=1)
    with pytest.raises(ValidationError):
        FieldConfidence(field=TransactionField.AMOUNT, confidence=1.1)


@pytest.mark.parametrize(
    ("source_type", "mime_type"),
    [
        (SourceType.CSV, "application/octet-stream"),
        (SourceType.DIGITAL_PDF, "text/plain"),
    ],
)
def test_document_source_and_mime_type_must_match(
    source_type: SourceType,
    mime_type: str,
) -> None:
    with pytest.raises(ValidationError):
        ImportDocument(
            source_document_id=DOCUMENT_ID,
            import_batch_id=BATCH_ID,
            source_type=source_type,
            source_filename="statement",
            file_hash=HASH,
            mime_type=mime_type,
            byte_size=100,
            uploaded_at=UPLOADED_AT,
        )


def test_pdf_document_requires_aware_time_and_valid_hash() -> None:
    with pytest.raises(ValidationError) as error:
        ImportDocument(
            source_document_id=DOCUMENT_ID,
            import_batch_id=BATCH_ID,
            source_type=SourceType.OCR_PDF,
            source_filename="scan.pdf",
            file_hash="invalid",
            mime_type="application/pdf",
            byte_size=100,
            uploaded_at=datetime(2026, 8, 4),
        )

    assert error.value.error_count() == 2


def test_valid_pdf_document_is_accepted() -> None:
    document = ImportDocument(
        source_document_id=DOCUMENT_ID,
        import_batch_id=BATCH_ID,
        source_type=SourceType.DIGITAL_PDF,
        source_filename="statement.pdf",
        file_hash=HASH,
        mime_type="application/pdf",
        byte_size=2048,
        uploaded_at=UPLOADED_AT,
    )

    assert document.mime_type == "application/pdf"


def test_candidate_preserves_confidence_issues_and_confirmation() -> None:
    payload = valid_candidate_payload()
    payload.update(
        field_confidences=(
            FieldConfidence(
                field=TransactionField.AMOUNT,
                confidence=0.81,
                raw_value="12.SO",
            ),
        ),
        issues=(
            ImportIssue(
                code="low_ocr_confidence",
                message="Confirm the recognised amount",
                severity=IssueSeverity.WARNING,
                field=TransactionField.AMOUNT,
            ),
        ),
        review_status=ReviewStatus.CONFIRMED,
        user_confirmed=True,
        reviewed_at=UPLOADED_AT,
    )

    candidate = ImportCandidate.model_validate(payload)

    assert candidate.user_confirmed is True
    assert candidate.issues[0].field is TransactionField.AMOUNT


def test_csv_candidate_requires_source_row_number() -> None:
    payload = valid_candidate_payload()
    payload["source_row_number"] = None

    with pytest.raises(ValidationError, match="source row number"):
        ImportCandidate.model_validate(payload)


def test_field_confidence_entries_must_be_unique() -> None:
    payload = valid_candidate_payload()
    payload["field_confidences"] = (
        FieldConfidence(field=TransactionField.AMOUNT, confidence=0.8),
        FieldConfidence(field=TransactionField.AMOUNT, confidence=0.9),
    )

    with pytest.raises(ValidationError, match="must be unique"):
        ImportCandidate.model_validate(payload)


def test_confirmed_candidate_requires_confirmation_metadata() -> None:
    payload = valid_candidate_payload()
    payload["review_status"] = ReviewStatus.CONFIRMED

    with pytest.raises(ValidationError, match="require user confirmation"):
        ImportCandidate.model_validate(payload)


@pytest.mark.parametrize(
    "metadata",
    [
        {"user_confirmed": True},
        {"reviewed_at": UPLOADED_AT},
    ],
)
def test_unconfirmed_candidate_rejects_confirmation_metadata(
    metadata: dict[str, object],
) -> None:
    payload = valid_candidate_payload()
    payload.update(metadata)

    with pytest.raises(ValidationError, match="only valid for confirmed"):
        ImportCandidate.model_validate(payload)


def test_candidate_requires_preserved_raw_payload() -> None:
    payload = valid_candidate_payload()
    payload["raw_payload"] = {}

    with pytest.raises(ValidationError):
        ImportCandidate.model_validate(payload)
