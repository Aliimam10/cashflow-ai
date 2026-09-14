"""Safe, non-persistent preview of CSV statement uploads."""

from __future__ import annotations

import csv
import hashlib
import re
from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF16_LE
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from io import StringIO
from typing import Final

from cashflow_ai.schemas.csv_imports import (
    CsvColumnSuggestions,
    CsvDocument,
    CsvEncoding,
    CsvImportPlan,
    CsvPreview,
    CsvPreviewRow,
)
from cashflow_ai.schemas.statements import DateRange

DEFAULT_MAX_CSV_BYTES: Final = 10 * 1024 * 1024
DEFAULT_PREVIEW_ROWS: Final = 25
MAX_CSV_COLUMNS: Final = 100
MAX_CELL_CHARACTERS: Final = 100_000
ALLOWED_DELIMITERS: Final = ",;\t|"


class CsvImportErrorCode(StrEnum):
    """Stable failure codes suitable for a later user interface."""

    INVALID_LIMIT = "invalid_limit"
    INVALID_FILENAME = "invalid_filename"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    BINARY_CONTENT = "binary_content"
    MALFORMED_CSV = "malformed_csv"
    INVALID_HEADER = "invalid_header"
    MISSING_MAPPED_COLUMN = "missing_mapped_column"
    CONFIRMATION_REQUIRED = "confirmation_required"
    INVALID_CONFIRMATION_TIME = "invalid_confirmation_time"
    PREVIEW_CHANGED = "preview_changed"
    UNSUPPORTED_MIME_TYPE = "unsupported_mime_type"
    ACCOUNT_NOT_FOUND = "account_not_found"
    ACCOUNT_CURRENCY_MISMATCH = "account_currency_mismatch"
    TRANSACTION_OUTSIDE_COVERAGE = "transaction_outside_coverage"


class CsvImportError(ValueError):
    """Expected CSV preview or mapping failure with a stable error code."""

    def __init__(self, code: CsvImportErrorCode, message: str) -> None:
        """Store a stable machine-readable code alongside the user message."""
        super().__init__(message)
        self.code = code


_COLUMN_ALIASES: Final[dict[str, frozenset[str]]] = {
    "transaction_date": frozenset(
        {"date", "transaction date", "transaction date time", "txn date"}
    ),
    "posting_date": frozenset(
        {"booking date", "posted date", "posting date", "value date"}
    ),
    "description": frozenset(
        {
            "description",
            "details",
            "memo",
            "narrative",
            "reference",
            "transaction details",
        }
    ),
    "signed_amount": frozenset(
        {"amount", "amount gbp", "money in out", "transaction amount", "value"}
    ),
    "debit_amount": frozenset(
        {"debit", "debit amount", "money out", "paid out", "withdrawal"}
    ),
    "credit_amount": frozenset(
        {"credit", "credit amount", "deposit", "money in", "paid in"}
    ),
    "running_balance": frozenset(
        {"account balance", "balance", "balance gbp", "running balance"}
    ),
    "currency": frozenset({"currency", "currency code", "iso currency"}),
    "external_id": frozenset(
        {"external id", "reference id", "transaction id", "txn id"}
    ),
    "transaction_type": frozenset(
        {"category", "category type", "transaction type", "type"}
    ),
}

_CONSOLIDATED_GBP_HEADERS: Final = (
    "date",
    "description",
    "category",
    "money in out",
    "balance",
    "tax withheld",
    "other taxes",
    "fees",
)
_CONSOLIDATED_DUAL_CURRENCY_HEADERS: Final = (
    "date",
    "description",
    "category",
    "money in out",
    "money in out",
    "balance",
    "balance",
    "tax withheld",
    "tax withheld",
    "other taxes",
    "other taxes",
    "fees",
    "fees",
)
_CONSOLIDATED_DATE = re.compile(r"^[A-Za-z]{3} \d{1,2}, \d{4}$")
_CONSOLIDATED_GBP_MONEY = re.compile(r"^[+-]?£\d[\d,]*(?:\.\d{2})$")
_GENERIC_PARSER_NAME: Final = "generic_csv"
_GENERIC_LAYOUT_VERSION: Final = "flat_v1"
_REVOLUT_PARSER_NAME: Final = "revolut_consolidated_csv"
_REVOLUT_PARSER_VERSION: Final = "1.0.0"
_REVOLUT_LAYOUT_VERSION: Final = "consolidated_v2_gbp_1"
_NON_GBP_WARNING: Final = "non_gbp_transaction_sections_excluded"
_EMPTY_GBP_SECTION_WARNING: Final = "empty_gbp_transaction_sections_ignored"


