"""Local, in-memory OCR extraction for scanned PDF bank statements."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean
from typing import Any, Final, Literal, Protocol, cast

import pymupdf
import pytesseract  # type: ignore[import-untyped]
from PIL import Image, ImageOps

from cashflow_ai.imports.normalisation import (
    TransactionNormalisationError,
    calculate_file_hash,
    calculate_source_fingerprint,
    normalise_transaction,
)
from cashflow_ai.imports.text_pdf import (
    DEFAULT_MAX_PDF_BYTES,
    DEFAULT_MAX_PDF_PAGES,
    DEFAULT_MIN_EMBEDDED_CHARACTERS,
    MAX_PAGE_TEXT_CHARACTERS,
    PdfImportError,
    PdfImportErrorCode,
    _extract_statement_balances,
    _extract_statement_coverage,
    _ExtractedRow,
    _mapped_original,
    _rows_from_text,
    _safe_pdf_filename,
)
from cashflow_ai.schemas.imports import (
    ExtractionMethod,
    ExtractionProvenance,
    FieldConfidence,
    ImportIssue,
    IssueSeverity,
    ParserIdentity,
    ReviewStatus,
    SourceType,
    TransactionField,
)
from cashflow_ai.schemas.normalisation import SourceRecordIdentity
from cashflow_ai.schemas.ocr_imports import (
    OcrLineExtraction,
    OcrPageExtraction,
    OcrPdfPreview,
    OcrTransactionCandidate,
)
from cashflow_ai.schemas.transactions import Currency, FinancialRole, TransactionDraft

DEFAULT_OCR_RENDER_DPI: Final = 300
DEFAULT_MAX_RENDER_PIXELS: Final = 50_000_000
LOW_CONTRAST_RANGE: Final = 96
OCR_EXTRACTOR_IDENTITY: Final = ParserIdentity(
    name="cashflow_ocr_extractor",
    version="1.0.0",
)
RotationDegrees = Literal[0, 90, 180, 270]


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One word returned by an OCR engine before line aggregation."""

    text: str
    confidence: float
    block_number: int
    paragraph_number: int
    line_number: int


class OcrEngine(Protocol):
    """Small local OCR boundary that keeps the adapter deterministic in tests."""

    def ensure_available(self) -> None:
        """Raise a stable import error when the local OCR executable is absent."""

    def detect_orientation(
        self,
        image: Image.Image,
    ) -> tuple[RotationDegrees, float] | None:
        """Return clockwise correction degrees and confidence when detectable."""

    def recognise_words(self, image: Image.Image) -> tuple[OcrWord, ...]:
        """Recognise ordered words from an in-memory preprocessed page image."""


class PytesseractOcrEngine:
    """Local Tesseract implementation of the OCR engine boundary."""

    def ensure_available(self) -> None:
        """Check the Tesseract executable without exposing subprocess details."""
        try:
            pytesseract.get_tesseract_version()
        except pytesseract.TesseractNotFoundError as error:
            raise PdfImportError(
                PdfImportErrorCode.OCR_ENGINE_UNAVAILABLE,
                "local Tesseract OCR is not installed or is not on PATH",
            ) from error

    def detect_orientation(
        self,
        image: Image.Image,
    ) -> tuple[RotationDegrees, float] | None:
        """Use Tesseract orientation detection when the page has enough text."""
        try:
            result = cast(
                dict[str, Any],
                pytesseract.image_to_osd(
                    image,
                    output_type=pytesseract.Output.DICT,
                ),
            )
        except pytesseract.TesseractError:
            return None
        rotation = int(result.get("rotate", 0))
        if rotation not in {0, 90, 180, 270}:
            return None
        raw_confidence = float(result.get("orientation_conf", 0.0))
        confidence = max(0.0, min(raw_confidence / 100.0, 1.0))
        return cast(RotationDegrees, rotation), confidence

    def recognise_words(self, image: Image.Image) -> tuple[OcrWord, ...]:
        """Recognise words and retain Tesseract's line grouping and confidence."""
        try:
            result = cast(
                dict[str, list[Any]],
                pytesseract.image_to_data(
                    image,
                    output_type=pytesseract.Output.DICT,
                    config="--psm 6",
                ),
            )
        except pytesseract.TesseractError as error:
            raise PdfImportError(
                PdfImportErrorCode.OCR_FAILED,
                "local OCR failed while reading a rendered statement page",
            ) from error

        words: list[OcrWord] = []
        for index, raw_text in enumerate(result.get("text", [])):
            text = " ".join(str(raw_text).split())
            if not text:
                continue
            try:
                raw_confidence = float(result["conf"][index])
            except (KeyError, IndexError, TypeError, ValueError):
                raw_confidence = 0.0
            if raw_confidence < 0:
                continue
            words.append(
                OcrWord(
                    text=text,
                    confidence=max(0.0, min(raw_confidence / 100.0, 1.0)),
                    block_number=_integer_at(result, "block_num", index),
                    paragraph_number=_integer_at(result, "par_num", index),
                    line_number=_integer_at(result, "line_num", index),
                )
            )
        return tuple(words)


