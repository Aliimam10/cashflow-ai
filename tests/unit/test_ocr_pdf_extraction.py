"""Tests for local scanned-PDF OCR extraction."""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO
from typing import Any, cast

import pymupdf
import pytesseract  # type: ignore[import-untyped]
import pytest
from PIL import Image, ImageDraw

from cashflow_ai.imports import (
    OCR_EXTRACTOR_IDENTITY,
    OcrWord,
    PdfImportError,
    PdfImportErrorCode,
    PytesseractOcrEngine,
    calculate_file_hash,
    extract_ocr_pdf,
)
from cashflow_ai.imports.ocr_pdf import (
    _integer_at,
    _matching_line_numbers,
    _open_pdf,
    _preprocess_page,
)
from cashflow_ai.imports.text_pdf import _rows_from_text
from cashflow_ai.schemas import (
    ExtractionMethod,
    OcrLineExtraction,
    ReviewStatus,
    SourceType,
)


def _new_document() -> Any:
    return pymupdf.open()  # type: ignore[no-untyped-call]


def _finish(document: Any, **options: Any) -> bytes:
    content = cast(bytes, document.tobytes(**options))
    document.close()
    return content


def scanned_pdf(*, pages: int = 1, low_contrast: bool = False) -> bytes:
    background = 230 if low_contrast else 255
    foreground = 180 if low_contrast else 0
    image = Image.new("RGB", (240, 320), (background, background, background))
    drawing = ImageDraw.Draw(image)
    drawing.text((15, 20), "Fictional scanned statement", fill=foreground)
    output = BytesIO()
    image.save(output, format="PNG")
    image.close()

    document = _new_document()
    for _ in range(pages):
        page = document.new_page(width=240, height=320)
        page.insert_image(page.rect, stream=output.getvalue())
    return _finish(document)


def digital_pdf() -> bytes:
    document = _new_document()
    page = document.new_page(width=240, height=320)
    page.insert_text((20, 30), "Fictional PDF with usable embedded statement text")
    return _finish(document)


def ocr_words(
    lines: Sequence[str],
    *,
    confidence: float = 0.9,
) -> tuple[OcrWord, ...]:
    return tuple(
        OcrWord(
            text=text,
            confidence=confidence,
            block_number=1,
            paragraph_number=1,
            line_number=line_number,
        )
        for line_number, text in enumerate(lines, start=1)
    )


TRANSACTION_LINES = (
    "Fictional Example Bank",
    "Date | Description | Amount | Balance",
    "01/07/2026 | SYNTHETIC SHOP | -4.50 | 95.50",
    " | SECOND DESCRIPTION LINE | | ",
)


class FakeOcrEngine:
    def __init__(
        self,
        page_words: Sequence[tuple[OcrWord, ...]],
        *,
        orientation: tuple[int, float] | None = None,
        failure: PdfImportError | None = None,
    ) -> None:
        self.page_words = list(page_words)
        self.orientation = orientation
        self.failure = failure
        self.available_checked = False
        self.orientation_modes: list[str] = []
        self.recognised_images: list[Image.Image] = []

    def ensure_available(self) -> None:
        self.available_checked = True

    def detect_orientation(self, image: Image.Image) -> tuple[int, float] | None:
        self.orientation_modes.append(image.mode)
        return self.orientation

    def recognise_words(self, image: Image.Image) -> tuple[OcrWord, ...]:
        self.recognised_images.append(image)
        if self.failure is not None:
            raise self.failure
        return self.page_words.pop(0)


def test_scanned_pdf_is_rendered_preprocessed_and_converted_to_candidates() -> None:
    content = scanned_pdf()
    engine = FakeOcrEngine(
        (ocr_words(TRANSACTION_LINES, confidence=0.84),),
        orientation=(90, 0.72),
    )

    preview = extract_ocr_pdf(
        content,
        "../../synthetic scan.pdf",
        mime_type="application/pdf",
        account_id=" account-1 ",
        engine=cast(Any, engine),
        render_dpi=72,
    )

    assert engine.available_checked is True
    assert engine.orientation_modes == ["L"]
    assert preview.source_filename == "synthetic scan.pdf"
    assert preview.file_hash == calculate_file_hash(content)
    assert preview.page_count == 1
    assert preview.pages[0].rotation_applied_degrees == 90
    assert preview.pages[0].orientation_confidence == 0.72
    assert preview.pages[0].confidence == pytest.approx(0.84)
    assert preview.pages[0].raw_text == "\n".join(TRANSACTION_LINES)
    assert len(preview.candidates) == 1
    candidate = preview.candidates[0]
    assert candidate.original.description_text == (
        "SYNTHETIC SHOP\nSECOND DESCRIPTION LINE"
    )
    assert candidate.draft.amount is not None
    assert str(candidate.draft.amount) == "-4.50"
    assert candidate.draft.account_id == "account-1"
    assert candidate.source_identity.source_type is SourceType.OCR_PDF
    assert candidate.provenance.method is ExtractionMethod.OCR
    assert candidate.provenance.parser == OCR_EXTRACTOR_IDENTITY
    assert candidate.provenance.confidence == pytest.approx(0.84)
    assert candidate.line_numbers == (3, 4)
    assert len(candidate.field_confidences) == 4
    assert candidate.review_status is ReviewStatus.NEEDS_REVIEW
    assert candidate.canonical_fingerprint is not None
    assert {issue.code for issue in preview.document_issues} == {
        "image_only_pdf_detected",
        "statement_period_not_found",
        "statement_balances_not_found",
    }
    assert preview.requires_user_confirmation is True
    assert preview.temporary_artifacts_retained is False
    with pytest.raises(ValueError, match="closed"):
        engine.recognised_images[0].getpixel((0, 0))