@dataclass(frozen=True, slots=True)
class _CsvTableSelection:
    """One validated embedded transaction table and controlled metadata."""

    headers: tuple[str, ...]
    rows: tuple[CsvPreviewRow, ...]
    warning_codes: tuple[str, ...]
    excluded_transaction_rows: int


def _normalise_heading(value: str) -> str:
    # Python's ``\w`` includes underscores, while bank exports commonly use
    # snake_case for the same headings that other exports write with spaces.
    separated = value.casefold().replace("_", " ")
    return " ".join(re.sub(r"[^\w]+", " ", separated).split())


def _safe_filename(filename: str) -> str:
    cleaned = filename.strip().replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    cleaned = "".join(character for character in cleaned if character.isprintable())
    if not cleaned or len(cleaned) > 255 or cleaned in {".", ".."}:
        raise CsvImportError(
            CsvImportErrorCode.INVALID_FILENAME,
            "provide a non-empty filename of at most 255 characters",
        )
    if not cleaned.casefold().endswith(".csv"):
        raise CsvImportError(
            CsvImportErrorCode.UNSUPPORTED_FILE_TYPE,
            "CSV previews require a .csv filename",
        )
    return cleaned


def _decode_csv(content: bytes) -> tuple[str, CsvEncoding]:
    candidates: tuple[tuple[CsvEncoding, str], ...]
    if content.startswith(BOM_UTF8):
        candidates = ((CsvEncoding.UTF_8_SIG, "utf-8-sig"),)
    elif content.startswith(BOM_UTF16_LE) or content.startswith(BOM_UTF16_BE):
        candidates = ((CsvEncoding.UTF_16, "utf-16"),)
    else:
        candidates = (
            (CsvEncoding.UTF_8, "utf-8"),
            (CsvEncoding.WINDOWS_1252, "cp1252"),
        )

    for detected, codec in candidates:
        try:
            return content.decode(codec, errors="strict"), detected
        except UnicodeDecodeError:
            continue
    raise CsvImportError(
        CsvImportErrorCode.UNSUPPORTED_ENCODING,
        "CSV text must use UTF-8, UTF-16 with a byte-order mark, or Windows-1252",
    )


def _validate_text(text: str) -> None:
    if not text.strip():
        raise CsvImportError(CsvImportErrorCode.EMPTY_FILE, "CSV file is empty")
    if any(
        ord(character) < 32 and character not in {"\t", "\n", "\r"}
        for character in text
    ):
        raise CsvImportError(
            CsvImportErrorCode.BINARY_CONTENT,
            "CSV contains binary control characters",
        )


def _detect_delimiter(text: str) -> str:
    header_sample = text.splitlines()[0]
    try:
        dialect = csv.Sniffer().sniff(
            header_sample[:8192], delimiters=ALLOWED_DELIMITERS
        )
    except csv.Error as exc:
        raise CsvImportError(
            CsvImportErrorCode.MALFORMED_CSV,
            "could not determine the CSV delimiter",
        ) from exc
    return dialect.delimiter


def _validate_headers(raw_headers: list[str]) -> tuple[str, ...]:
    headers = tuple(value.strip() for value in raw_headers)
    if not headers or any(not value or len(value) > 255 for value in headers):
        raise CsvImportError(
            CsvImportErrorCode.INVALID_HEADER,
            "CSV headings must be non-empty and at most 255 characters",
        )
    normalised = [_normalise_heading(value) for value in headers]
    if len(normalised) != len(set(normalised)):
        raise CsvImportError(
            CsvImportErrorCode.INVALID_HEADER,
            "CSV headings must be unique",
        )
    return headers


def _normalised_row(row: list[str]) -> tuple[str, ...]:
    return tuple(_normalise_heading(value) for value in row)


def _is_transaction_table_header(row: list[str]) -> bool:
    """Recognise transaction-like headings so changed layouts fail closed."""
    normalised = _normalised_row(row)
    return (
        len(normalised) >= 4
        and normalised[:3] == ("date", "description", "category")
        and "money in out" in normalised[3:]
    )


def _is_consolidated_header(row: list[str]) -> bool:
    """Recognise the GBP transaction-table heading prefix."""
    width = len(_CONSOLIDATED_GBP_HEADERS)
    return (
        len(row) >= width and _normalised_row(row[:width]) == _CONSOLIDATED_GBP_HEADERS
    )


