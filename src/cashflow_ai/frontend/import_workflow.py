"""Pure form-to-contract helpers for the thin statement-import page."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from cashflow_ai.schemas.api import PdfSourceType
from cashflow_ai.schemas.csv_imports import CsvPreview
from cashflow_ai.schemas.reconciliation import (
    RowDecision,
    RowReview,
    StatementBalanceField,
    StatementReview,
    StatementReviewRow,
)
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    DateRange,
    ImportContext,
    StatementBalances,
    StatementCoverage,
    StatementFlag,
)
from cashflow_ai.schemas.transactions import Currency, Direction, TransactionDraft


class UploadKind(StrEnum):
    """User-facing source choices mapped to the existing API adapters."""

    CSV = "CSV export"
    DIGITAL_PDF = "Digital PDF"
    OCR_PDF = "Scanned or camera PDF"

    @property
    def extensions(self) -> tuple[str, ...]:
        """Return the browser file filter for this source."""
        return ("csv",) if self is UploadKind.CSV else ("pdf",)

    @property
    def mime_type(self) -> str:
        """Return the expected media type sent to the local API."""
        return "text/csv" if self is UploadKind.CSV else "application/pdf"

    @property
    def pdf_source_type(self) -> PdfSourceType:
        """Return the API PDF adapter for a PDF source selection."""
        if self is UploadKind.DIGITAL_PDF:
            return PdfSourceType.DIGITAL_PDF
        if self is UploadKind.OCR_PDF:
            return PdfSourceType.OCR_PDF
        raise ValueError("CSV uploads do not have a PDF source type")


def optional_text(value: str) -> str | None:
    """Convert a blank form field to absence without exposing its content."""
    cleaned = value.strip()
    return cleaned or None


def optional_money(value: str) -> Decimal | None:
    """Parse a blank-or-decimal form field without echoing rejected input."""
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation as error:
        raise ValueError("enter money as a decimal number") from error


def optional_iso_date(value: str) -> date | None:
    """Parse an optional ISO date without echoing rejected input."""
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError as error:
        raise ValueError("enter dates as YYYY-MM-DD") from error


def parse_gap_ranges(value: str) -> tuple[DateRange, ...]:
    """Parse one inclusive `start,end` ISO date range per non-empty line."""
    ranges: list[DateRange] = []
    for line in value.splitlines():
        if not line.strip():
            continue
        pieces = tuple(part.strip() for part in line.split(","))
        if len(pieces) != 2:
            raise ValueError("enter each missing period as start-date,end-date")
        start = optional_iso_date(pieces[0])
        end = optional_iso_date(pieces[1])
        if start is None or end is None:
            raise ValueError("missing-period dates cannot be blank")
        ranges.append(DateRange(start_date=start, end_date=end))
    return tuple(ranges)


def build_statement_coverage(
    *,
    start_date: date,
    end_date: date,
    status: CoverageStatus,
    missing_periods_text: str,
) -> StatementCoverage:
    """Build the canonical coverage contract from explicit form values."""
    return StatementCoverage(
        statement_start_date=start_date,
        statement_end_date=end_date,
        status=status,
        missing_periods=parse_gap_ranges(missing_periods_text),
    )


def build_statement_balances(
    *,
    currency: Currency,
    opening_balance_text: str,
    closing_balance_text: str,
) -> StatementBalances | None:
    """Build balance evidence only when at least one amount was supplied."""
    opening = optional_money(opening_balance_text)
    closing = optional_money(closing_balance_text)
    if opening is None and closing is None:
        return None
    return StatementBalances(
        currency=currency,
        opening_balance=opening,
        closing_balance=closing,
    )


def build_import_context(
    *,
    account_id: str,
    coverage: StatementCoverage,
    balances: StatementBalances | None,
    flags: tuple[StatementFlag, ...],
    note: str,
) -> ImportContext:
    """Build inert statement context without interpreting notes or flags."""
    return ImportContext(
        account_id=account_id,
        coverage=coverage,
        balances=balances,
        flags=frozenset(flags),
        note=optional_text(note),
    )


def suggested_column_index(
    columns: tuple[str, ...],
    suggestions: tuple[str, ...],
    *,
    optional: bool,
) -> int:
    """Return a stable select-box index, preferring the first valid suggestion."""
    offset = 1 if optional else 0
    for suggestion in suggestions:
        if suggestion in columns:
            return columns.index(suggestion) + offset
    return 0


def csv_preview_rows(preview: CsvPreview) -> tuple[dict[str, object], ...]:
    """Pair preserved preview values with their source headings for display."""
    return tuple(
        {
            "source row": row.source_row_number,
            **dict(zip(preview.columns, row.values, strict=True)),
        }
        for row in preview.rows
    )


def pdf_review_rows(review: StatementReview) -> tuple[dict[str, object], ...]:
    """Create a local-only, readable extraction table with confidence metadata."""
    rows: list[dict[str, object]] = []
    for index, row in enumerate(review.rows, start=1):
        confidence_values = tuple(item.confidence for item in row.field_confidences)
        confidence = (
            min(confidence_values) if confidence_values else row.provenance.confidence
        )
        rows.append(
            {
                "row": index,
                "page": row.source_identity.page_number,
                "date": row.working_draft.transaction_date,
                "description": row.working_draft.description,
                "amount": row.working_draft.amount,
                "balance": row.working_draft.balance_after,
                "minimum confidence": confidence,
                "review reasons": ", ".join(
                    sorted(reason.value for reason in row.review_reasons)
                ),
            }
        )
    return tuple(rows)


def corrected_row_review(
    row: StatementReviewRow,
    *,
    decision: RowDecision,
    transaction_date_text: str,
    posting_date_text: str,
    description: str,
    amount_text: str,
    balance_after_text: str,
) -> RowReview:
    """Build one explicit row decision while retaining protected source fields."""
    if decision is RowDecision.REJECT:
        return RowReview(
            source_fingerprint=row.source_fingerprint,
            decision=decision,
        )
    transaction_date = optional_iso_date(transaction_date_text)
    amount = optional_money(amount_text)
    direction = None
    if amount is not None and amount > 0:
        direction = Direction.INFLOW
    elif amount is not None and amount < 0:
        direction = Direction.OUTFLOW
    corrected = row.working_draft.model_copy(
        update={
            "transaction_date": transaction_date,
            "posting_date": optional_iso_date(posting_date_text),
            "description": optional_text(description),
            "amount": amount,
            "balance_after": optional_money(balance_after_text),
            "direction": direction,
        }
    )
    return RowReview(
        source_fingerprint=row.source_fingerprint,
        decision=decision,
        corrected_draft=TransactionDraft.model_validate(corrected),
    )


def balances_confirmed_from_review(
    review: StatementReview,
    *,
    opening_balance_text: str,
    closing_balance_text: str,
) -> StatementBalances | None:
    """Confirm exactly the balance fields for which source evidence exists."""
    evidence_fields = {item.field for item in review.balance_evidence}
    if not evidence_fields:
        return None
    currency = next(
        row.working_draft.currency
        for row in review.rows
        if row.working_draft.currency is not None
    )
    return StatementBalances(
        currency=currency,
        opening_balance=(
            optional_money(opening_balance_text)
            if StatementBalanceField.OPENING in evidence_fields
            else None
        ),
        closing_balance=(
            optional_money(closing_balance_text)
            if StatementBalanceField.CLOSING in evidence_fields
            else None
        ),
    )


__all__ = [
    "UploadKind",
    "balances_confirmed_from_review",
    "build_import_context",
    "build_statement_balances",
    "build_statement_coverage",
    "corrected_row_review",
    "csv_preview_rows",
    "optional_iso_date",
    "optional_money",
    "optional_text",
    "parse_gap_ranges",
    "pdf_review_rows",
    "suggested_column_index",
]