def test_low_contrast_page_is_thresholded_without_orientation_result() -> None:
    image = Image.new("RGB", (20, 10), (220, 220, 220))
    engine = FakeOcrEngine(())

    processed, rotation, confidence, threshold_applied = _preprocess_page(
        image,
        cast(Any, engine),
    )

    assert processed.mode == "L"
    assert rotation == 0
    assert confidence is None
    assert threshold_applied is True
    colors = processed.getcolors(maxcolors=256)
    assert colors is not None
    assert {value for _, value in colors} <= {0, 255}
    processed.close()
    image.close()


def test_invalid_ocr_candidate_retains_source_values_and_issue() -> None:
    words = ocr_words(
        (
            "Fictional statement",
            "Date | Description | Amount | Balance",
            "31/02/2026 | SYNTHETIC INVALID | -2.00 | 98.00",
        )
    )
    preview = extract_ocr_pdf(
        scanned_pdf(),
        "invalid.pdf",
        mime_type="application/pdf",
        account_id="account-1",
        engine=cast(Any, FakeOcrEngine((words,))),
        render_dpi=72,
    )

    candidate = preview.candidates[0]
    assert candidate.original.transaction_date_text == "31/02/2026"
    assert candidate.canonical_fingerprint is None
    assert candidate.issues[0].code == "invalid_date"


def test_ocr_extracts_statement_opening_and_closing_balances_for_review() -> None:
    words = ocr_words(
        (
            "Statement period: 01/07/2026 to 31/07/2026",
            "Opening balance: 100.00",
            "Date | Description | Amount | Balance",
            "01/07/2026 | SYNTHETIC ITEM | -2.00 | 98.00",
            "Closing balance: 98.00",
        )
    )
    preview = extract_ocr_pdf(
        scanned_pdf(),
        "balances.pdf",
        mime_type="application/pdf",
        account_id="account-1",
        engine=cast(Any, FakeOcrEngine((words,))),
        render_dpi=72,
    )

    assert preview.statement_balances is not None
    assert preview.statement_coverage is not None
    assert preview.statement_coverage.statement_end_date.isoformat() == "2026-07-31"
    assert str(preview.statement_balances.opening_balance) == "100.00"
    assert str(preview.statement_balances.closing_balance) == "98.00"
    assert "statement_balances_not_found" not in {
        issue.code for issue in preview.document_issues
    }


def test_multi_page_ocr_and_embedded_text_warning() -> None:
    page_words = ocr_words(
        (
            "Fictional statement",
            "2026-07-01 SYNTHETIC ITEM -2.00 98.00",
        )
    )
    preview = extract_ocr_pdf(
        digital_pdf(),
        "digital.pdf",
        mime_type="application/pdf",
        account_id="account-1",
        engine=cast(Any, FakeOcrEngine((page_words,))),
        render_dpi=72,
    )

    assert len(preview.candidates) == 1
    assert preview.candidates[0].line_numbers == (2,)
    assert {issue.code for issue in preview.document_issues} == {
        "ocr_used_with_embedded_text",
        "statement_period_not_found",
        "statement_balances_not_found",
    }