def _is_consolidated_gbp_header(row: list[str]) -> bool:
    """Recognise the narrow GBP-only table in the supported layout."""
    width = len(_CONSOLIDATED_GBP_HEADERS)
    return _is_consolidated_header(row) and all(
        not value.strip() for value in row[width:]
    )


def _is_consolidated_dual_currency_header(row: list[str]) -> bool:
    """Recognise the excluded paired-currency table in the supported layout."""
    return _normalised_row(row) == _CONSOLIDATED_DUAL_CURRENCY_HEADERS


def _parse_consolidated_date(value: str) -> datetime:
    return datetime.strptime(value, "%b %d, %Y")


def _parse_consolidated_gbp(value: str) -> Decimal:
    try:
        return Decimal(value.replace("£", "").replace(",", ""))
    except InvalidOperation as error:  # pragma: no cover - guarded by the regex
        raise ValueError("invalid consolidated money") from error


def _embedded_table_row_count(
    raw_rows: list[list[str]], header_index: int
) -> int | None:
    """Count a complete excluded table without inspecting its private values."""
    for position, raw_row in enumerate(raw_rows[header_index + 1 :], start=1):
        if not any(value.strip() for value in raw_row):
            return None
        if _normalise_heading(raw_row[0]) == "total":
            return position - 1 or None
    return None


def _validated_gbp_rows(
    raw_rows: list[list[str]], header_index: int
) -> tuple[CsvPreviewRow, ...] | None:
    """Require chronological, balance-reconciled rows and a matching total."""
    width = len(_CONSOLIDATED_GBP_HEADERS)
    selected: list[CsvPreviewRow] = []
    amounts: list[Decimal] = []
    previous_date: datetime | None = None
    previous_balance: Decimal | None = None
    footer_found = False
    for row_index, raw_row in enumerate(
        raw_rows[header_index + 1 :],
        start=header_index + 2,
    ):
        if not any(value.strip() for value in raw_row):
            break
        if _normalise_heading(raw_row[0]) == "total":
            footer_found = (
                len(raw_row) >= width
                and all(not value.strip() for value in raw_row[width:])
                and _CONSOLIDATED_GBP_MONEY.fullmatch(raw_row[3].strip()) is not None
                and _parse_consolidated_gbp(raw_row[3].strip()) == sum(amounts)
            )
            break
        if (
            len(raw_row) < width
            or any(value.strip() for value in raw_row[width:])
            or _CONSOLIDATED_DATE.fullmatch(raw_row[0].strip()) is None
            or not raw_row[1].strip()
            or not raw_row[2].strip()
            or _CONSOLIDATED_GBP_MONEY.fullmatch(raw_row[3].strip()) is None
            or _CONSOLIDATED_GBP_MONEY.fullmatch(raw_row[4].strip()) is None
            or any(
                value.strip()
                and _CONSOLIDATED_GBP_MONEY.fullmatch(value.strip()) is None
                for value in raw_row[5:8]
            )
        ):
            return None
        try:
            parsed_date = _parse_consolidated_date(raw_row[0].strip())
        except ValueError:
            return None
        amount = _parse_consolidated_gbp(raw_row[3].strip())
        balance = _parse_consolidated_gbp(raw_row[4].strip())
        if previous_date is not None and parsed_date < previous_date:
            return None
        if previous_balance is not None and balance != previous_balance + amount:
            return None
        selected.append(
            CsvPreviewRow(
                source_row_number=row_index,
                values=tuple(raw_row[:width]),
            )
        )
        amounts.append(amount)
        previous_date = parsed_date
        previous_balance = balance
    if not selected or not footer_found:
        return None
    return tuple(selected)


def _is_empty_gbp_table(raw_rows: list[list[str]], header_index: int) -> bool:
    """Recognise a declared GBP table which contains only a zero-value total.

    Some consolidated exports append a second account section even when it has no
    transactions.  It is safe to ignore that section only when its first content
    row is a structurally valid zero total; a malformed or data-bearing second
    section must still make the layout unsupported.
    """
    width = len(_CONSOLIDATED_GBP_HEADERS)
    for raw_row in raw_rows[header_index + 1 :]:
        if not any(value.strip() for value in raw_row):
            return False
        if _normalise_heading(raw_row[0]) != "total":
            return False
        return (
            len(raw_row) >= width
            and all(not value.strip() for value in raw_row[width:])
            and _CONSOLIDATED_GBP_MONEY.fullmatch(raw_row[3].strip()) is not None
            and _parse_consolidated_gbp(raw_row[3].strip()) == Decimal("0.00")
        )
    return False


