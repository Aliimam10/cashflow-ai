"""Deterministic spatial reconstruction for embedded-text PDF statements.

This module does not persist, categorise, or log statement content. It converts
positioned PDF words into an in-memory table that a higher-level import service
can either map automatically, present for explicit column mapping, or reject.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from itertools import pairwise
from statistics import median
from typing import Any, Final

import pymupdf

DEFAULT_MAX_SPATIAL_PDF_PAGES: Final = 100
DEFAULT_MAX_SPATIAL_PDF_WORDS: Final = 250_000
DEFAULT_MIN_SPATIAL_CHARACTERS: Final = 20
_MAX_WORD_CHARACTERS: Final = 4_096
_HEADER_POSITION_TOLERANCE: Final = 0.08
_MONEY_CLUSTER_TOLERANCE: Final = 0.035


class SpatialPdfState(StrEnum):
    """Safe outcome of a spatial PDF reconstruction attempt."""

    READY = "ready"
    MAPPING_REQUIRED = "mapping_required"
    UNSUPPORTED_LAYOUT = "unsupported_layout"


class SpatialColumnRole(StrEnum):
    """Transaction fields that a reconstructed column may provide."""

    TRANSACTION_DATE = "transaction_date"
    DESCRIPTION = "description"
    SIGNED_AMOUNT = "signed_amount"
    DEBIT_AMOUNT = "debit_amount"
    CREDIT_AMOUNT = "credit_amount"
    RUNNING_BALANCE = "running_balance"


class SpatialPdfErrorCode(StrEnum):
    """Stable failures raised before a safe reconstruction result is possible."""

    INVALID_LIMIT = "invalid_limit"
    MALFORMED_PDF = "malformed_pdf"
    ENCRYPTED_PDF = "encrypted_pdf"
    TOO_MANY_PAGES = "too_many_pages"
    PAGE_TOO_COMPLEX = "page_too_complex"
    INVALID_MAPPING = "invalid_mapping"


class SpatialPdfError(ValueError):
    """Privacy-safe failure carrying a stable machine-readable code."""

    def __init__(self, code: SpatialPdfErrorCode, message: str) -> None:
        """Initialise an error without retaining source statement content."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SpatialPdfWord:
    """One embedded PDF word and its page-space bounding box."""

    page_number: int
    x0: float
    top: float
    x1: float
    bottom: float
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SpatialPdfColumn:
    """One stable left-to-right table column."""

    column_id: str
    left: float
    right: float
    header_text: str = field(repr=False)
    role_hint: SpatialColumnRole | None = None


@dataclass(frozen=True, slots=True)
class SpatialPdfCell:
    """Source text assigned to one reconstructed table cell."""

    column_id: str
    text: str = field(repr=False)
    x0: float = 0.0
    top: float = 0.0
    x1: float = 0.0
    bottom: float = 0.0


@dataclass(frozen=True, slots=True)
class SpatialPdfRecord:
    """One spatial row with stable PDF page and record provenance."""

    page_number: int
    page_record_number: int
    source_line_numbers: tuple[int, ...]
    cells: tuple[SpatialPdfCell, ...] = field(repr=False)
    unresolved: bool = False

    def value(self, column_id: str) -> str:
        """Return a preserved cell value, or an empty string for no such column."""
        return next(
            (cell.text for cell in self.cells if cell.column_id == column_id),
            "",
        )


@dataclass(frozen=True, slots=True)
class SpatialPdfTable:
    """A privacy-sensitive in-memory table reconstructed from positioned words."""

    page_count: int
    columns: tuple[SpatialPdfColumn, ...]
    records: tuple[SpatialPdfRecord, ...] = field(repr=False)
    structure_digest: str