@pytest.mark.parametrize(
    ("filename", "mime_type", "account_id", "limits", "code"),
    [
        ("", "application/pdf", "account-1", {}, PdfImportErrorCode.INVALID_FILENAME),
        (
            "scan.txt",
            "application/pdf",
            "account-1",
            {},
            PdfImportErrorCode.UNSUPPORTED_FILE_TYPE,
        ),
        (
            "scan.pdf",
            "text/plain",
            "account-1",
            {},
            PdfImportErrorCode.UNSUPPORTED_MIME_TYPE,
        ),
        (
            "scan.pdf",
            "application/pdf",
            "",
            {},
            PdfImportErrorCode.INVALID_ACCOUNT,
        ),
        (
            "scan.pdf",
            "application/pdf",
            "account-1",
            {"render_dpi": 71},
            PdfImportErrorCode.INVALID_LIMIT,
        ),
        (
            "scan.pdf",
            "application/pdf",
            "account-1",
            {"render_dpi": 601},
            PdfImportErrorCode.INVALID_LIMIT,
        ),
        (
            "scan.pdf",
            "application/pdf",
            "account-1",
            {"max_render_pixels": 0},
            PdfImportErrorCode.INVALID_LIMIT,
        ),
    ],
)
def test_request_metadata_and_limits_are_validated(
    filename: str,
    mime_type: str,
    account_id: str,
    limits: dict[str, int],
    code: PdfImportErrorCode,
) -> None:
    with pytest.raises(PdfImportError) as error:
        extract_ocr_pdf(
            scanned_pdf(),
            filename,
            mime_type=mime_type,
            account_id=account_id,
            engine=cast(Any, FakeOcrEngine(())),
            **cast(Any, limits),
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
        extract_ocr_pdf(
            content,
            "scan.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            engine=cast(Any, FakeOcrEngine(())),
            max_bytes=max_bytes,
        )
    assert error.value.code is code


def test_encrypted_page_count_and_render_size_limits_are_enforced() -> None:
    document = _new_document()
    document.new_page()
    encrypted = _finish(
        document,
        encryption=cast(Any, pymupdf).PDF_ENCRYPT_AES_256,
        owner_pw="synthetic-owner",
        user_pw="synthetic-user",
    )
    with pytest.raises(PdfImportError) as encrypted_error:
        extract_ocr_pdf(
            encrypted,
            "encrypted.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            engine=cast(Any, FakeOcrEngine(())),
        )
    assert encrypted_error.value.code is PdfImportErrorCode.ENCRYPTED_PDF

    with pytest.raises(PdfImportError) as page_error:
        extract_ocr_pdf(
            scanned_pdf(pages=2),
            "long.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            engine=cast(Any, FakeOcrEngine(())),
            max_pages=1,
        )
    assert page_error.value.code is PdfImportErrorCode.TOO_MANY_PAGES

    with pytest.raises(PdfImportError) as pixel_error:
        extract_ocr_pdf(
            scanned_pdf(),
            "large-page.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            engine=cast(Any, FakeOcrEngine(())),
            render_dpi=72,
            max_render_pixels=1,
        )
    assert pixel_error.value.code is PdfImportErrorCode.OCR_PAGE_TOO_LARGE
    assert pixel_error.value.page_numbers == (1,)


def test_no_ocr_text_no_transactions_and_text_limit_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PdfImportError) as no_text:
        extract_ocr_pdf(
            scanned_pdf(),
            "empty-ocr.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            engine=cast(Any, FakeOcrEngine(((),))),
            render_dpi=72,
        )
    assert no_text.value.code is PdfImportErrorCode.OCR_NO_TEXT
    assert no_text.value.page_numbers == (1,)

    no_rows = ocr_words(("Fictional recognised statement without transaction rows",))
    with pytest.raises(PdfImportError) as no_transactions:
        extract_ocr_pdf(
            scanned_pdf(),
            "no-rows.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            engine=cast(Any, FakeOcrEngine((no_rows,))),
            render_dpi=72,
        )
    assert no_transactions.value.code is PdfImportErrorCode.NO_TRANSACTIONS

    import cashflow_ai.imports.ocr_pdf as ocr_pdf_module

    monkeypatch.setattr(ocr_pdf_module, "MAX_PAGE_TEXT_CHARACTERS", 10)
    with pytest.raises(PdfImportError) as too_much_text:
        extract_ocr_pdf(
            scanned_pdf(),
            "too-much-text.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            engine=cast(Any, FakeOcrEngine((ocr_words(TRANSACTION_LINES),))),
            render_dpi=72,
        )
    assert too_much_text.value.code is PdfImportErrorCode.PAGE_TEXT_TOO_LARGE