def _integer_at(result: dict[str, list[Any]], key: str, index: int) -> int:
    try:
        return int(result[key][index])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def _issue(code: str, message: str) -> ImportIssue:
    return ImportIssue(
        code=code,
        message=message,
        severity=IssueSeverity.WARNING,
    )


def _validate_request(
    content: bytes,
    filename: str,
    *,
    mime_type: str,
    account_id: str,
    max_bytes: int,
    max_pages: int,
    render_dpi: int,
    max_render_pixels: int,
) -> str:
    if (
        max_bytes < 1
        or max_pages < 1
        or not 72 <= render_dpi <= 600
        or max_render_pixels < 1
    ):
        raise PdfImportError(
            PdfImportErrorCode.INVALID_LIMIT,
            "OCR byte, page, DPI, and rendered-pixel limits must be valid",
        )
    safe_filename = _safe_pdf_filename(filename)
    if mime_type != "application/pdf":
        raise PdfImportError(
            PdfImportErrorCode.UNSUPPORTED_MIME_TYPE,
            "OCR extraction requires the application/pdf MIME type",
        )
    if not account_id.strip() or len(account_id) > 255:
        raise PdfImportError(
            PdfImportErrorCode.INVALID_ACCOUNT,
            "provide a valid destination account identifier",
        )
    if not content:
        raise PdfImportError(PdfImportErrorCode.EMPTY_FILE, "PDF file is empty")
    if len(content) > max_bytes:
        raise PdfImportError(
            PdfImportErrorCode.FILE_TOO_LARGE,
            f"PDF exceeds the configured {max_bytes}-byte limit",
        )
    if b"%PDF-" not in content[:1024]:
        raise PdfImportError(
            PdfImportErrorCode.INVALID_SIGNATURE,
            "uploaded bytes do not contain a valid PDF signature",
        )
    return safe_filename


def _open_pdf(content: bytes, max_pages: int) -> Any:
    try:
        document: Any = pymupdf.open(  # type: ignore[no-untyped-call]
            stream=content,
            filetype="pdf",
        )
    except (pymupdf.FileDataError, RuntimeError, ValueError) as error:
        raise PdfImportError(
            PdfImportErrorCode.MALFORMED_PDF,
            "PDF structure could not be opened safely",
        ) from error
    if document.needs_pass:
        document.close()
        raise PdfImportError(
            PdfImportErrorCode.ENCRYPTED_PDF,
            "password-protected PDFs must be unlocked before extraction",
        )
    if document.page_count < 1:
        document.close()
        raise PdfImportError(
            PdfImportErrorCode.MALFORMED_PDF,
            "PDF must contain at least one page",
        )
    if document.page_count > max_pages:
        document.close()
        raise PdfImportError(
            PdfImportErrorCode.TOO_MANY_PAGES,
            f"PDF cannot contain more than {max_pages} pages",
        )
    return document


def _render_page(page: Any, render_dpi: int, max_render_pixels: int) -> Image.Image:
    pixmap: Any = page.get_pixmap(
        dpi=render_dpi,
        alpha=False,
        colorspace=pymupdf.csRGB,
    )
    if pixmap.width * pixmap.height > max_render_pixels:
        raise PdfImportError(
            PdfImportErrorCode.OCR_PAGE_TOO_LARGE,
            "a rendered PDF page exceeds the configured pixel limit",
            page_numbers=(page.number + 1,),
        )
    return Image.frombytes(
        "RGB",
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )


def _preprocess_page(
    image: Image.Image,
    engine: OcrEngine,
) -> tuple[Image.Image, RotationDegrees, float | None, bool]:
    grayscale = ImageOps.grayscale(image)
    orientation = engine.detect_orientation(grayscale)
    rotation, orientation_confidence = orientation or (0, None)
    corrected = (
        grayscale.rotate(-rotation, expand=True) if rotation else grayscale.copy()
    )
    grayscale.close()

    extrema = cast(tuple[int, int], corrected.getextrema())
    contrast_range = int(extrema[1]) - int(extrema[0])
    enhanced = ImageOps.autocontrast(corrected)
    corrected.close()
    threshold_applied = contrast_range < LOW_CONTRAST_RANGE
    if threshold_applied:
        thresholded = enhanced.point(lambda value: 255 if value >= 160 else 0)
        enhanced.close()
        enhanced = thresholded
    return enhanced, rotation, orientation_confidence, threshold_applied