@dataclass(frozen=True, slots=True)
class SpatialColumnMapping:
    """Explicit mapping from spatial column identifiers to transaction fields."""

    transaction_date: str
    description: str
    signed_amount: str | None = None
    debit_amount: str | None = None
    credit_amount: str | None = None
    running_balance: str | None = None

    def __post_init__(self) -> None:
        """Require one unambiguous amount representation and unique columns."""
        required = (self.transaction_date, self.description)
        if any(not value.strip() for value in required):
            raise SpatialPdfError(
                SpatialPdfErrorCode.INVALID_MAPPING,
                "date and description columns are required",
            )
        debit_pair = self.debit_amount is not None or self.credit_amount is not None
        if debit_pair and (self.debit_amount is None or self.credit_amount is None):
            raise SpatialPdfError(
                SpatialPdfErrorCode.INVALID_MAPPING,
                "debit and credit columns must be mapped together",
            )
        if (self.signed_amount is None) == (not debit_pair):
            raise SpatialPdfError(
                SpatialPdfErrorCode.INVALID_MAPPING,
                "map either one signed amount or separate debit and credit columns",
            )
        selected = tuple(
            value
            for value in (
                self.transaction_date,
                self.description,
                self.signed_amount,
                self.debit_amount,
                self.credit_amount,
                self.running_balance,
            )
            if value is not None
        )
        if any(not value.strip() for value in selected) or len(selected) != len(
            set(selected)
        ):
            raise SpatialPdfError(
                SpatialPdfErrorCode.INVALID_MAPPING,
                "mapped columns must be non-empty and unique",
            )


@dataclass(frozen=True, slots=True)
class MappedSpatialRecord:
    """A spatial row projected onto canonical transaction source fields."""

    source: SpatialPdfRecord = field(repr=False)
    transaction_date_text: str = field(repr=False)
    description_text: str = field(repr=False)
    signed_amount_text: str | None = field(default=None, repr=False)
    debit_amount_text: str | None = field(default=None, repr=False)
    credit_amount_text: str | None = field(default=None, repr=False)
    running_balance_text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SpatialPdfResult:
    """A discriminated, privacy-safe spatial reconstruction outcome."""

    state: SpatialPdfState
    page_count: int
    reason_code: str
    table: SpatialPdfTable | None = field(default=None, repr=False)
    mapping: SpatialColumnMapping | None = None
    records: tuple[MappedSpatialRecord, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _VisualLine:
    page_number: int
    line_number: int
    words: tuple[SpatialPdfWord, ...] = field(repr=False)

    @property
    def top(self) -> float:
        return min(word.top for word in self.words)

    @property
    def bottom(self) -> float:
        return max(word.bottom for word in self.words)


@dataclass(frozen=True, slots=True)
class _PageLines:
    page_number: int
    width: float
    height: float
    lines: tuple[_VisualLine, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Header:
    line_index: int
    columns: tuple[SpatialPdfColumn, ...]
    mapping: SpatialColumnMapping


_HEADER_ALIASES: Final[dict[SpatialColumnRole, tuple[tuple[str, ...], ...]]] = {
    SpatialColumnRole.TRANSACTION_DATE: (
        ("transaction", "date"),
        ("value", "date"),
        ("booking", "date"),
        ("date",),
    ),
    SpatialColumnRole.DESCRIPTION: (
        ("transaction", "details"),
        ("description",),
        ("details",),
        ("narrative",),
    ),
    SpatialColumnRole.SIGNED_AMOUNT: (
        ("transaction", "amount"),
        ("amount",),
        ("value",),
    ),
    SpatialColumnRole.DEBIT_AMOUNT: (
        ("money", "out"),
        ("paid", "out"),
        ("debit", "amount"),
        ("withdrawal",),
        ("debit",),
    ),
    SpatialColumnRole.CREDIT_AMOUNT: (
        ("money", "in"),
        ("paid", "in"),
        ("credit", "amount"),
        ("deposit",),
        ("credit",),
    ),
    SpatialColumnRole.RUNNING_BALANCE: (
        ("running", "balance"),
        ("account", "balance"),
        ("balance",),
    ),
}
_DATE_VALUE = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-](?:\d{2}|\d{4})|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+(?:\d{2}|\d{4}))",
    re.I,
)
_MONEY_VALUE = re.compile(
    r"(?:GBP\s*)?[+-]?(?:\u00a3\s*)?\(?\d+(?:,\d{3})*\.\d{2}\)?(?:\s*(?:DR|CR))?",
    re.I,
)
_PAGE_NUMBER = re.compile(r"^(?:page\s+)?\d+(?:\s+(?:of|/)\s*\d+)?$", re.I)
_STRUCTURAL_PREFIXES: Final = (
    "statement period",
    "opening balance",
    "closing balance",
    "balance brought forward",
    "balance carried forward",
    "total money in",
    "total money out",
    "total debits",
    "total credits",
)


