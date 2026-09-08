"""Focused branch and failure tests for the digital-PDF API services."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

import cashflow_ai.api.services as services
from cashflow_ai.api.services import ApiServiceError, ApiServiceErrorCode
from cashflow_ai.imports import (
    PdfImportError,
    PdfImportErrorCode,
    SpatialPdfError,
    SpatialPdfErrorCode,
    SpatialPdfResult,
    SpatialPdfState,
    calculate_file_hash,
)
from cashflow_ai.schemas.api import PdfSourceType
from cashflow_ai.schemas.pdf_api import (
    DigitalPdfColumnMapping,
    DigitalPdfReviewState,
)
from cashflow_ai.schemas.transactions import Currency


def _skip_account_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        services, "_require_import_account", lambda *args, **kwargs: None
    )


def test_mapping_preview_returns_none_without_a_reconstructed_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services,
        "reconstruct_spatial_pdf",
        lambda content: SpatialPdfResult(
            state=SpatialPdfState.UNSUPPORTED_LAYOUT,
            page_count=1,
            reason_code="no_table",
        ),
    )

    assert services._mapping_preview(b"%PDF", file_hash="a" * 64) is None


def test_mapping_contract_translates_reconstruction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"%PDF-synthetic"
    mapping = DigitalPdfColumnMapping(
        file_hash=calculate_file_hash(content),
        structure_digest="b" * 64,
        transaction_date="column_1",
        description="column_2",
        signed_amount="column_3",
    )

    def fail_reconstruction(content: bytes) -> SpatialPdfResult:
        del content
        raise SpatialPdfError(
            SpatialPdfErrorCode.MALFORMED_PDF,
            "the PDF cannot be reconstructed safely",
        )

    monkeypatch.setattr(services, "reconstruct_spatial_pdf", fail_reconstruction)

    with pytest.raises(ApiServiceError) as captured:
        services._spatial_mapping_from_contract(content, mapping)

    assert captured.value.code is ApiServiceErrorCode.INVALID_PDF_MAPPING


def test_digital_review_preserves_non_mapping_extraction_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_account_check(monkeypatch)

    def fail_extraction(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise PdfImportError(
            PdfImportErrorCode.MALFORMED_PDF,
            "the PDF is malformed",
        )

    monkeypatch.setattr(services, "extract_text_pdf", fail_extraction)

    with pytest.raises(PdfImportError) as captured:
        services.review_digital_pdf_statement(
            MagicMock(),
            b"%PDF",
            "synthetic.pdf",
            mime_type="application/pdf",
            account_id="synthetic-account",
            account_currency=Currency.GBP,
        )

    assert captured.value.code is PdfImportErrorCode.MALFORMED_PDF


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [
        (SpatialPdfState.UNSUPPORTED_LAYOUT, "changed_layout"),
        (SpatialPdfState.READY, "unsafe_transaction_row_accounting"),
    ],
)
def test_digital_review_falls_back_when_spatial_rows_are_not_safe(
    state: SpatialPdfState,
    expected_reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_account_check(monkeypatch)

    def no_transactions(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise PdfImportError(
            PdfImportErrorCode.NO_TRANSACTIONS,
            "no safe transactions were found",
        )

    monkeypatch.setattr(services, "extract_text_pdf", no_transactions)
    monkeypatch.setattr(
        services,
        "reconstruct_spatial_pdf",
        lambda content, mapping=None: SpatialPdfResult(
            state=state,
            page_count=1,
            reason_code="changed_layout",
        ),
    )

    result = services.review_digital_pdf_statement(
        MagicMock(),
        b"%PDF",
        "synthetic.pdf",
        mime_type="application/pdf",
        account_id="synthetic-account",
        account_currency=Currency.GBP,
    )

    assert result.state is DigitalPdfReviewState.UNSUPPORTED_LAYOUT
    assert result.reason_code == expected_reason


def test_digital_review_converts_spatial_failure_to_safe_csv_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_account_check(monkeypatch)

    def no_transactions(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise PdfImportError(
            PdfImportErrorCode.NO_TRANSACTIONS,
            "no safe transactions were found",
        )

    def fail_reconstruction(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise SpatialPdfError(
            SpatialPdfErrorCode.PAGE_TOO_COMPLEX,
            "the page is too complex",
        )

    monkeypatch.setattr(services, "extract_text_pdf", no_transactions)
    monkeypatch.setattr(services, "reconstruct_spatial_pdf", fail_reconstruction)

    result = services.review_digital_pdf_statement(
        MagicMock(),
        b"%PDF",
        "synthetic.pdf",
        mime_type="application/pdf",
        account_id="synthetic-account",
        account_currency=Currency.GBP,
    )

    assert result.state is DigitalPdfReviewState.UNSUPPORTED_LAYOUT
    assert result.reason_code == "unsafe_spatial_reconstruction"
    assert result.recommended_format == "csv"


def test_internal_legacy_wrapper_still_supports_digital_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = MagicMock()
    review = MagicMock()
    preview_service = MagicMock(return_value=preview)
    review_service = MagicMock(return_value=review)
    monkeypatch.setattr(services, "preview_text_statement", preview_service)
    monkeypatch.setattr(services, "prepare_statement_review", review_service)

    result = services.prepare_pdf_statement_review(
        MagicMock(),
        b"%PDF",
        "synthetic.pdf",
        mime_type="application/pdf",
        source_type=PdfSourceType.DIGITAL_PDF,
        account_id="synthetic-account",
        account_currency=Currency.GBP,
        ocr_confidence_threshold=0.85,
        engine_factory=MagicMock(),
    )

    assert result is review
    preview_service.assert_called_once()
    review_service.assert_called_once_with(
        preview,
        ocr_confidence_threshold=0.85,
    )
