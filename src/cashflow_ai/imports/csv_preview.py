"""Safe, non-persistent preview of CSV statement uploads."""

from __future__ import annotations

import csv
import re
from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF16_LE
from collections.abc import Iterable
from enum import StrEnum
from io import StringIO
from typing import Final

from cashflow_ai.schemas.csv_imports import (
    CsvColumnSuggestions,
    CsvEncoding,
    CsvImportPlan,
    CsvPreview,
    CsvPreviewRow,
)

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
    "signed_amount": frozenset({"amount", "amount gbp", "transaction amount", "value"}),
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
    "transaction_type": frozenset({"category type", "transaction type", "type"}),
}


def _normalise_heading(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


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
    if len(headers) > MAX_CSV_COLUMNS:
        raise CsvImportError(
            CsvImportErrorCode.INVALID_HEADER,
            f"CSV cannot contain more than {MAX_CSV_COLUMNS} columns",
        )
    normalised = [_normalise_heading(value) for value in headers]
    if len(normalised) != len(set(normalised)):
        raise CsvImportError(
            CsvImportErrorCode.INVALID_HEADER,
            "CSV headings must be unique",
        )
    return headers


def _suggest_columns(columns: Iterable[str]) -> CsvColumnSuggestions:
    matches: dict[str, list[str]] = {key: [] for key in _COLUMN_ALIASES}
    for column in columns:
        normalised = _normalise_heading(column)
        for target, aliases in _COLUMN_ALIASES.items():
            if normalised in aliases:
                matches[target].append(column)
    return CsvColumnSuggestions.model_validate(matches)


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
        headers = _validate_headers(next(reader))
        preview: list[CsvPreviewRow] = []
        total_data_rows = 0
        for raw_row in reader:
            total_data_rows += 1
            if len(raw_row) != len(headers):
                raise CsvImportError(
                    CsvImportErrorCode.MALFORMED_CSV,
                    f"CSV row {total_data_rows + 1} has an unexpected column count",
                )
            if any(len(value) > MAX_CELL_CHARACTERS for value in raw_row):
                raise CsvImportError(
                    CsvImportErrorCode.MALFORMED_CSV,
                    f"CSV row {total_data_rows + 1} contains an oversized value",
                )
            if total_data_rows <= preview_rows:
                preview.append(
                    CsvPreviewRow(
                        source_row_number=total_data_rows + 1,
                        values=tuple(raw_row),
                    )
                )
    except csv.Error as exc:
        raise CsvImportError(
            CsvImportErrorCode.MALFORMED_CSV,
            "CSV quoting or row structure is malformed",
        ) from exc

    if total_data_rows == 0:
        raise CsvImportError(
            CsvImportErrorCode.EMPTY_FILE,
            "CSV contains headings but no data rows",
        )
    return CsvPreview(
        source_filename=safe_filename,
        byte_size=len(content),
        encoding=encoding,
        delimiter=delimiter,
        columns=headers,
        rows=tuple(preview),
        total_data_rows=total_data_rows,
        truncated=total_data_rows > len(preview),
        suggestions=_suggest_columns(headers),
    )


def validate_csv_import_plan(
    preview: CsvPreview,
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