def _aggregate_lines(words: Sequence[OcrWord]) -> tuple[OcrLineExtraction, ...]:
    grouped: dict[tuple[int, int, int], list[OcrWord]] = defaultdict(list)
    for word in words:
        grouped[(word.block_number, word.paragraph_number, word.line_number)].append(
            word
        )
    return tuple(
        OcrLineExtraction(
            line_number=index,
            raw_text=" ".join(word.text for word in line_words),
            confidence=fmean(word.confidence for word in line_words),
            word_count=len(line_words),
        )
        for index, line_words in enumerate(grouped.values(), start=1)
    )


def _matching_line_numbers(
    row: _ExtractedRow,
    lines: Sequence[OcrLineExtraction],
) -> tuple[int, ...]:
    anchor_text = row.values[0].casefold().strip()
    matched: list[int] = []
    for line in lines:
        if anchor_text and anchor_text in line.raw_text.casefold():
            matched.append(line.line_number)
            break
    original = _mapped_original(row)
    for continuation in original.description_text.splitlines()[1:]:
        continuation = continuation.casefold().strip()
        for line in lines:
            if (
                line.line_number not in matched
                and continuation
                and continuation in line.raw_text.casefold()
            ):
                matched.append(line.line_number)
                break
    if matched:
        return tuple(sorted(matched))
    fallback = min(row.page_record_number, len(lines))
    return (fallback,)


def _field_confidences(
    row: _ExtractedRow,
    confidence: float,
) -> tuple[FieldConfidence, ...]:
    original = _mapped_original(row)
    values = (
        (
            TransactionField.TRANSACTION_DATE,
            original.transaction_date_text,
        ),
        (TransactionField.DESCRIPTION, original.description_text),
        (
            TransactionField.AMOUNT,
            original.signed_amount_text
            or original.debit_amount_text
            or original.credit_amount_text,
        ),
        (TransactionField.BALANCE_AFTER, original.running_balance_text),
    )
    return tuple(
        FieldConfidence(field=field, confidence=confidence, raw_value=value)
        for field, value in values
        if value is not None
    )


def _candidate_from_ocr_row(
    row: _ExtractedRow,
    *,
    lines: Sequence[OcrLineExtraction],
    file_hash: str,
    account_id: str,
    account_currency: Currency,
) -> OcrTransactionCandidate:
    original = _mapped_original(row)
    identity = SourceRecordIdentity(
        source_type=SourceType.OCR_PDF,
        source_document_hash=file_hash,
        page_number=row.page_number,
        page_record_number=row.page_record_number,
    )
    source_fingerprint = calculate_source_fingerprint(identity, original)
    line_numbers = _matching_line_numbers(row, lines)
    line_confidences = {line.line_number: line.confidence for line in lines}
    confidence = fmean(line_confidences[number] for number in line_numbers)
    issues: tuple[ImportIssue, ...] = ()
    canonical_fingerprint: str | None = None
    try:
        normalised = normalise_transaction(
            original,
            account_id=account_id,
            account_currency=account_currency,
            source_identity=identity,
            parser=OCR_EXTRACTOR_IDENTITY,
        )
        draft = normalised.draft
        canonical_fingerprint = normalised.canonical_fingerprint
    except TransactionNormalisationError as error:
        draft = TransactionDraft(
            account_id=account_id,
            currency=account_currency,
            financial_role=FinancialRole.UNKNOWN,
        )
        issues = (
            ImportIssue(
                code=error.code.value,
                message=str(error),
                severity=IssueSeverity.ERROR,
            ),
        )
    return OcrTransactionCandidate(
        original=original,
        draft=draft,
        source_identity=identity,
        source_fingerprint=source_fingerprint,
        canonical_fingerprint=canonical_fingerprint,
        provenance=ExtractionProvenance(
            source_type=SourceType.OCR_PDF,
            method=ExtractionMethod.OCR,
            page_number=row.page_number,
            confidence=confidence,
            parser=OCR_EXTRACTOR_IDENTITY,
        ),
        line_numbers=line_numbers,
        field_confidences=_field_confidences(row, confidence),
        issues=issues,
        review_status=ReviewStatus.NEEDS_REVIEW,
    )