def _normalise_token(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _line_text(line: _VisualLine) -> str:
    return " ".join(word.text for word in line.words)


def _words_to_lines(
    words: tuple[SpatialPdfWord, ...],
    *,
    page_number: int,
) -> tuple[_VisualLine, ...]:
    if not words:
        return ()
    ordered = sorted(words, key=lambda word: (word.top, word.x0))
    groups: list[list[SpatialPdfWord]] = []
    for word in ordered:
        if not groups:
            groups.append([word])
            continue
        current = groups[-1]
        current_top = min(item.top for item in current)
        current_bottom = max(item.bottom for item in current)
        overlap = min(current_bottom, word.bottom) - max(current_top, word.top)
        tolerance = max(
            2.0, min(word.bottom - word.top, current_bottom - current_top) * 0.3
        )
        if overlap >= 0 or abs(word.top - current_top) <= tolerance:
            current.append(word)
        else:
            groups.append([word])
    return tuple(
        _VisualLine(
            page_number=page_number,
            line_number=index,
            words=tuple(sorted(group, key=lambda word: word.x0)),
        )
        for index, group in enumerate(groups, start=1)
    )


def _extract_pages(
    content: bytes,
    *,
    max_pages: int,
    max_words: int,
    min_embedded_characters: int,
) -> tuple[tuple[_PageLines, ...], tuple[int, ...]]:
    try:
        document: Any = pymupdf.open(  # type: ignore[no-untyped-call]
            stream=content,
            filetype="pdf",
        )
    except (pymupdf.FileDataError, RuntimeError, ValueError) as error:
        raise SpatialPdfError(
            SpatialPdfErrorCode.MALFORMED_PDF,
            "PDF structure could not be opened safely",
        ) from error

    pages: list[_PageLines] = []
    pages_without_text: list[int] = []
    word_count = 0
    with document:
        if document.needs_pass:
            raise SpatialPdfError(
                SpatialPdfErrorCode.ENCRYPTED_PDF,
                "password-protected PDFs are not supported",
            )
        if document.page_count > max_pages:
            raise SpatialPdfError(
                SpatialPdfErrorCode.TOO_MANY_PAGES,
                "PDF exceeds the configured page limit",
            )
        for page_index in range(document.page_count):
            page_number = page_index + 1
            try:
                page: Any = document.load_page(page_index)
                raw_words: list[Any] = page.get_text("words", sort=True)
            except (RuntimeError, ValueError) as error:
                raise SpatialPdfError(
                    SpatialPdfErrorCode.MALFORMED_PDF,
                    "PDF embedded text could not be inspected safely",
                ) from error
            width = float(page.rect.width)
            height = float(page.rect.height)
            if (
                not math.isfinite(width)
                or not math.isfinite(height)
                or min(width, height) <= 0
            ):
                raise SpatialPdfError(
                    SpatialPdfErrorCode.MALFORMED_PDF,
                    "PDF contains invalid page geometry",
                )
            extracted: list[SpatialPdfWord] = []
            character_count = 0
            for raw_word in raw_words:
                if len(raw_word) < 5:
                    continue
                x0, top, x1, bottom = (float(raw_word[index]) for index in range(4))
                text = str(raw_word[4])
                if not text.strip():
                    continue
                if len(text) > _MAX_WORD_CHARACTERS or not all(
                    math.isfinite(value) for value in (x0, top, x1, bottom)
                ):
                    raise SpatialPdfError(
                        SpatialPdfErrorCode.PAGE_TOO_COMPLEX,
                        "PDF page contains unsupported embedded-text complexity",
                    )
                if x1 <= x0 or bottom <= top:
                    continue
                extracted.append(
                    SpatialPdfWord(
                        page_number=page_number,
                        x0=x0,
                        top=top,
                        x1=x1,
                        bottom=bottom,
                        text=text,
                    )
                )
                character_count += sum(character.isalnum() for character in text)
            word_count += len(extracted)
            if word_count > max_words:
                raise SpatialPdfError(
                    SpatialPdfErrorCode.PAGE_TOO_COMPLEX,
                    "PDF exceeds the configured embedded-word limit",
                )
            if character_count < min_embedded_characters:
                pages_without_text.append(page_number)
            pages.append(
                _PageLines(
                    page_number=page_number,
                    width=width,
                    height=height,
                    lines=_words_to_lines(tuple(extracted), page_number=page_number),
                )
            )
    return tuple(pages), tuple(pages_without_text)


def _header_matches(line: _VisualLine) -> dict[SpatialColumnRole, tuple[int, int]]:
    tokens = tuple(_normalise_token(word.text) for word in line.words)
    matches: dict[SpatialColumnRole, tuple[int, int]] = {}
    for role, aliases in _HEADER_ALIASES.items():
        candidates: list[tuple[int, int]] = []
        for alias in aliases:
            width = len(alias)
            for start in range(len(tokens) - width + 1):
                if tokens[start : start + width] == alias:
                    candidates.append((start, start + width))
        if candidates:
            matches[role] = max(
                candidates, key=lambda item: (item[1] - item[0], -item[0])
            )
    return matches


def _mapping_from_roles(
    role_columns: dict[SpatialColumnRole, str],
) -> SpatialColumnMapping:
    return SpatialColumnMapping(
        transaction_date=role_columns[SpatialColumnRole.TRANSACTION_DATE],
        description=role_columns[SpatialColumnRole.DESCRIPTION],
        signed_amount=role_columns.get(SpatialColumnRole.SIGNED_AMOUNT),
        debit_amount=role_columns.get(SpatialColumnRole.DEBIT_AMOUNT),
        credit_amount=role_columns.get(SpatialColumnRole.CREDIT_AMOUNT),
        running_balance=role_columns.get(SpatialColumnRole.RUNNING_BALANCE),
    )


def _header_from_line(
    line: _VisualLine,
    *,
    line_index: int,
    page_width: float,
) -> _Header | None:
    matches = _header_matches(line)
    required = {
        SpatialColumnRole.TRANSACTION_DATE,
        SpatialColumnRole.DESCRIPTION,
    }
    has_amount = SpatialColumnRole.SIGNED_AMOUNT in matches or {
        SpatialColumnRole.DEBIT_AMOUNT,
        SpatialColumnRole.CREDIT_AMOUNT,
    }.issubset(matches)
    if not required.issubset(matches) or not has_amount:
        return None

    anchors: list[tuple[SpatialColumnRole, float, float, str]] = []
    for role, (start, end) in matches.items():
        words = line.words[start:end]
        anchors.append(
            (
                role,
                min(word.x0 for word in words),
                max(word.x1 for word in words),
                " ".join(word.text for word in words),
            )
        )
    anchors.sort(key=lambda item: (item[1] + item[2]) / 2)
    roles = tuple(anchor[0] for anchor in anchors)
    if roles.index(SpatialColumnRole.TRANSACTION_DATE) > roles.index(
        SpatialColumnRole.DESCRIPTION
    ) or any(
        roles.index(role) < roles.index(SpatialColumnRole.DESCRIPTION)
        for role in (
            SpatialColumnRole.SIGNED_AMOUNT,
            SpatialColumnRole.DEBIT_AMOUNT,
            SpatialColumnRole.CREDIT_AMOUNT,
        )
        if role in roles
    ):
        return None

    centres = tuple((anchor[1] + anchor[2]) / 2 / page_width for anchor in anchors)
    boundaries = (
        0.0,
        *((left + right) / 2 for left, right in pairwise(centres)),
        1.0,
    )
    columns = tuple(
        SpatialPdfColumn(
            column_id=f"column_{index + 1}",
            left=boundaries[index],
            right=boundaries[index + 1],
            header_text=anchor[3],
            role_hint=anchor[0],
        )
        for index, anchor in enumerate(anchors)
    )
    role_columns = {column.role_hint: column.column_id for column in columns}
    return _Header(
        line_index=line_index,
        columns=columns,
        mapping=_mapping_from_roles(
            {
                role: column_id
                for role, column_id in role_columns.items()
                if role is not None
            }
        ),
    )


def _find_headers(page: _PageLines) -> tuple[_Header, ...]:
    return tuple(
        header
        for index, line in enumerate(page.lines)
        if (
            header := _header_from_line(
                line,
                line_index=index,
                page_width=page.width,
            )
        )
        is not None
    )


def _same_header_layout(first: _Header, other: _Header) -> bool:
    first_roles = tuple(column.role_hint for column in first.columns)
    other_roles = tuple(column.role_hint for column in other.columns)
    return first_roles == other_roles and all(
        abs(((left.left + left.right) / 2) - ((right.left + right.right) / 2))
        <= _HEADER_POSITION_TOLERANCE
        for left, right in zip(first.columns, other.columns, strict=True)
    )


def _cell_from_words(
    column: SpatialPdfColumn,
    words: tuple[SpatialPdfWord, ...],
) -> SpatialPdfCell:
    if not words:
        return SpatialPdfCell(column_id=column.column_id, text="")
    return SpatialPdfCell(
        column_id=column.column_id,
        text=" ".join(word.text for word in words),
        x0=min(word.x0 for word in words),
        top=min(word.top for word in words),
        x1=max(word.x1 for word in words),
        bottom=max(word.bottom for word in words),
    )


def _cells_from_line(
    line: _VisualLine,
    *,
    page_width: float,
    columns: tuple[SpatialPdfColumn, ...],
) -> tuple[SpatialPdfCell, ...]:
    grouped: dict[str, list[SpatialPdfWord]] = {
        column.column_id: [] for column in columns
    }
    for word in line.words:
        centre = ((word.x0 + word.x1) / 2) / page_width
        column = next(
            (
                candidate
                for candidate in columns
                if candidate.left <= centre < candidate.right
            ),
            columns[-1],
        )
        grouped[column.column_id].append(word)
    return tuple(
        _cell_from_words(column, tuple(grouped[column.column_id])) for column in columns
    )


def _mapping_value(
    cells: tuple[SpatialPdfCell, ...],
    column_id: str | None,
) -> str:
    if column_id is None:
        return ""
    return next((cell.text for cell in cells if cell.column_id == column_id), "")


def _looks_like_date(value: str) -> bool:
    return _DATE_VALUE.fullmatch(" ".join(value.split())) is not None


def _looks_like_money(value: str) -> bool:
    return _MONEY_VALUE.fullmatch(" ".join(value.split())) is not None


def _is_structural_line(line: _VisualLine) -> bool:
    text = " ".join(_normalise_token(_line_text(line)).split())
    return bool(
        _PAGE_NUMBER.fullmatch(text)
        or any(text.startswith(prefix) for prefix in _STRUCTURAL_PREFIXES)
    )


def _amount_values(
    cells: tuple[SpatialPdfCell, ...], mapping: SpatialColumnMapping
) -> tuple[str, ...]:
    if mapping.signed_amount is not None:
        return (_mapping_value(cells, mapping.signed_amount),)
    return (
        _mapping_value(cells, mapping.debit_amount),
        _mapping_value(cells, mapping.credit_amount),
    )


def _append_continuation(
    record: SpatialPdfRecord,
    line: _VisualLine,
    cells: tuple[SpatialPdfCell, ...],
    description_column: str,
) -> SpatialPdfRecord:
    continuation = _mapping_value(cells, description_column)
    updated_cells: list[SpatialPdfCell] = []
    for cell in record.cells:
        if cell.column_id != description_column:
            updated_cells.append(cell)
            continue
        source_cell = next(
            item for item in cells if item.column_id == description_column
        )
        updated_cells.append(
            replace(
                cell,
                text=f"{cell.text}\n{continuation}",
                x0=min(cell.x0, source_cell.x0),
                top=min(cell.top, source_cell.top),
                x1=max(cell.x1, source_cell.x1),
                bottom=max(cell.bottom, source_cell.bottom),
            )
        )
    return replace(
        record,
        source_line_numbers=(*record.source_line_numbers, line.line_number),
        cells=tuple(updated_cells),
    )


def _records_for_sections(
    page: _PageLines,
    headers: tuple[_Header, ...],
) -> tuple[SpatialPdfRecord, ...]:
    records: list[SpatialPdfRecord] = []
    for header_number, header in enumerate(headers):
        section_end = (
            headers[header_number + 1].line_index
            if header_number + 1 < len(headers)
            else len(page.lines)
        )
        previous_line: _VisualLine | None = None
        for line in page.lines[header.line_index + 1 : section_end]:
            if _is_structural_line(line):
                previous_line = line
                continue
            cells = _cells_from_line(
                line,
                page_width=page.width,
                columns=header.columns,
            )
            date_value = _mapping_value(cells, header.mapping.transaction_date)
            description = _mapping_value(cells, header.mapping.description)
            amounts = _amount_values(cells, header.mapping)
            balance = _mapping_value(cells, header.mapping.running_balance)
            has_money = any(value.strip() for value in (*amounts, balance))
            is_complete = (
                _looks_like_date(date_value)
                and bool(description.strip())
                and any(_looks_like_money(value) for value in amounts if value.strip())
            )
            if is_complete:
                records.append(
                    SpatialPdfRecord(
                        page_number=page.page_number,
                        page_record_number=len(records) + 1,
                        source_line_numbers=(line.line_number,),
                        cells=cells,
                    )
                )
            elif (
                records
                and not date_value.strip()
                and bool(description.strip())
                and not has_money
                and previous_line is not None
                and line.top - previous_line.bottom
                <= max(6.0, (previous_line.bottom - previous_line.top) * 1.5)
            ):
                records[-1] = _append_continuation(
                    records[-1],
                    line,
                    cells,
                    header.mapping.description,
                )
            elif date_value.strip() or has_money:
                records.append(
                    SpatialPdfRecord(
                        page_number=page.page_number,
                        page_record_number=len(records) + 1,
                        source_line_numbers=(line.line_number,),
                        cells=cells,
                        unresolved=True,
                    )
                )
            previous_line = line
    return tuple(records)


def _leading_date_end(words: tuple[SpatialPdfWord, ...]) -> int | None:
    for end in range(min(3, len(words)), 0, -1):
        if _looks_like_date(" ".join(word.text for word in words[:end])):
            return end
    return None


def _cluster_money_centres(values: list[float]) -> tuple[float, ...]:
    clusters: list[list[float]] = []
    for value in sorted(values):
        if clusters and abs(value - (sum(clusters[-1]) / len(clusters[-1]))) <= (
            _MONEY_CLUSTER_TOLERANCE
        ):
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return tuple(sum(cluster) / len(cluster) for cluster in clusters)


def _infer_columns(
    pages: tuple[_PageLines, ...],
) -> tuple[tuple[SpatialPdfColumn, ...], tuple[_VisualLine, ...]] | None:
    candidates: list[_VisualLine] = []
    date_centres: list[float] = []
    date_rights: list[float] = []
    money_centres: list[float] = []
    for page in pages:
        for line in page.lines:
            date_end = _leading_date_end(line.words)
            if date_end is None:
                continue
            monetary_words = tuple(
                word for word in line.words[date_end:] if _looks_like_money(word.text)
            )
            if not monetary_words:
                continue
            first_money_x = min(word.x0 for word in monetary_words)
            if not any(
                word.x0 < first_money_x and word.text.strip()
                for word in line.words[date_end:]
            ):
                continue
            candidates.append(line)
            date_words = line.words[:date_end]
            date_centres.append(
                (
                    (
                        min(word.x0 for word in date_words)
                        + max(word.x1 for word in date_words)
                    )
                    / 2
                )
                / page.width
            )
            date_rights.append(max(word.x1 for word in date_words) / page.width)
            money_centres.extend(
                ((word.x0 + word.x1) / 2) / page.width for word in monetary_words
            )
    if len(candidates) < 2:
        return None
    money_columns = _cluster_money_centres(money_centres)
    if not 1 <= len(money_columns) <= 3:
        return None
    date_centre = median(date_centres)
    description_centre = (max(date_rights) + money_columns[0]) / 2
    centres = (date_centre, description_centre, *money_columns)
    if any(right - left <= 0.02 for left, right in pairwise(centres)):
        return None
    boundaries = (
        0.0,
        *((left + right) / 2 for left, right in pairwise(centres)),
        1.0,
    )
    columns = tuple(
        SpatialPdfColumn(
            column_id=f"column_{index + 1}",
            left=boundaries[index],
            right=boundaries[index + 1],
            header_text=f"Column {index + 1}",
        )
        for index in range(len(centres))
    )
    return columns, tuple(candidates)


def _page_has_transaction_signal(page: _PageLines) -> bool:
    for line in page.lines:
        date_end = _leading_date_end(line.words)
        if date_end is None:
            continue
        monetary_words = tuple(
            word for word in line.words[date_end:] if _looks_like_money(word.text)
        )
        if monetary_words and any(
            word.x0 < min(item.x0 for item in monetary_words)
            for word in line.words[date_end:]
        ):
            return True
    return False


def _records_from_inference(
    pages: tuple[_PageLines, ...],
    columns: tuple[SpatialPdfColumn, ...],
    candidates: tuple[_VisualLine, ...],
) -> tuple[SpatialPdfRecord, ...]:
    candidate_locations = {(line.page_number, line.line_number) for line in candidates}
    records: list[SpatialPdfRecord] = []
    page_record_counts: dict[int, int] = {}
    previous_line: _VisualLine | None = None
    for page in pages:
        for line in page.lines:
            location = (line.page_number, line.line_number)
            if location in candidate_locations:
                page_record_counts[page.page_number] = (
                    page_record_counts.get(page.page_number, 0) + 1
                )
                records.append(
                    SpatialPdfRecord(
                        page_number=page.page_number,
                        page_record_number=page_record_counts[page.page_number],
                        source_line_numbers=(line.line_number,),
                        cells=_cells_from_line(
                            line,
                            page_width=page.width,
                            columns=columns,
                        ),
                    )
                )
            elif (
                records
                and previous_line is not None
                and line.page_number == records[-1].page_number
            ):
                cells = _cells_from_line(
                    line,
                    page_width=page.width,
                    columns=columns,
                )
                description = _mapping_value(cells, columns[1].column_id)
                has_other_values = any(
                    _mapping_value(cells, column.column_id).strip()
                    for column in (columns[0], *columns[2:])
                )
                if (
                    description.strip()
                    and not has_other_values
                    and line.top - previous_line.bottom
                    <= max(6.0, (previous_line.bottom - previous_line.top) * 1.5)
                ):
                    records[-1] = _append_continuation(
                        records[-1], line, cells, columns[1].column_id
                    )
            previous_line = line
    return tuple(records)


def _validate_mapping_columns(
    table: SpatialPdfTable,
    mapping: SpatialColumnMapping,
) -> None:
    known = {column.column_id for column in table.columns}
    selected = {
        value
        for value in (
            mapping.transaction_date,
            mapping.description,
            mapping.signed_amount,
            mapping.debit_amount,
            mapping.credit_amount,
            mapping.running_balance,
        )
        if value is not None
    }
    if not selected.issubset(known):
        raise SpatialPdfError(
            SpatialPdfErrorCode.INVALID_MAPPING,
            "mapping references a column outside the reconstructed table",
        )


def _map_records(
    table: SpatialPdfTable,
    mapping: SpatialColumnMapping,
) -> tuple[tuple[MappedSpatialRecord, ...], bool]:
    _validate_mapping_columns(table, mapping)
    mapped: list[MappedSpatialRecord] = []
    complete = True
    for source in table.records:
        date_value = source.value(mapping.transaction_date)
        description = source.value(mapping.description)
        signed = (
            source.value(mapping.signed_amount)
            if mapping.signed_amount is not None
            else None
        )
        debit = (
            source.value(mapping.debit_amount)
            if mapping.debit_amount is not None
            else None
        )
        credit = (
            source.value(mapping.credit_amount)
            if mapping.credit_amount is not None
            else None
        )
        balance = (
            source.value(mapping.running_balance)
            if mapping.running_balance is not None
            else None
        )
        amounts_complete = (
            bool(signed and signed.strip())
            if signed is not None
            else bool(debit and debit.strip()) != bool(credit and credit.strip())
        )
        row_complete = (
            not source.unresolved
            and _looks_like_date(date_value)
            and bool(description.strip())
            and amounts_complete
        )
        complete = complete and row_complete
        mapped.append(
            MappedSpatialRecord(
                source=source,
                transaction_date_text=date_value,
                description_text=description,
                signed_amount_text=signed,
                debit_amount_text=debit,
                credit_amount_text=credit,
                running_balance_text=balance,
            )
        )
    return tuple(mapped), complete and bool(mapped)


def _structure_digest(
    *,
    page_count: int,
    columns: tuple[SpatialPdfColumn, ...],
    records: tuple[SpatialPdfRecord, ...],
) -> str:
    payload = repr(
        (
            page_count,
            tuple(
                (
                    column.column_id,
                    round(column.left, 4),
                    round(column.right, 4),
                    column.role_hint,
                )
                for column in columns
            ),
            tuple(
                (record.page_number, record.page_record_number) for record in records
            ),
        )
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _table(
    *,
    page_count: int,
    columns: tuple[SpatialPdfColumn, ...],
    records: tuple[SpatialPdfRecord, ...],
) -> SpatialPdfTable:
    return SpatialPdfTable(
        page_count=page_count,
        columns=columns,
        records=records,
        structure_digest=_structure_digest(
            page_count=page_count,
            columns=columns,
            records=records,
        ),
    )


def reconstruct_spatial_pdf(
    content: bytes,
    *,
    mapping: SpatialColumnMapping | None = None,
    max_pages: int = DEFAULT_MAX_SPATIAL_PDF_PAGES,
    max_words: int = DEFAULT_MAX_SPATIAL_PDF_WORDS,
    min_embedded_characters: int = DEFAULT_MIN_SPATIAL_CHARACTERS,
) -> SpatialPdfResult:
    """Reconstruct a selectable-text PDF without guessing ambiguous columns.

    The returned objects contain private statement text only in fields excluded
    from ``repr``. Callers must keep the result in memory and must not log it.
    """
    if max_pages < 1 or max_words < 1 or min_embedded_characters < 1:
        raise SpatialPdfError(
            SpatialPdfErrorCode.INVALID_LIMIT,
            "spatial PDF limits must be positive",
        )
    pages, pages_without_text = _extract_pages(
        content,
        max_pages=max_pages,
        max_words=max_words,
        min_embedded_characters=min_embedded_characters,
    )
    page_count = len(pages)
    if pages_without_text:
        return SpatialPdfResult(
            state=SpatialPdfState.UNSUPPORTED_LAYOUT,
            page_count=page_count,
            reason_code="image_only_or_mixed_pdf",
        )

    page_headers = tuple((page, _find_headers(page)) for page in pages)
    headers = tuple(header for _, found in page_headers for header in found)
    if headers:
        first = headers[0]
        if any(not _same_header_layout(first, header) for header in headers[1:]) or any(
            not found and _page_has_transaction_signal(page)
            for page, found in page_headers
        ):
            return SpatialPdfResult(
                state=SpatialPdfState.UNSUPPORTED_LAYOUT,
                page_count=page_count,
                reason_code="inconsistent_page_layout",
            )
        records = tuple(
            record
            for page, found in page_headers
            for record in _records_for_sections(page, found)
        )
        if not records:
            return SpatialPdfResult(
                state=SpatialPdfState.UNSUPPORTED_LAYOUT,
                page_count=page_count,
                reason_code="no_spatial_transaction_rows",
            )
        reconstructed = _table(
            page_count=page_count,
            columns=first.columns,
            records=records,
        )
        selected_mapping = mapping or first.mapping
        mapped, complete = _map_records(reconstructed, selected_mapping)
        return SpatialPdfResult(
            state=(
                SpatialPdfState.READY if complete else SpatialPdfState.MAPPING_REQUIRED
            ),
            page_count=page_count,
            reason_code=(
                "automatic_header_mapping"
                if complete and mapping is None
                else "explicit_column_mapping"
                if complete
                else "unresolved_spatial_rows"
            ),
            table=reconstructed,
            mapping=selected_mapping,
            records=mapped,
        )

    inferred = _infer_columns(pages)
    if inferred is None:
        return SpatialPdfResult(
            state=SpatialPdfState.UNSUPPORTED_LAYOUT,
            page_count=page_count,
            reason_code="no_stable_spatial_table",
        )
    columns, candidate_lines = inferred
    records = _records_from_inference(pages, columns, candidate_lines)
    reconstructed = _table(
        page_count=page_count,
        columns=columns,
        records=records,
    )
    if mapping is None:
        return SpatialPdfResult(
            state=SpatialPdfState.MAPPING_REQUIRED,
            page_count=page_count,
            reason_code="column_mapping_required",
            table=reconstructed,
        )
    mapped, complete = _map_records(reconstructed, mapping)
    return SpatialPdfResult(
        state=SpatialPdfState.READY if complete else SpatialPdfState.MAPPING_REQUIRED,
        page_count=page_count,
        reason_code=("explicit_column_mapping" if complete else "invalid_mapped_rows"),
        table=reconstructed,
        mapping=mapping,
        records=mapped,
    )
