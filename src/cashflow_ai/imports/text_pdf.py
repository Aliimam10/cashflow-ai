"""In-memory extraction of reviewable transactions from embedded-text PDFs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from io import BytesIO
from typing import Any, Final, cast

import pdfplumber
import pymupdf

from cashflow_ai.imports.normalisation import (
    TransactionNormalisationError,
    calculate_file_hash,
    calculate_source_fingerprint,
    normalise_transaction,
    parse_amount_value,
    parse_date_value,
)
from cashflow_ai.schemas.imports import (
    ExtractionMethod,
    ExtractionProvenance,
    ImportIssue,
    IssueSeverity,
    ParserIdentity,
    ReviewStatus,
    SourceType,
)
from cashflow_ai.schemas.normalisation import (
    OriginalTransactionValues,
    SourceFieldValue,
    SourceRecordIdentity,
)
from cashflow_ai.schemas.pdf_imports import (
    PdfExtractionLayout,
    PdfPageExtraction,
    PdfTransactionCandidate,
    TextPdfPreview,
)
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    StatementBalances,
    StatementCoverage,
)
from cashflow_ai.schemas.transactions import Currency, FinancialRole, TransactionDraft

DEFAULT_MAX_PDF_BYTES: Final = 20 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES: Final = 100
DEFAULT_MIN_EMBEDDED_CHARACTERS: Final = 20
MAX_PAGE_TEXT_CHARACTERS: Final = 1_000_000
PDF_EXTRACTOR_IDENTITY: Final = ParserIdentity(
    name="cashflow_text_pdf_extractor",
    version="1.0.0",
)


class PdfImportErrorCode(StrEnum):
    """Stable failures produced before a digital-PDF preview is returned."""

    INVALID_LIMIT = "invalid_limit"
    INVALID_FILENAME = "invalid_filename"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    UNSUPPORTED_MIME_TYPE = "unsupported_mime_type"
    INVALID_ACCOUNT = "invalid_account"
    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    INVALID_SIGNATURE = "invalid_signature"
    MALFORMED_PDF = "malformed_pdf"
    ENCRYPTED_PDF = "encrypted_pdf"
    TOO_MANY_PAGES = "too_many_pages"
    PAGE_TEXT_TOO_LARGE = "page_text_too_large"
    OCR_REQUIRED = "ocr_required"
    OCR_ENGINE_UNAVAILABLE = "ocr_engine_unavailable"
    OCR_FAILED = "ocr_failed"
    OCR_NO_TEXT = "ocr_no_text"
    OCR_PAGE_TOO_LARGE = "ocr_page_too_large"
    NO_TRANSACTIONS = "no_transactions"


class PdfImportError(ValueError):
    """Expected PDF extraction failure with a stable code and page locations."""

    def __init__(
        self,
        code: PdfImportErrorCode,
        message: str,
        *,
        page_numbers: tuple[int, ...] = (),
    ) -> None:
        """Retain machine-readable failure information for a later interface."""
        super().__init__(message)
        self.code = code
        self.page_numbers = page_numbers


@dataclass(frozen=True, slots=True)
class _ExtractedRow:
    page_number: int
    page_record_number: int
    method: ExtractionMethod
    columns: tuple[str, ...]
    values: tuple[str, ...]


_HEADER_ALIASES: Final[dict[str, frozenset[str]]] = {
    "transaction_date": frozenset(
        {"date", "transaction date", "txn date", "value date"}
    ),
    "posting_date": frozenset({"posting date", "posted date", "booking date"}),
    "description": frozenset(
        {"description", "details", "narrative", "transaction details"}
    ),
    "signed_amount": frozenset({"amount", "transaction amount", "value"}),
    "debit_amount": frozenset(
        {"debit", "debit amount", "money out", "paid out", "withdrawal"}
    ),
    "credit_amount": frozenset(
        {"credit", "credit amount", "money in", "paid in", "deposit"}
    ),
    "running_balance": frozenset({"balance", "running balance", "account balance"}),
    "currency": frozenset({"currency", "currency code"}),
    "external_id": frozenset(
        {"transaction id", "txn id", "reference id", "external id"}
    ),
    "transaction_type": frozenset({"transaction type", "type"}),
}
_PAGE_NUMBER = re.compile(r"^(?:page\s+)?\d+(?:\s+(?:of|/)\s*\d+)?$", re.I)
_DATE_TOKEN = (
    r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})"
)
_PERIOD_PATTERNS: Final = (
    re.compile(
        rf"statement\s+period\s*:?\s*({_DATE_TOKEN})\s*"
        rf"(?:to|\u2013|\u2014|-)\s*"
        rf"({_DATE_TOKEN})",
        re.I,
    ),
    re.compile(rf"\bfrom\s+({_DATE_TOKEN})\s+to\s+({_DATE_TOKEN})", re.I),
)
_BALANCE_LINE = re.compile(
    r"\b(?P<kind>opening|closing)\s+balance\s*:?\s*(?P<amount>.+?)\s*$",
    re.I,
)


def _issue(
    code: str,
    message: str,
    severity: IssueSeverity = IssueSeverity.WARNING,
) -> ImportIssue:
    return ImportIssue(code=code, message=message, severity=severity)


def _safe_pdf_filename(filename: str) -> str:
    cleaned = filename.strip().replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    cleaned = "".join(character for character in cleaned if character.isprintable())
    if not cleaned or len(cleaned) > 255 or cleaned in {".", ".."}:
        raise PdfImportError(
            PdfImportErrorCode.INVALID_FILENAME,
            "provide a non-empty PDF filename of at most 255 characters",
        )
    if not cleaned.casefold().endswith(".pdf"):
        raise PdfImportError(
            PdfImportErrorCode.UNSUPPORTED_FILE_TYPE,
            "digital-PDF extraction requires a .pdf filename",
        )
    return cleaned


def _normalise_heading(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _header_mapping(cells: Sequence[str]) -> dict[str, int] | None:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(cells):
        heading = _normalise_heading(cell)
        for target, aliases in _HEADER_ALIASES.items():
            if heading in aliases and target not in mapping:
                mapping[target] = index
                break
    required = {"transaction_date", "description"}
    has_amount = "signed_amount" in mapping or {
        "debit_amount",
        "credit_amount",
    }.issubset(mapping)
    return mapping if required.issubset(mapping) and has_amount else None


def _clean_cell(value: str | None) -> str:
    return "" if value is None else value.strip()


def _is_page_number(values: Iterable[str]) -> bool:
    text = " ".join(value.strip() for value in values if value.strip())
    return bool(text and _PAGE_NUMBER.fullmatch(text))


def _cell(values: Sequence[str], mapping: dict[str, int], target: str) -> str:
    index = mapping.get(target)
    return "" if index is None or index >= len(values) else values[index]


def _is_continuation(values: Sequence[str], mapping: dict[str, int]) -> bool:
    if _cell(values, mapping, "transaction_date").strip():
        return False
    if not _cell(values, mapping, "description").strip():
        return False
    monetary = (
        "signed_amount",
        "debit_amount",
        "credit_amount",
        "running_balance",
    )
    return not any(_cell(values, mapping, target).strip() for target in monetary)


def _append_matrix_rows(
    destination: list[_ExtractedRow],
    *,
    page_number: int,
    method: ExtractionMethod,
    headers: Sequence[str],
    rows: Iterable[Sequence[str | None]],
) -> int:
    safe_headers = tuple(
        _clean_cell(value) or f"column_{index + 1}"
        for index, value in enumerate(headers)
    )
    mapping = cast(dict[str, int], _header_mapping(safe_headers))

    added = 0
    for raw_values in rows:
        values = tuple(_clean_cell(value) for value in raw_values)
        values = (values + ("",) * len(safe_headers))[: len(safe_headers)]
        if _header_mapping(values) is not None or _is_page_number(values):
            continue
        if _is_continuation(values, mapping) and destination:
            previous = destination[-1]
            description_index = mapping["description"]
            combined = list(previous.values)
            combined[description_index] = (
                f"{combined[description_index]}\n{values[description_index]}"
            )
            destination[-1] = replace(previous, values=tuple(combined))
            continue
        if not any(value.strip() for value in values):
            continue
        destination.append(
            _ExtractedRow(
                page_number=page_number,
                page_record_number=added + 1,
                method=method,
                columns=safe_headers,
                values=values,
            )
        )
        added += 1
    return added


def _rows_from_tables(
    tables: Sequence[Sequence[Sequence[str | None]]],
    page_number: int,
) -> list[_ExtractedRow]:
    rows: list[_ExtractedRow] = []
    for table in tables:
        if not table:
            continue
        header_index = next(
            (
                index
                for index, candidate in enumerate(table)
                if _header_mapping(tuple(_clean_cell(cell) for cell in candidate))
                is not None
            ),
            None,
        )
        if header_index is None:
            continue
        before = len(rows)
        _append_matrix_rows(
            rows,
            page_number=page_number,
            method=ExtractionMethod.PDF_TABLE,
            headers=tuple(_clean_cell(cell) for cell in table[header_index]),
            rows=table[header_index + 1 :],
        )
        if before and len(rows) > before:
            offset = rows[before - 1].page_record_number
            for index in range(before, len(rows)):
                rows[index] = replace(
                    rows[index],
                    page_record_number=rows[index].page_record_number + offset,
                )
    return rows


def _split_text_line(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if "|" in stripped:
        return tuple(part.strip() for part in stripped.split("|"))
    return tuple(part.strip() for part in re.split(r"\s{2,}", stripped) if part)


def _rows_from_text(text: str, page_number: int) -> list[_ExtractedRow]:
    rows: list[_ExtractedRow] = []
    headers: tuple[str, ...] | None = None
    for line in text.splitlines():
        if not line.strip() or _is_page_number((line,)):
            continue
        parts = _split_text_line(line)
        if _header_mapping(parts) is not None:
            headers = parts
            continue
        if headers is not None:
            mapping = cast(dict[str, int], _header_mapping(headers))
            padded = (parts + ("",) * len(headers))[: len(headers)]
            description = _cell(padded, mapping, "description").strip()
            has_amount = any(
                _cell(padded, mapping, target).strip()
                for target in ("signed_amount", "debit_amount", "credit_amount")
            )
            if not description or (
                not has_amount and not _is_continuation(padded, mapping)
            ):
                continue
            before = len(rows)
            _append_matrix_rows(
                rows,
                page_number=page_number,
                method=ExtractionMethod.PDF_TEXT,
                headers=headers,
                rows=(parts,),
            )
            if len(rows) > before:
                rows[-1] = replace(rows[-1], page_record_number=len(rows))
            continue

        if len(parts) == 1:
            tokens = line.split()
            if (
                len(tokens) >= 4
                and re.fullmatch(_DATE_TOKEN, tokens[0], flags=re.I) is not None
            ):
                parts = (
                    tokens[0],
                    " ".join(tokens[1:-2]),
                    tokens[-2],
                    tokens[-1],
                )
        if not parts or re.fullmatch(_DATE_TOKEN, parts[0], flags=re.I) is None:
            continue
        if len(parts) not in {3, 4}:
            continue
        default_headers = (
            ("Date", "Description", "Amount")
            if len(parts) == 3
            else ("Date", "Description", "Amount", "Balance")
        )
        _append_matrix_rows(
            rows,
            page_number=page_number,
            method=ExtractionMethod.PDF_TEXT,
            headers=default_headers,
            rows=(parts,),
        )
        rows[-1] = replace(rows[-1], page_record_number=len(rows))
    return rows


def _mapped_original(row: _ExtractedRow) -> OriginalTransactionValues:
    mapping = cast(dict[str, int], _header_mapping(row.columns))

    def value(target: str) -> str | None:
        extracted = _cell(row.values, mapping, target)
        return extracted if target in mapping else None

    return OriginalTransactionValues(
        transaction_date_text=value("transaction_date") or "",
        description_text=value("description") or "",
        signed_amount_text=value("signed_amount"),
        debit_amount_text=value("debit_amount"),
        credit_amount_text=value("credit_amount"),
        posting_date_text=value("posting_date"),
        running_balance_text=value("running_balance"),
        currency_text=value("currency"),
        external_id_text=value("external_id"),
        transaction_type_text=value("transaction_type"),
        raw_fields=tuple(
            SourceFieldValue(column=column, value=value)
            for column, value in zip(row.columns, row.values, strict=True)
        ),
    )


def _candidate_from_row(
    row: _ExtractedRow,
    *,
    file_hash: str,
    account_id: str,
    account_currency: Currency,
) -> PdfTransactionCandidate:
    original = _mapped_original(row)
    identity = SourceRecordIdentity(
        source_type=SourceType.DIGITAL_PDF,
        source_document_hash=file_hash,
        page_number=row.page_number,
        page_record_number=row.page_record_number,
    )
    source_fingerprint = calculate_source_fingerprint(identity, original)
    issues: tuple[ImportIssue, ...] = ()
    canonical_fingerprint: str | None = None
    try:
        normalised = normalise_transaction(
            original,
            account_id=account_id,
            account_currency=account_currency,
            source_identity=identity,
            parser=PDF_EXTRACTOR_IDENTITY,
        )
        draft = normalised.draft
        canonical_fingerprint = normalised.canonical_fingerprint
    except TransactionNormalisationError as error:
        draft = TransactionDraft(
            account_id=account_id,
            currency=account_currency,
            financial_role=FinancialRole.UNKNOWN,
        )
        issues = (_issue(error.code.value, str(error), severity=IssueSeverity.ERROR),)
    return PdfTransactionCandidate(
        original=original,
        draft=draft,
        source_identity=identity,
        source_fingerprint=source_fingerprint,
        canonical_fingerprint=canonical_fingerprint,
        provenance=ExtractionProvenance(
            source_type=SourceType.DIGITAL_PDF,
            method=row.method,
            page_number=row.page_number,
            parser=PDF_EXTRACTOR_IDENTITY,
        ),
        issues=issues,
        review_status=ReviewStatus.NEEDS_REVIEW,
    )


def _extract_statement_coverage(
    text: str,
) -> tuple[StatementCoverage | None, ImportIssue | None]:
    flattened = " ".join(text.split())
    for pattern in _PERIOD_PATTERNS:
        match = pattern.search(flattened)
        if match is None:
            continue
        try:
            start = parse_date_value(match.group(1), "statement start date")
            end = parse_date_value(match.group(2), "statement end date")
            return (
                StatementCoverage(
                    statement_start_date=start,
                    statement_end_date=end,
                    status=CoverageStatus.UNKNOWN,
                ),
                None,
            )
        except (TransactionNormalisationError, ValueError):
            return None, _issue(
                "invalid_statement_period",
                "the detected statement period could not be validated",
            )
    return None, _issue(
        "statement_period_not_found",
        "statement start and end dates were not detected",
    )


def _extract_statement_balances(
    text: str,
) -> tuple[StatementBalances | None, tuple[ImportIssue, ...]]:
    values: dict[str, Any] = {}
    issues: list[ImportIssue] = []
    for line in text.splitlines():
        match = _BALANCE_LINE.search(line.strip())
        if match is None:
            continue
        kind = match.group("kind").casefold()
        try:
            values[f"{kind}_balance"] = parse_amount_value(
                match.group("amount"),
                f"{kind} balance",
            )
        except TransactionNormalisationError:
            issues.append(
                _issue(
                    f"invalid_{kind}_balance",
                    f"the detected {kind} balance could not be validated",
                )
            )
    if not values:
        if not issues:
            issues.append(
                _issue(
                    "statement_balances_not_found",
                    "opening and closing statement balances were not detected",
                )
            )
        return None, tuple(issues)
    return StatementBalances.model_validate(values), tuple(issues)


def _inspect_pdf(
    content: bytes,
    *,
    max_pages: int,
    min_embedded_characters: int,
) -> tuple[tuple[str, ...], tuple[int, ...]]:
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
    with document:
        if document.needs_pass:
            raise PdfImportError(
                PdfImportErrorCode.ENCRYPTED_PDF,
                "password-protected PDFs must be unlocked before extraction",
            )
        if document.page_count > max_pages:
            raise PdfImportError(
                PdfImportErrorCode.TOO_MANY_PAGES,
                f"PDF cannot contain more than {max_pages} pages",
            )
        page_texts: list[str] = []
        character_counts: list[int] = []
        ocr_pages: list[int] = []
        for page_index in range(document.page_count):
            page_number = page_index + 1
            page: Any = document.load_page(page_index)
            text = page.get_text("text", sort=True)
            if len(text) > MAX_PAGE_TEXT_CHARACTERS:
                raise PdfImportError(
                    PdfImportErrorCode.PAGE_TEXT_TOO_LARGE,
                    f"PDF page {page_number} contains too much embedded text",
                    page_numbers=(page_number,),
                )
            count = sum(character.isalnum() for character in text)
            page_texts.append(text)
            character_counts.append(count)
            if count < min_embedded_characters:
                ocr_pages.append(page_number)
        if ocr_pages:
            raise PdfImportError(
                PdfImportErrorCode.OCR_REQUIRED,
                "one or more PDF pages do not contain enough embedded text",
                page_numbers=tuple(ocr_pages),
            )
    return tuple(page_texts), tuple(character_counts)


def _extract_rows_and_pages(
    content: bytes,
    page_texts: tuple[str, ...],
    character_counts: tuple[int, ...],
) -> tuple[
    tuple[_ExtractedRow, ...],
    tuple[PdfPageExtraction, ...],
    frozenset[PdfExtractionLayout],
    tuple[ImportIssue, ...],
]:
    all_rows: list[_ExtractedRow] = []
    pages: list[PdfPageExtraction] = []
    layouts: set[PdfExtractionLayout] = set()
    issues: list[ImportIssue] = []
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            for page_number, (page, raw_text, character_count) in enumerate(
                zip(pdf.pages, page_texts, character_counts, strict=True),
                start=1,
            ):
                tables = page.extract_tables()
                table_rows = _rows_from_tables(tables, page_number)
                if table_rows:
                    rows = table_rows
                    layouts.add(PdfExtractionLayout.TABLE)
                else:
                    layout_text = page.extract_text(layout=True) or raw_text
                    rows = _rows_from_text(layout_text, page_number)
                    if rows:
                        layouts.add(PdfExtractionLayout.GENERIC_TEXT)
                all_rows.extend(rows)
                pages.append(
                    PdfPageExtraction(
                        page_number=page_number,
                        raw_text=raw_text,
                        embedded_character_count=character_count,
                        tables_found=len(tables),
                    )
                )
    except Exception as error:
        issues.append(
            _issue(
                "table_extraction_failed",
                "table extraction failed; generic embedded text was used",
            )
        )
        all_rows.clear()
        pages.clear()
        layouts.clear()
        for page_number, (raw_text, character_count) in enumerate(
            zip(page_texts, character_counts, strict=True),
            start=1,
        ):
            rows = _rows_from_text(raw_text, page_number)
            all_rows.extend(rows)
            if rows:
                layouts.add(PdfExtractionLayout.GENERIC_TEXT)
            pages.append(
                PdfPageExtraction(
                    page_number=page_number,
                    raw_text=raw_text,
                    embedded_character_count=character_count,
                    tables_found=0,
                )
            )
        if not all_rows:
            raise PdfImportError(
                PdfImportErrorCode.NO_TRANSACTIONS,
                "no supported transaction rows were found in embedded PDF text",
            ) from error

    if PdfExtractionLayout.GENERIC_TEXT in layouts:
        issues.append(
            _issue(
                "generic_text_fallback",
                "one or more pages used a generic text layout and require close review",
            )
        )
    return tuple(all_rows), tuple(pages), frozenset(layouts), tuple(issues)


def extract_text_pdf(
    content: bytes,
    filename: str,
    *,
    mime_type: str,
    account_id: str,
    account_currency: Currency = Currency.GBP,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    max_pages: int = DEFAULT_MAX_PDF_PAGES,
    min_embedded_characters: int = DEFAULT_MIN_EMBEDDED_CHARACTERS,
) -> TextPdfPreview:
    """Validate and extract a review-only preview from a digital bank PDF."""
    if max_bytes < 1 or max_pages < 1 or min_embedded_characters < 1:
        raise PdfImportError(
            PdfImportErrorCode.INVALID_LIMIT,
            "PDF byte, page, and embedded-text limits must be positive",
        )
    safe_filename = _safe_pdf_filename(filename)
    if mime_type != "application/pdf":
        raise PdfImportError(
            PdfImportErrorCode.UNSUPPORTED_MIME_TYPE,
            "digital-PDF extraction requires the application/pdf MIME type",
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

    page_texts, character_counts = _inspect_pdf(
        content,
        max_pages=max_pages,
        min_embedded_characters=min_embedded_characters,
    )
    rows, pages, layouts, extraction_issues = _extract_rows_and_pages(
        content,
        page_texts,
        character_counts,
    )
    if not rows:
        raise PdfImportError(
            PdfImportErrorCode.NO_TRANSACTIONS,
            "no supported transaction rows were found in embedded PDF text",
        )

    file_hash = calculate_file_hash(content)
    candidates = tuple(
        _candidate_from_row(
            row,
            file_hash=file_hash,
            account_id=account_id.strip(),
            account_currency=account_currency,
        )
        for row in rows
    )
    document_text = "\n".join(page_texts)
    coverage, coverage_issue = _extract_statement_coverage(document_text)
    balances, balance_issues = _extract_statement_balances(document_text)
    document_issues = list(extraction_issues)
    if coverage_issue is not None:
        document_issues.append(coverage_issue)
    document_issues.extend(balance_issues)
    return TextPdfPreview(
        source_filename=safe_filename,
        byte_size=len(content),
        file_hash=file_hash,
        page_count=len(pages),
        pages=pages,
        layouts=layouts,
        statement_coverage=coverage,
        statement_balances=balances,
        candidates=candidates,
        document_issues=tuple(document_issues),
    )