def extract_ocr_pdf(
    content: bytes,
    filename: str,
    *,
    mime_type: str,
    account_id: str,
    account_currency: Currency = Currency.GBP,
    engine: OcrEngine | None = None,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    max_pages: int = DEFAULT_MAX_PDF_PAGES,
    render_dpi: int = DEFAULT_OCR_RENDER_DPI,
    max_render_pixels: int = DEFAULT_MAX_RENDER_PIXELS,
) -> OcrPdfPreview:
    """Render and locally OCR a scanned PDF into a review-only preview."""
    safe_filename = _validate_request(
        content,
        filename,
        mime_type=mime_type,
        account_id=account_id,
        max_bytes=max_bytes,
        max_pages=max_pages,
        render_dpi=render_dpi,
        max_render_pixels=max_render_pixels,
    )
    ocr_engine = engine or PytesseractOcrEngine()
    document = _open_pdf(content, max_pages)
    file_hash = calculate_file_hash(content)
    pages: list[OcrPageExtraction] = []
    page_rows: list[tuple[_ExtractedRow, tuple[OcrLineExtraction, ...]]] = []
    embedded_character_counts: list[int] = []
    try:
        ocr_engine.ensure_available()
        for page_index in range(document.page_count):
            page_number = page_index + 1
            page: Any = document.load_page(page_index)
            embedded_text = page.get_text("text", sort=True)
            embedded_character_counts.append(
                sum(character.isalnum() for character in embedded_text)
            )
            rendered = _render_page(page, render_dpi, max_render_pixels)
            processed: Image.Image | None = None
            try:
                processed, rotation, orientation_confidence, threshold_applied = (
                    _preprocess_page(rendered, ocr_engine)
                )
                pixel_width, pixel_height = processed.size
                words = ocr_engine.recognise_words(processed)
                lines = _aggregate_lines(words)
            except PdfImportError as error:
                if error.page_numbers:
                    raise
                raise PdfImportError(
                    error.code,
                    str(error),
                    page_numbers=(page_number,),
                ) from error
            finally:
                if processed is not None:
                    processed.close()
                rendered.close()

            if not lines:
                raise PdfImportError(
                    PdfImportErrorCode.OCR_NO_TEXT,
                    f"local OCR did not recognise text on PDF page {page_number}",
                    page_numbers=(page_number,),
                )
            raw_text = "\n".join(line.raw_text for line in lines)
            if len(raw_text) > MAX_PAGE_TEXT_CHARACTERS:
                raise PdfImportError(
                    PdfImportErrorCode.PAGE_TEXT_TOO_LARGE,
                    f"OCR text on PDF page {page_number} exceeds the safe limit",
                    page_numbers=(page_number,),
                )
            page_confidence = fmean(line.confidence for line in lines)
            pages.append(
                OcrPageExtraction(
                    page_number=page_number,
                    pixel_width=pixel_width,
                    pixel_height=pixel_height,
                    render_dpi=render_dpi,
                    rotation_applied_degrees=rotation,
                    orientation_confidence=orientation_confidence,
                    threshold_applied=threshold_applied,
                    raw_text=raw_text,
                    confidence=page_confidence,
                    lines=lines,
                )
            )
            page_rows.extend(
                (row, lines) for row in _rows_from_text(raw_text, page_number)
            )
    finally:
        document.close()

    if not page_rows:
        raise PdfImportError(
            PdfImportErrorCode.NO_TRANSACTIONS,
            "no supported transaction rows were found in local OCR text",
        )

    if all(
        count < DEFAULT_MIN_EMBEDDED_CHARACTERS for count in embedded_character_counts
    ):
        document_issues = (
            _issue(
                "image_only_pdf_detected",
                "the PDF contained no usable embedded text and was processed "
                "locally with OCR",
            ),
        )
    else:
        document_issues = (
            _issue(
                "ocr_used_with_embedded_text",
                "the PDF contained some embedded text but this preview used OCR "
                "for every page",
            ),
        )

    candidates = tuple(
        _candidate_from_ocr_row(
            row,
            lines=lines,
            file_hash=file_hash,
            account_id=account_id.strip(),
            account_currency=account_currency,
        )
        for row, lines in page_rows
    )
    document_text = "\n".join(page.raw_text for page in pages)
    statement_coverage, coverage_issue = _extract_statement_coverage(document_text)
    statement_balances, balance_issues = _extract_statement_balances(document_text)
    return OcrPdfPreview(
        source_filename=safe_filename,
        byte_size=len(content),
        file_hash=file_hash,
        page_count=len(pages),
        pages=tuple(pages),
        statement_coverage=statement_coverage,
        statement_balances=statement_balances,
        candidates=candidates,
        document_issues=(
            *document_issues,
            *((coverage_issue,) if coverage_issue is not None else ()),
            *balance_issues,
        ),
    )