def _consolidated_gbp_table(
    raw_rows: list[list[str]],
) -> _CsvTableSelection | None:
    """Select one unambiguous GBP transaction table from a multi-section CSV."""
    transaction_indexes = tuple(
        index for index, row in enumerate(raw_rows) if _is_transaction_table_header(row)
    )
    gbp_indexes = tuple(
        index
        for index in transaction_indexes
        if _is_consolidated_gbp_header(raw_rows[index])
    )
    excluded_indexes = tuple(
        index
        for index in transaction_indexes
        if _is_consolidated_dual_currency_header(raw_rows[index])
    )
    if len(gbp_indexes) + len(excluded_indexes) != len(transaction_indexes):
        return None
    selected_tables = tuple(
        (index, rows)
        for index in gbp_indexes
        if (rows := _validated_gbp_rows(raw_rows, index)) is not None
    )
    empty_gbp_indexes = tuple(
        index
        for index in gbp_indexes
        if _validated_gbp_rows(raw_rows, index) is None
        and _is_empty_gbp_table(raw_rows, index)
    )
    if len(selected_tables) != 1 or len(selected_tables) + len(
        empty_gbp_indexes
    ) != len(gbp_indexes):
        return None
    header_index, selected = selected_tables[0]
    width = len(_CONSOLIDATED_GBP_HEADERS)
    excluded_counts = tuple(
        _embedded_table_row_count(raw_rows, index) for index in excluded_indexes
    )
    if any(count is None for count in excluded_counts):
        return None
    excluded_rows = sum(count for count in excluded_counts if count is not None)
    return _CsvTableSelection(
        headers=_validate_headers(raw_rows[header_index][:width]),
        rows=selected,
        warning_codes=tuple(
            warning
            for warning, applies in (
                (_NON_GBP_WARNING, excluded_rows > 0),
                (_EMPTY_GBP_SECTION_WARNING, bool(empty_gbp_indexes)),
            )
            if applies
        ),
        excluded_transaction_rows=excluded_rows,
    )


def _suggest_columns(columns: Iterable[str]) -> CsvColumnSuggestions:
    matches: dict[str, list[str]] = {key: [] for key in _COLUMN_ALIASES}
    for column in columns:
        normalised = _normalise_heading(column)
        for target, aliases in _COLUMN_ALIASES.items():
            if normalised in aliases:
                matches[target].append(column)
    return CsvColumnSuggestions.model_validate(matches)


def _suggest_statement_period(
    document: CsvDocument,
) -> tuple[str | None, DateRange | None]:
    """Infer full-file bounds from the first recognised transaction-date column."""
    if not document.suggestions.transaction_date:
        return None, None

    # Import locally to retain one bank-date parser without a package import cycle.
    from cashflow_ai.imports.normalisation import (
        TransactionNormalisationError,
        parse_date_value,
    )

    column = document.suggestions.transaction_date[0]
    column_index = document.columns.index(column)
    parsed_dates = []
    for row in document.rows:
        try:
            parsed_dates.append(parse_date_value(row.values[column_index]))
        except TransactionNormalisationError:
            continue
    if not parsed_dates:
        return None, None
    return column, DateRange(
        start_date=min(parsed_dates),
        end_date=max(parsed_dates),
    )


def preview_csv(
    content: bytes,
    filename: str,
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
    preview_rows: int = DEFAULT_PREVIEW_ROWS,
) -> CsvPreview:
    """Validate CSV bytes and return a safe, row-limited structural preview.

    The full file is checked for structurally malformed rows, but only the first
    ``preview_rows`` records are retained in the returned object. No uploaded
    content is written to disk or converted into accepted transactions.
    """
    if max_bytes < 1 or preview_rows < 1:
        raise CsvImportError(
            CsvImportErrorCode.INVALID_LIMIT,
            "file-size and preview-row limits must be positive",
        )
    document = parse_csv_document(content, filename, max_bytes=max_bytes)
    preview = document.rows[:preview_rows]
    date_column, statement_period = _suggest_statement_period(document)
    return CsvPreview(
        source_filename=document.source_filename,
        byte_size=document.byte_size,
        file_hash=document.file_hash,
        encoding=document.encoding,
        delimiter=document.delimiter,
        columns=document.columns,
        rows=preview,
        total_data_rows=len(document.rows),
        truncated=len(document.rows) > len(preview),
        suggestions=document.suggestions,
        suggested_date_column=date_column,
        suggested_statement_period=statement_period,
        parser_name=document.parser_name,
        parser_version=document.parser_version,
        layout_version=document.layout_version,
        warning_codes=document.warning_codes,
        excluded_transaction_rows=document.excluded_transaction_rows,
    )