def test_engine_failures_gain_page_context_without_overwriting_existing_context() -> (
    None
):
    failure = PdfImportError(PdfImportErrorCode.OCR_FAILED, "synthetic OCR failure")
    with pytest.raises(PdfImportError) as error:
        extract_ocr_pdf(
            scanned_pdf(),
            "failure.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            engine=cast(Any, FakeOcrEngine((), failure=failure)),
            render_dpi=72,
        )
    assert error.value.code is PdfImportErrorCode.OCR_FAILED
    assert error.value.page_numbers == (1,)

    located = PdfImportError(
        PdfImportErrorCode.OCR_FAILED,
        "located synthetic OCR failure",
        page_numbers=(9,),
    )
    with pytest.raises(PdfImportError) as existing:
        extract_ocr_pdf(
            scanned_pdf(),
            "located.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            engine=cast(Any, FakeOcrEngine((), failure=located)),
            render_dpi=72,
        )
    assert existing.value.page_numbers == (9,)

    class OrientationFailureEngine(FakeOcrEngine):
        def detect_orientation(
            self,
            image: Image.Image,
        ) -> tuple[int, float] | None:
            del image
            raise PdfImportError(
                PdfImportErrorCode.OCR_FAILED,
                "synthetic orientation failure",
            )

    with pytest.raises(PdfImportError) as orientation_failure:
        extract_ocr_pdf(
            scanned_pdf(),
            "orientation-failure.pdf",
            mime_type="application/pdf",
            account_id="account-1",
            engine=cast(Any, OrientationFailureEngine(())),
            render_dpi=72,
        )
    assert orientation_failure.value.page_numbers == (1,)


def test_empty_document_and_candidate_line_fallback_are_guarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyDocument:
        needs_pass = False
        page_count = 0

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    empty_document = EmptyDocument()
    monkeypatch.setattr(pymupdf, "open", lambda **kwargs: empty_document)
    with pytest.raises(PdfImportError) as error:
        _open_pdf(b"%PDF-synthetic", 10)
    assert error.value.code is PdfImportErrorCode.MALFORMED_PDF
    assert empty_document.closed is True

    row = _rows_from_text("2026-07-01 SYNTHETIC ITEM -2.00 98.00", 1)[0]
    unrelated_line = OcrLineExtraction(
        line_number=1,
        raw_text="unrelated OCR line",
        confidence=0.5,
        word_count=3,
    )
    assert _matching_line_numbers(row, (unrelated_line,)) == (1,)

    continued_row = _rows_from_text(
        "Date | Description | Amount | Balance\n"
        "2026-07-01 | FIRST LINE | -2.00 | 98.00\n"
        " | UNMATCHED CONTINUATION | | ",
        1,
    )[0]
    transaction_line = OcrLineExtraction(
        line_number=1,
        raw_text="2026-07-01 FIRST LINE -2.00 98.00",
        confidence=0.8,
        word_count=5,
    )
    assert _matching_line_numbers(continued_row, (transaction_line,)) == (1,)


def test_pytesseract_engine_availability_orientation_and_word_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PytesseractOcrEngine()
    image = Image.new("L", (20, 10), 255)
    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.0")
    engine.ensure_available()

    monkeypatch.setattr(
        pytesseract,
        "image_to_osd",
        lambda image, output_type: {"rotate": 90, "orientation_conf": 82},
    )
    assert engine.detect_orientation(image) == (90, 0.82)
    monkeypatch.setattr(
        pytesseract,
        "image_to_osd",
        lambda image, output_type: {"rotate": 45, "orientation_conf": 82},
    )
    assert engine.detect_orientation(image) is None

    result = {
        "text": ["", "ignored", "Date", "Synthetic"],
        "conf": ["0", "-1", "95", "bad"],
        "block_num": [0, 1, 1, "bad"],
        "par_num": [0, 1, 1, "bad"],
        "line_num": [0, 1, 2, "bad"],
    }
    monkeypatch.setattr(
        pytesseract,
        "image_to_data",
        lambda image, output_type, config: result,
    )
    words = engine.recognise_words(image)
    assert [word.text for word in words] == ["Date", "Synthetic"]
    assert words[0].confidence == 0.95
    assert words[1].confidence == 0.0
    assert words[1].line_number == 0
    assert _integer_at({}, "missing", 0) == 0
    image.close()


def test_pytesseract_failures_are_stable_import_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PytesseractOcrEngine()
    image = Image.new("L", (20, 10), 255)

    def missing() -> None:
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", missing)
    with pytest.raises(PdfImportError) as unavailable:
        engine.ensure_available()
    assert unavailable.value.code is PdfImportErrorCode.OCR_ENGINE_UNAVAILABLE

    def tesseract_failure(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise pytesseract.TesseractError(1, "synthetic failure")

    monkeypatch.setattr(pytesseract, "image_to_osd", tesseract_failure)
    assert engine.detect_orientation(image) is None
    monkeypatch.setattr(pytesseract, "image_to_data", tesseract_failure)
    with pytest.raises(PdfImportError) as failed:
        engine.recognise_words(image)
    assert failed.value.code is PdfImportErrorCode.OCR_FAILED
    image.close()
