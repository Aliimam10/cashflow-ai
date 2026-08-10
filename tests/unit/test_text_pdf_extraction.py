"""Tests for safe embedded-text PDF statement extraction."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, cast

import pdfplumber
import pymupdf
import pytest

from cashflow_ai.imports import (
    PDF_EXTRACTOR_IDENTITY,
    PdfImportError,
    PdfImportErrorCode,
    calculate_file_hash,
    extract_text_pdf,
)
from cashflow_ai.imports.text_pdf import _rows_from_tables, _rows_from_text
from cashflow_ai.schemas import (
    ExtractionMethod,
    PdfExtractionLayout,
    ReviewStatus,
)


def _new_document() -> Any:
    return pymupdf.open()  # type: ignore[no-untyped-call]


def _finish(document: Any, **options: Any) -> bytes:
    content = cast(bytes, document.tobytes(**options))
    document.close()
    return content


def text_pdf(*pages: tuple[str, ...]) -> bytes:
    document = _new_document()
    for lines in pages:
        page = document.new_page(width=595, height=842)
        for index, line in enumerate(lines):
            page.insert_text((45, 45 + index * 22), line, fontsize=10)
    return _finish(document)


def _draw_table(
    page: Any,
    rows: tuple[tuple[str, ...], ...],
    *,
    top: float = 145,
) -> None:
    x_positions = (30, 115, 320, 395, 470, 565)
    row_height = 34
    bottom = top + row_height * len(rows)
    for x_position in x_positions:
        page.draw_line((x_position, top), (x_position, bottom))
    for row_index in range(len(rows) + 1):
        y_position = top + row_index * row_height
        page.draw_line((x_positions[0], y_position), (x_positions[-1], y_position))
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            if not value:
                continue
            x_position = x_positions[column_index] + 3
            y_position = top + row_index * row_height + 13
            for line_index, line in enumerate(value.split("\n")):
                page.insert_text(
                    (x_position, y_position + line_index * 11),
                    line,
                    fontsize=8,
                )


def table_statement_pdf() -> bytes:
    document = _new_document()
    first = document.new_page(width=595, height=842)
    first.insert_text((40, 40), "Fictional Example Bank", fontsize=11)
    first.insert_text(
        (40, 65),
        "Statement period: 01 July 2026 to 31 July 2026",
        fontsize=10,
    )
    first.insert_text((40, 90), "Opening balance: GBP 1,000.00", fontsize=10)
    _draw_table(
        first,
        (
            ("Date", "Description", "Debit", "Credit", "Balance"),
            ("01/07/2026", "CARD PAYMENT", "4.50", "", "995.50"),
            ("", "SYNTHETIC CAFE", "", "", ""),
            ("Date", "Description", "Debit", "Credit", "Balance"),
            ("02/07/2026", "SYNTHETIC SALARY", "", "1000.00", "1995.50"),
        ),
    )
    first.insert_text((270, 810), "Page 1 of 2", fontsize=9)

    second = document.new_page(width=595, height=842)
    second.insert_text((40, 40), "Fictional Example Bank", fontsize=11)
    second.insert_text((40, 70), "Closing balance: GBP 1,987.50", fontsize=10)
    _draw_table(
        second,
        (
            ("Date", "Description", "Debit", "Credit", "Balance"),
            ("31/02/2026", "SYNTHETIC BROKEN DATE", "8.00", "", "1987.50"),
        ),
        top=110,
    )
    second.insert_text((270, 810), "Page 2 of 2", fontsize=9)
    return _finish(document)


def generic_statement_pdf() -> bytes:
    return text_pdf(
        (
            "Fictional Generic Statement",
            "Statement period: 01/08/2026 - 31/08/2026",
            "Opening balance: GBP 500.00",
            "Date | Description | Amount | Balance",
            "01/08/2026 | SYNTHETIC SHOP | -10.00 | 490.00",
            " | SECOND DESCRIPTION LINE | | ",
            "Date | Description | Amount | Balance",
            "02/08/2026 | SYNTHETIC REFUND | 5.00 | 495.00",
            "Closing balance: GBP 495.00",
            "Page 1 of 1",
        )
    )


def test_table_pdf_extracts_metadata_candidates_and_page_lineage() -> None:
    content = table_statement_pdf()

    preview = extract_text_pdf(
        content,
        "../../synthetic statement.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )

    assert preview.source_filename == "synthetic statement.pdf"
    assert preview.file_hash == calculate_file_hash(content)
    assert preview.page_count == 2
    assert preview.layouts == {PdfExtractionLayout.TABLE}
    assert all(page.tables_found == 1 for page in preview.pages)
    assert preview.statement_coverage is not None
    assert preview.statement_coverage.statement_start_date == date(2026, 7, 1)
    assert preview.statement_coverage.statement_end_date == date(2026, 7, 31)
    assert preview.statement_balances is not None
    assert preview.statement_balances.opening_balance == Decimal("1000.00")
    assert preview.statement_balances.closing_balance == Decimal("1987.50")
    assert len(preview.candidates) == 3
    assert preview.candidates[0].original.description_text == (
        "CARD PAYMENT\nSYNTHETIC CAFE"
    )
    assert preview.candidates[0].draft.description == "SYNTHETIC CAFE"
    assert preview.candidates[0].draft.amount == Decimal("-4.50")
    assert preview.candidates[1].draft.amount == Decimal("1000.00")
    assert preview.candidates[0].provenance.method is ExtractionMethod.PDF_TABLE
    assert preview.candidates[0].provenance.parser == PDF_EXTRACTOR_IDENTITY
    assert preview.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW
    assert preview.candidates[0].canonical_fingerprint is not None
    assert preview.candidates[2].source_identity.page_number == 2
    assert preview.candidates[2].source_identity.page_record_number == 1
    assert preview.candidates[2].canonical_fingerprint is None
    assert preview.candidates[2].issues[0].code == "invalid_date"
    assert preview.requires_user_confirmation is True


def test_generic_text_fallback_joins_descriptions_and_removes_headers_pages() -> None:
    preview = extract_text_pdf(
        generic_statement_pdf(),
        "generic.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )

    assert preview.layouts == {PdfExtractionLayout.GENERIC_TEXT}
    assert len(preview.candidates) == 2
    assert preview.candidates[0].original.description_text == (
        "SYNTHETIC SHOP\nSECOND DESCRIPTION LINE"
    )
    assert preview.candidates[0].provenance.method is ExtractionMethod.PDF_TEXT
    assert preview.statement_balances is not None
    assert preview.statement_balances.closing_balance == Decimal("495.00")
    assert "generic_text_fallback" in {issue.code for issue in preview.document_issues}


def test_headerless_generic_text_and_missing_metadata_are_reviewable() -> None:
    content = text_pdf(
        (
            "Fictional Statement Transactions",
            "01/09/2026  SYNTHETIC ITEM  -2.00  98.00",
        )
    )

    preview = extract_text_pdf(
        content,
        "headerless.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )

    assert len(preview.candidates) == 1
    assert preview.candidates[0].draft.amount == Decimal("-2.00")
    assert preview.statement_coverage is None
    assert preview.statement_balances is None
    assert {issue.code for issue in preview.document_issues} >= {
        "statement_period_not_found",
        "statement_balances_not_found",
    }


def test_alternative_period_and_invalid_metadata_are_reported() -> None:
    valid_alternative = text_pdf(
        (
            "Fictional Statement",
            "From 01 Sep 2026 to 30 Sep 2026",
            "Opening balance: GBP 100.00",
            "01/09/2026  SYNTHETIC ITEM  -2.00  98.00",
        )
    )
    valid_preview = extract_text_pdf(
        valid_alternative,
        "alternative-period.pdf",
        mime_type="application/pdf",
        account_id=" account-1 ",
    )
    assert valid_preview.statement_coverage is not None
    assert valid_preview.statement_coverage.statement_end_date == date(2026, 9, 30)
    assert valid_preview.candidates[0].draft.account_id == "account-1"

    invalid_metadata = text_pdf(
        (
            "Fictional Statement",
            "Statement period: 30 Sep 2026 to 01 Sep 2026",
            "Opening balance: not-money",
            "01/09/2026  SYNTHETIC ITEM  -2.00  98.00",
        )
    )
    invalid_preview = extract_text_pdf(
        invalid_metadata,
        "invalid-metadata.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )
    assert invalid_preview.statement_coverage is None
    assert invalid_preview.statement_balances is None
    assert {issue.code for issue in invalid_preview.document_issues} >= {
        "invalid_statement_period",
        "invalid_opening_balance",
    }


def test_table_and_text_row_cleanup_handles_unsupported_structures() -> None:
    tables = (
        (),
        (("Unrecognised", "Columns"), ("value", "value")),
        (
            ("Date", "Description", "Amount", "Balance"),
            ("", "", "", ""),
            ("Page 1 of 1", "", "", ""),
            ("2026-07-01", "FIRST", "-1.00", "99.00"),
        ),
        (
            ("Date", "Description", "Amount", "Balance"),
            ("2026-07-02", "SECOND", "-2.00", "97.00"),
        ),
    )

    rows = _rows_from_tables(cast(Any, tables), 1)

    assert [row.page_record_number for row in rows] == [1, 2]
    assert [row.values[1] for row in rows] == ["FIRST", "SECOND"]
    text_rows = _rows_from_text(
        "2026-07-01 | TOO | MANY | -1.00 | COLUMNS\n2026-07-02 | VALID | -2.00 | 97.00",
        1,
    )
    assert len(text_rows) == 1
    assert text_rows[0].values[1] == "VALID"


@pytest.mark.parametrize(
    ("filename", "mime_type", "account_id", "code"),
    [
        ("", "application/pdf", "account-1", PdfImportErrorCode.INVALID_FILENAME),
        (
            "statement.txt",
            "application/pdf",
            "account-1",
            PdfImportErrorCode.UNSUPPORTED_FILE_TYPE,
        ),
        (
            "statement.pdf",
            "text/plain",
            "account-1",
            PdfImportErrorCode.UNSUPPORTED_MIME_TYPE,
        ),
        (
            "statement.pdf",
            "application/pdf",
            "",
            PdfImportErrorCode.INVALID_ACCOUNT,
        ),
    ],
)
def test_filename_mime_and_account_validation(
    filename: str,
    mime_type: str,
    account_id: str,
    code: PdfImportErrorCode,
) -> None:
    with pytest.raises(PdfImportError) as error:
        extract_text_pdf(
            generic_statement_pdf(),
            filename,
            mime_type=mime_type,
            account_id=account_id,
        )
    assert error.value.code is code


@pytest.mark.parametrize(
    ("filename", "account_id", "code"),
    [
        (".", "account-1", PdfImportErrorCode.INVALID_FILENAME),
        ("x" * 252 + ".pdf", "account-1", PdfImportErrorCode.INVALID_FILENAME),
        ("statement.pdf", "x" * 256, PdfImportErrorCode.INVALID_ACCOUNT),
    ],
)
def test_filename_and_account_length_limits(
    filename: str,
    account_id: str,
    code: PdfImportErrorCode,
) -> None:
    with pytest.raises(PdfImportError) as error:
        extract_text_pdf(
            generic_statement_pdf(),
            filename,
            mime_type="application/pdf",
            account_id=account_id,
        )
    assert error.value.code is code


@pytest.mark.parametrize(
    ("content", "max_bytes", "code"),
    [
        (b"", 100, PdfImportErrorCode.EMPTY_FILE),
        (b"%PDF-too-large", 2, PdfImportErrorCode.FILE_TOO_LARGE),
        (b"not a PDF", 100, PdfImportErrorCode.INVALID_SIGNATURE),
        (b"%PDF-malformed", 100, PdfImportErrorCode.MALFORMED_PDF),
    ],
)
def test_empty_oversized_unsigned_and_malformed_files_are_rejected(
    content: bytes,
    max_bytes: int,
    code: PdfImportErrorCode,
) -> None:
    with pytest.raises(PdfImportError) as error:
        extract_text_pdf(
            content,
            "statement.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            max_bytes=max_bytes,
        )
    assert error.value.code is code


@pytest.mark.parametrize(
    ("limits", "code"),
    [
        ({"max_bytes": 0}, PdfImportErrorCode.INVALID_LIMIT),
        ({"max_pages": 0}, PdfImportErrorCode.INVALID_LIMIT),
        ({"min_embedded_characters": 0}, PdfImportErrorCode.INVALID_LIMIT),
        ({"max_pages": 1}, PdfImportErrorCode.TOO_MANY_PAGES),
    ],
)
def test_configured_pdf_limits_are_enforced(
    limits: dict[str, int],
    code: PdfImportErrorCode,
) -> None:
    with pytest.raises(PdfImportError) as error:
        extract_text_pdf(
            table_statement_pdf(),
            "statement.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            **cast(Any, limits),
        )
    assert error.value.code is code


def test_encrypted_and_image_only_pages_are_routed_away_from_text_extraction() -> None:
    document = _new_document()
    page = document.new_page()
    page.insert_text((40, 40), "Private synthetic statement text")
    encrypted = _finish(
        document,
        encryption=cast(Any, pymupdf).PDF_ENCRYPT_AES_256,
        owner_pw="synthetic-owner",
        user_pw="synthetic-user",
    )
    with pytest.raises(PdfImportError) as encrypted_error:
        extract_text_pdf(
            encrypted,
            "encrypted.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )
    assert encrypted_error.value.code is PdfImportErrorCode.ENCRYPTED_PDF

    document = _new_document()
    document.new_page()
    image_only = _finish(document)
    with pytest.raises(PdfImportError) as ocr_error:
        extract_text_pdf(
            image_only,
            "scan.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )
    assert ocr_error.value.code is PdfImportErrorCode.OCR_REQUIRED
    assert ocr_error.value.page_numbers == (1,)


def test_text_size_and_missing_transaction_failures_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cashflow_ai.imports.text_pdf as text_pdf_module

    content = generic_statement_pdf()
    monkeypatch.setattr(text_pdf_module, "MAX_PAGE_TEXT_CHARACTERS", 10)
    with pytest.raises(PdfImportError) as oversized_text:
        extract_text_pdf(
            content,
            "statement.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )
    assert oversized_text.value.code is PdfImportErrorCode.PAGE_TEXT_TOO_LARGE
    assert oversized_text.value.page_numbers == (1,)

    monkeypatch.setattr(text_pdf_module, "MAX_PAGE_TEXT_CHARACTERS", 1_000_000)
    no_rows = text_pdf(("Fictional statement with embedded words but no rows",))
    with pytest.raises(PdfImportError) as missing_rows:
        extract_text_pdf(
            no_rows,
            "statement.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )
    assert missing_rows.value.code is PdfImportErrorCode.NO_TRANSACTIONS


def test_pdfplumber_failure_uses_generic_text_without_losing_the_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_table_reader(stream: object) -> None:
        del stream
        raise RuntimeError("synthetic table failure")

    monkeypatch.setattr(pdfplumber, "open", fail_table_reader)
    preview = extract_text_pdf(
        generic_statement_pdf(),
        "fallback.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )

    assert {issue.code for issue in preview.document_issues} >= {
        "table_extraction_failed",
        "generic_text_fallback",
    }


def test_pdfplumber_failure_without_generic_rows_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_table_reader(stream: object) -> None:
        del stream
        raise RuntimeError("synthetic table failure")

    monkeypatch.setattr(pdfplumber, "open", fail_table_reader)
    content = text_pdf(("Fictional statement with embedded words but no rows",))
    with pytest.raises(PdfImportError) as error:
        extract_text_pdf(
            content,
            "fallback.pdf",
            mime_type="application/pdf",
            account_id="account-1",
        )
    assert error.value.code is PdfImportErrorCode.NO_TRANSACTIONS


def test_empty_pdfplumber_text_uses_pymupdf_page_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def extract_tables(self) -> list[object]:
            return []

        def extract_text(self, *, layout: bool) -> None:
            assert layout is True
            return None

    class FakePdf:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        def __enter__(self) -> FakePdf:
            return self

        def __exit__(self, *exc_info: object) -> None:
            del exc_info

    monkeypatch.setattr(pdfplumber, "open", lambda stream: FakePdf())
    preview = extract_text_pdf(
        generic_statement_pdf(),
        "fallback.pdf",
        mime_type="application/pdf",
        account_id="account-1",
    )
    assert len(preview.candidates) == 2