def parse_csv_document(
    content: bytes,
    filename: str,
    *,
    max_bytes: int = DEFAULT_MAX_CSV_BYTES,
) -> CsvDocument:
    """Validate and retain every source row for a confirmed CSV import."""
    if max_bytes < 1:
        raise CsvImportError(
            CsvImportErrorCode.INVALID_LIMIT,
            "file-size limit must be positive",
        )
    safe_filename = _safe_filename(filename)
    if not content:
        raise CsvImportError(CsvImportErrorCode.EMPTY_FILE, "CSV file is empty")
    if len(content) > max_bytes:
        raise CsvImportError(
            CsvImportErrorCode.FILE_TOO_LARGE,
            f"CSV exceeds the configured {max_bytes}-byte limit",
        )

    text, encoding = _decode_csv(content)
    _validate_text(text)
    delimiter = _detect_delimiter(text)
    reader = csv.reader(StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        raw_rows = list(reader)
    except csv.Error as exc:
        raise CsvImportError(
            CsvImportErrorCode.MALFORMED_CSV,
            "CSV quoting or row structure is malformed",
        ) from exc

    if any(len(row) > MAX_CSV_COLUMNS for row in raw_rows):
        raise CsvImportError(
            CsvImportErrorCode.INVALID_HEADER,
            f"CSV cannot contain more than {MAX_CSV_COLUMNS} columns",
        )
    if any(len(value) > MAX_CELL_CHARACTERS for row in raw_rows for value in row):
        raise CsvImportError(
            CsvImportErrorCode.MALFORMED_CSV,
            "CSV contains an oversized value",
        )

    parser_name = _GENERIC_PARSER_NAME
    parser_version = "1.0.0"
    layout_version = _GENERIC_LAYOUT_VERSION
    warning_codes: tuple[str, ...] = ()
    excluded_transaction_rows = 0
    try:
        headers = _validate_headers(raw_rows[0])
    except CsvImportError as original_error:
        consolidated = _consolidated_gbp_table(raw_rows)
        if consolidated is None:
            raise original_error
        headers = consolidated.headers
        rows = list(consolidated.rows)
        parser_name = _REVOLUT_PARSER_NAME
        parser_version = _REVOLUT_PARSER_VERSION
        layout_version = _REVOLUT_LAYOUT_VERSION
        warning_codes = consolidated.warning_codes
        excluded_transaction_rows = consolidated.excluded_transaction_rows
    else:
        rows = []
        for source_row_number, raw_row in enumerate(raw_rows[1:], start=2):
            if len(raw_row) != len(headers):
                raise CsvImportError(
                    CsvImportErrorCode.MALFORMED_CSV,
                    f"CSV row {source_row_number} has an unexpected column count",
                )
            rows.append(
                CsvPreviewRow(
                    source_row_number=source_row_number,
                    values=tuple(raw_row),
                )
            )

    if not rows:
        raise CsvImportError(
            CsvImportErrorCode.EMPTY_FILE,
            "CSV contains headings but no data rows",
        )
    return CsvDocument(
        source_filename=safe_filename,
        byte_size=len(content),
        file_hash=hashlib.sha256(content).hexdigest(),
        encoding=encoding,
        delimiter=delimiter,
        columns=headers,
        rows=tuple(rows),
        suggestions=_suggest_columns(headers),
        parser_name=parser_name,
        parser_version=parser_version,
        layout_version=layout_version,
        warning_codes=warning_codes,
        excluded_transaction_rows=excluded_transaction_rows,
    )


def validate_csv_import_plan(
    preview: CsvPreview | CsvDocument,
    plan: CsvImportPlan,
) -> CsvImportPlan:
    """Require every selected mapping column to exist in the preview."""
    available = {column.casefold() for column in preview.columns}
    missing = [
        column
        for column in plan.mapping.source_columns
        if column.casefold() not in available
    ]
    if missing:
        formatted = ", ".join(missing)
        raise CsvImportError(
            CsvImportErrorCode.MISSING_MAPPED_COLUMN,
            f"mapped columns are not present in the CSV: {formatted}",
        )
    return plan
