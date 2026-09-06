"""Tests for the digital-PDF adapter's fail-closed spatial fallback."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pdfplumber
import pymupdf
import pytest

import cashflow_ai.imports.spatial_pdf as spatial_pdf_module
import cashflow_ai.imports.text_pdf as text_pdf_module
from cashflow_ai.imports.spatial_pdf import (
    SpatialColumnMapping,
    SpatialPdfError,
    SpatialPdfErrorCode,
    SpatialPdfResult,
    SpatialPdfState,
)
from cashflow_ai.imports.text_pdf import (
    SPATIAL_PDF_EXTRACTOR_IDENTITY,
    PdfImportError,
    PdfImportErrorCode,
    extract_text_pdf,
)


def _finish(document: Any) -> bytes:
    content = cast(bytes, document.tobytes())
    document.close()
    return content


def _write_row(
    page: Any,
    *,
    y: float,
    values: tuple[str, ...],
    positions: tuple[float, ...],
) -> None:
    for value, x in zip(values, positions, strict=True):
        if value:
            page.insert_text((x, y), value, fontsize=9)


def _spatial_statement(*, headers: bool) -> bytes:
    document: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    positions = (35.0, 145.0, 355.0, 475.0)
    first = document.new_page(width=595, height=842)
    first.insert_text((35, 35), "Fictional statement", fontsize=10)
    first.insert_text(
        (35, 55),
        "Statement period: 01 August 2026 to 31 August 2026",
        fontsize=9,
    )
    first.insert_text((35, 72), "Opening balance: GBP 500.00", fontsize=9)
    if headers:
        _write_row(
            first,
            y=100,
            values=("Date", "Description", "Amount", "Balance"),
            positions=positions,
        )
    _write_row(
        first,
        y=125,
        values=("01/08/2026", "SYNTHETIC SHOP", "-10.00", "490.00"),
        positions=positions,
    )
    _write_row(
        first,
        y=138,
        values=("", "CONTINUED DESCRIPTION", "", ""),
        positions=positions,
    )

    second = document.new_page(width=595, height=842)
    if headers:
        _write_row(
            second,
            y=80,
            values=("Date", "Description", "Amount", "Balance"),
            positions=positions,
        )
    _write_row(
        second,
        y=105,
        values=("02/08/2026", "SYNTHETIC REFUND", "+5.00", "495.00"),
        positions=positions,
    )
    second.insert_text((35, 140), "Closing balance: GBP 495.00", fontsize=9)
    return _finish(document)


def _debit_credit_statement() -> bytes:
    document: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    positions = (35.0, 135.0, 355.0, 465.0)
    _write_row(
        page,
        y=90,
        values=("Date", "Description", "Debit", "Credit"),
        positions=positions,
    )
    _write_row(
        page,
        y=115,
        values=("01/08/2026", "SYNTHETIC RENT", "25.00", ""),
        positions=positions,
    )
    return _finish(document)


def _offsetting_rows_statement() -> bytes:
    document: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    positions = (35.0, 135.0, 340.0, 410.0, 485.0)
    page.insert_text((35, 35), "Fictional statement", fontsize=10)
    page.insert_text((35, 55), "Opening balance: GBP 100.00", fontsize=9)
    _write_row(
        page,
        y=90,
        values=("Date", "Description", "Debit", "Credit", "Balance"),
        positions=positions,
    )
    _write_row(
        page,
        y=115,
        values=("01/08/2026", "SYNTHETIC ITEM", "5.00", "", "95.00"),
        positions=positions,
    )
    _write_row(
        page,
        y=140,
        values=("02/08/2026", "OFFSET DEBIT", "10.00", "", "85.00"),
        positions=positions,
    )
    _write_row(
        page,
        y=165,
        values=("03/08/2026", "OFFSET CREDIT", "", "10.00", "95.00"),
        positions=positions,
    )
    page.insert_text((35, 195), "Closing balance: GBP 95.00", fontsize=9)
    return _finish(document)


def _missing_amount_statement() -> bytes:
    document: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    positions = (35.0, 145.0, 355.0, 475.0)
    page.insert_text((35, 35), "Fictional statement", fontsize=10)
    page.insert_text((35, 55), "Opening balance: GBP 100.00", fontsize=9)
    _write_row(
        page,
        y=90,
        values=("Date", "Description", "Amount", "Balance"),
        positions=positions,
    )
    _write_row(
        page,
        y=115,
        values=("01/08/2026", "SYNTHETIC ITEM", "-10.00", "90.00"),
        positions=positions,
    )
    _write_row(
        page,
        y=140,
        values=("02/08/2026", "BROKEN SOURCE ROW", "", ""),
        positions=positions,
    )
    page.insert_text((35, 170), "Closing balance: GBP 90.00", fontsize=9)
    return _finish(document)


class _EmptyLayoutPage:
    def extract_tables(self) -> list[object]:
        return []

    def extract_text(self, *, layout: bool) -> str:
        assert layout is True
        return "layout intentionally unavailable"


class _EmptyLayoutPdf:
    def __init__(self, page_count: int) -> None:
        self.pages = [_EmptyLayoutPage() for _ in range(page_count)]

    def __enter__(self) -> _EmptyLayoutPdf:
        return self

    def __exit__(self, *exc_info: object) -> None:
        del exc_info


class _PartiallyParsedPage:
    def extract_tables(self) -> list[list[tuple[str, ...]]]:
        return [
            [
                ("Date", "Description", "Debit", "Credit", "Balance"),
                ("01/08/2026", "SYNTHETIC ITEM", "5.00", "", "95.00"),
            ]
        ]

    def extract_text(self, *, layout: bool) -> str:
        assert layout is True
        return "\n".join(
            (
                "01/08/2026  SYNTHETIC ITEM  5.00  95.00",
                "02/08/2026  OFFSET DEBIT  10.00  85.00",
                "03/08/2026  OFFSET CREDIT  10.00  95.00",
            )
        )


class _PartiallyParsedPdf:
    def __init__(self) -> None:
        self.pages = [_PartiallyParsedPage()]

    def __enter__(self) -> _PartiallyParsedPdf:
        return self

    def __exit__(self, *exc_info: object) -> None:
        del exc_info


def _disable_standard_row_detection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_count: int = 2,
) -> None:
    monkeypatch.setattr(
        pdfplumber,
        "open",
        lambda stream: _EmptyLayoutPdf(page_count),
    )


def test_spatial_fallback_preserves_values_lineage_and_parser_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_standard_row_detection(monkeypatch)

    preview = extract_text_pdf(
        _spatial_statement(headers=True),
        "spatial.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )

    assert len(preview.candidates) == 2
    assert preview.candidates[0].original.description_text == (
        "SYNTHETIC SHOP\nCONTINUED DESCRIPTION"
    )
    assert preview.candidates[0].original.signed_amount_text == "-10.00"
    assert preview.candidates[0].draft.balance_after is not None
    assert preview.candidates[0].source_identity.page_number == 1
    assert preview.candidates[0].source_identity.page_record_number == 1
    assert preview.candidates[1].source_identity.page_number == 2
    assert preview.candidates[1].source_identity.page_record_number == 1
    assert all(
        candidate.provenance.parser == SPATIAL_PDF_EXTRACTOR_IDENTITY
        for candidate in preview.candidates
    )
    assert "spatial_reconstruction_fallback" in {
        issue.code for issue in preview.document_issues
    }
    assert preview.statement_coverage is not None
    assert preview.statement_balances is not None


def test_headerless_spatial_layout_requires_then_accepts_explicit_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_standard_row_detection(monkeypatch)
    content = _spatial_statement(headers=False)

    with pytest.raises(PdfImportError) as unresolved:
        extract_text_pdf(
            content,
            "headerless.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )
    assert unresolved.value.code is PdfImportErrorCode.NO_TRANSACTIONS
    assert "mapping" in str(unresolved.value)

    preview = extract_text_pdf(
        content,
        "headerless.pdf",
        mime_type="application/pdf",
        account_id="account-1",
        spatial_mapping=SpatialColumnMapping(
            transaction_date="column_1",
            description="column_2",
            signed_amount="column_3",
        ),
    )

    assert [
        candidate.original.signed_amount_text for candidate in preview.candidates
    ] == [
        "-10.00",
        "+5.00",
    ]
    assert all(
        candidate.provenance.parser == SPATIAL_PDF_EXTRACTOR_IDENTITY
        for candidate in preview.candidates
    )
    assert preview.candidates[0].original.running_balance_text is None
    assert "490.00" in {
        field.value for field in preview.candidates[0].original.raw_fields
    }


def test_spatial_fallback_supports_separate_debit_and_credit_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_standard_row_detection(monkeypatch, page_count=1)

    preview = extract_text_pdf(
        _debit_credit_statement(),
        "debit-credit.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )

    candidate = preview.candidates[0]
    assert candidate.original.signed_amount_text is None
    assert candidate.original.debit_amount_text == "25.00"
    assert candidate.original.credit_amount_text == ""
    assert candidate.original.running_balance_text is None


def test_spatial_failures_and_empty_ready_results_remain_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_standard_row_detection(monkeypatch, page_count=1)
    content = _debit_credit_statement()

    def fail_reconstruction(*args: object, **kwargs: object) -> SpatialPdfResult:
        del args, kwargs
        raise SpatialPdfError(
            SpatialPdfErrorCode.PAGE_TOO_COMPLEX,
            "safe synthetic failure",
        )

    monkeypatch.setattr(text_pdf_module, "reconstruct_spatial_pdf", fail_reconstruction)
    with pytest.raises(PdfImportError) as reconstruction_failure:
        extract_text_pdf(
            content,
            "failed.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )
    assert reconstruction_failure.value.code is PdfImportErrorCode.NO_TRANSACTIONS

    monkeypatch.setattr(
        text_pdf_module,
        "reconstruct_spatial_pdf",
        lambda *args, **kwargs: SpatialPdfResult(
            state=SpatialPdfState.READY,
            page_count=1,
            reason_code="synthetic_empty_ready",
        ),
    )
    with pytest.raises(PdfImportError) as empty_result:
        extract_text_pdf(
            content,
            "empty.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )
    assert empty_result.value.code is PdfImportErrorCode.NO_TRANSACTIONS


def test_partial_parser_cannot_hide_offsetting_debit_and_credit_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _offsetting_rows_statement()
    monkeypatch.setattr(pdfplumber, "open", lambda stream: _PartiallyParsedPdf())

    preview = extract_text_pdf(
        content,
        "offsetting-lines.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )

    assert [candidate.draft.amount for candidate in preview.candidates] == [
        -5,
        -10,
        10,
    ]
    assert "source_row_accounting_fallback" in {
        issue.code for issue in preview.document_issues
    }
    assert all(
        candidate.provenance.parser == SPATIAL_PDF_EXTRACTOR_IDENTITY
        for candidate in preview.candidates
    )


def test_spatial_reconstruction_must_account_for_every_detected_source_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _offsetting_rows_statement()
    complete = spatial_pdf_module.reconstruct_spatial_pdf(content)
    assert complete.state is SpatialPdfState.READY
    assert len(complete.records) == 3
    incomplete = replace(complete, records=complete.records[:1])
    monkeypatch.setattr(pdfplumber, "open", lambda stream: _PartiallyParsedPdf())
    monkeypatch.setattr(
        text_pdf_module,
        "reconstruct_spatial_pdf",
        lambda *args, **kwargs: incomplete,
    )

    with pytest.raises(PdfImportError) as error:
        extract_text_pdf(
            content,
            "incomplete-spatial.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )

    assert error.value.code is PdfImportErrorCode.NO_TRANSACTIONS
    assert "every transaction-like" in str(error.value)


def test_equal_row_counts_with_conflicting_source_evidence_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConflictingPage(_PartiallyParsedPage):
        def extract_tables(self) -> list[list[tuple[str, ...]]]:
            return [
                [
                    ("Date", "Description", "Debit", "Credit", "Balance"),
                    ("01/08/2026", "SYNTHETIC ITEM", "5.00", "", "95.00"),
                    ("02/08/2026", "OFFSET DEBIT", "10.00", "", "85.00"),
                    ("04/08/2026", "CONFLICTING ROW", "7.00", "", "78.00"),
                ]
            ]

    class ConflictingPdf:
        def __init__(self) -> None:
            self.pages = [ConflictingPage()]

        def __enter__(self) -> ConflictingPdf:
            return self

        def __exit__(self, *exc_info: object) -> None:
            del exc_info

    monkeypatch.setattr(pdfplumber, "open", lambda stream: ConflictingPdf())

    with pytest.raises(PdfImportError) as error:
        extract_text_pdf(
            _offsetting_rows_statement(),
            "conflicting-evidence.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )

    assert error.value.code is PdfImportErrorCode.NO_TRANSACTIONS
    assert "every transaction-like" in str(error.value)


def test_dated_source_row_without_an_amount_cannot_be_silently_omitted() -> None:
    with pytest.raises(PdfImportError) as error:
        extract_text_pdf(
            _missing_amount_statement(),
            "missing-amount.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )

    assert error.value.code is PdfImportErrorCode.NO_TRANSACTIONS
