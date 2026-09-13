"""Pure presentation helpers for the editable statement workspace."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import PurePath
from typing import Protocol

from cashflow_ai.frontend.client import UploadedDocument
from cashflow_ai.imports import DEFAULT_MAX_CSV_BYTES, DEFAULT_MAX_PDF_BYTES
from cashflow_ai.schemas.statements import CoverageStatus, DateRange
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceCoverageConfirmation,
    WorkspaceDuplicateDecision,
    WorkspaceEditRequest,
    WorkspaceRowReviewState,
    WorkspaceRowRevision,
    WorkspaceSourceType,
)

MAX_WORKSPACE_FILES = 20


class EditorDecision(StrEnum):
    """Plain-language row choices shown in the spreadsheet."""

    REVIEW = "Review needed"
    INCLUDE = "Include"
    EXCLUDE = "Exclude"


CATEGORY_OPTIONS = (
    "food",
    "groceries",
    "restaurants",
    "transport",
    "housing",
    "utilities",
    "entertainment",
    "shopping",
    "health",
    "income",
    "transfers",
    "other",
)


class UploadedFileLike(Protocol):
    """Minimal structural upload shape used without retaining widget objects."""

    name: str

    def getvalue(self) -> bytes:
        """Return bytes owned by the current Streamlit widget run."""
        ...


def uploaded_documents(
    files: Sequence[UploadedFileLike],
) -> tuple[UploadedDocument, ...]:
    """Validate a bounded mixed upload and copy it only into ephemeral documents."""
    if not files:
        raise ValueError("choose at least one CSV or digital PDF")
    if len(files) > MAX_WORKSPACE_FILES:
        raise ValueError(f"choose no more than {MAX_WORKSPACE_FILES} files at once")

    documents: list[UploadedDocument] = []
    for uploaded in files:
        suffix = PurePath(uploaded.name).suffix.casefold()
        mime_type = {
            ".csv": "text/csv",
            ".pdf": "application/pdf",
        }.get(suffix)
        if mime_type is None:
            raise ValueError("only CSV exports and selectable-text PDFs are supported")
        content = uploaded.getvalue()
        if not content:
            raise ValueError("an uploaded statement is empty")
        max_bytes = DEFAULT_MAX_CSV_BYTES if suffix == ".csv" else DEFAULT_MAX_PDF_BYTES
        if len(content) > max_bytes:
            format_name = "CSV" if suffix == ".csv" else "PDF"
            raise ValueError(
                f"each {format_name} statement must be "
                f"{max_bytes // (1024 * 1024)} MB or smaller"
            )
        documents.append(
            UploadedDocument(
                filename=PurePath(uploaded.name).name,
                content=content,
                mime_type=mime_type,
            )
        )
    return tuple(documents)


def editor_rows(workspace: StatementWorkspace) -> list[dict[str, object]]:
    """Build an editable projection without exposing internal row identifiers."""
    sources = {source.source_id: source.display_name for source in workspace.sources}
    return [
        {
            "Source": (
                "Manual edit"
                if row.source_id is None
                else sources.get(row.source_id, "Unknown source")
            ),
            "Source location": (
                f"Page {row.page_number}, row {row.source_record_number}"
                if row.page_number is not None and row.source_record_number is not None
                else f"Page {row.page_number}"
                if row.page_number is not None
                else f"Row {row.source_record_number}"
                if row.source_record_number is not None
                else "Manual row"
            ),
            "Date": row.transaction_date,
            "Description": row.description or "",
            "Amount": "" if row.amount is None else str(row.amount),
            "Balance": "" if row.balance_after is None else str(row.balance_after),
            "Category": row.category_id,
            "Financial role": row.financial_role.value,
            "Decision": _editor_decision(row.review_state).value,
            "Review note": ", ".join(row.issue_codes),
        }
        for row in workspace.rows
    ]


def _editor_decision(state: WorkspaceRowReviewState) -> EditorDecision:
    if state is WorkspaceRowReviewState.CONFIRMED:
        return EditorDecision.INCLUDE
    if state is WorkspaceRowReviewState.REJECTED:
        return EditorDecision.EXCLUDE
    return EditorDecision.REVIEW


def _optional_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as error:
            raise ValueError("dates must use YYYY-MM-DD") from error
    raise ValueError("dates must use YYYY-MM-DD")


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("amounts must be numbers")
    try:
        parsed = Decimal(str(value))
        pennies = parsed.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("amounts must be numbers") from error
    if parsed != pennies:
        raise ValueError("amounts cannot contain fractions of a penny")
    return pennies


def build_edit_request(
    workspace: StatementWorkspace,
    values: Sequence[Mapping[str, object]],
    *,
    include_all_clean: bool = False,
) -> WorkspaceEditRequest:
    """Validate edits and optionally confirm every extraction-clean row."""
    if len(values) != len(workspace.rows):
        raise ValueError("the table shape changed; reload the workspace and try again")

    revisions: list[WorkspaceRowRevision] = []
    for original, edited in zip(workspace.rows, values, strict=True):
        try:
            decision = EditorDecision(str(edited.get("Decision", "")))
        except ValueError as error:
            raise ValueError(
                "choose Include or Exclude for every reviewed row"
            ) from error
        if decision is EditorDecision.REVIEW:
            clean = (
                original.review_state is WorkspaceRowReviewState.READY
                and original.probable_duplicate_of is None
                and not original.issue_codes
            )
            if not include_all_clean or not clean:
                continue
            decision = EditorDecision.INCLUDE
        rejected = decision is EditorDecision.EXCLUDE
        duplicate_decision = (
            WorkspaceDuplicateDecision.REJECT
            if rejected and original.probable_duplicate_of is not None
            else WorkspaceDuplicateDecision.KEEP
            if original.probable_duplicate_of is not None
            else None
        )
        try:
            revisions.append(
                WorkspaceRowRevision(
                    row_id=original.row_id,
                    expected_revision=original.revision,
                    transaction_date=_optional_date(edited.get("Date")),
                    description=str(edited.get("Description") or "").strip() or None,
                    amount=_optional_decimal(edited.get("Amount")),
                    balance_after=_optional_decimal(edited.get("Balance")),
                    category_id=str(edited.get("Category") or "other"),
                    financial_role=FinancialRole(
                        str(edited.get("Financial role") or "unknown")
                    ),
                    review_state=(
                        WorkspaceRowReviewState.REJECTED
                        if rejected
                        else WorkspaceRowReviewState.CONFIRMED
                    ),
                    duplicate_decision=duplicate_decision,
                )
            )
        except ValueError as error:
            raise ValueError(
                "check each included row's date, description, amount, "
                "category, and role"
            ) from error
    if not revisions:
        raise ValueError("mark at least one row Include or Exclude before saving")
    return WorkspaceEditRequest(
        expected_workspace_revision=workspace.revision,
        rows=tuple(revisions),
    )


def suggested_coverage(workspace: StatementWorkspace) -> tuple[date, date]:
    """Suggest bounded coverage without claiming that transaction dates prove it."""
    periods = tuple(
        source.suggested_period
        for source in workspace.sources
        if source.suggested_period is not None
    )
    row_dates = tuple(
        row.transaction_date
        for row in workspace.rows
        if row.transaction_date is not None
    )
    today = date.today()
    starts = tuple(period.start_date for period in periods) + row_dates
    ends = tuple(period.end_date for period in periods) + row_dates
    return min(starts, default=today), max(ends, default=today)


def build_coverage_confirmation(
    *,
    start_date: date,
    end_date: date,
    status: CoverageStatus,
    missing_periods_text: str,
) -> WorkspaceCoverageConfirmation:
    """Parse explicit coverage gaps without treating absent dates as zero activity."""
    gaps: list[DateRange] = []
    for raw_line in missing_periods_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = tuple(part.strip() for part in line.split(","))
        if len(parts) != 2:
            raise ValueError("enter one start,end missing-date range per line")
        try:
            gaps.append(
                DateRange(
                    start_date=date.fromisoformat(parts[0]),
                    end_date=date.fromisoformat(parts[1]),
                )
            )
        except ValueError as error:
            raise ValueError("missing dates must use YYYY-MM-DD") from error
    return WorkspaceCoverageConfirmation(
        start_date=start_date,
        end_date=end_date,
        status=status,
        missing_periods=tuple(gaps),
        confirmed=True,
    )


def source_type_for_document(document: UploadedDocument) -> WorkspaceSourceType:
    """Return the canonical visible source type for one validated document."""
    return (
        WorkspaceSourceType.CSV
        if document.mime_type == "text/csv"
        else WorkspaceSourceType.DIGITAL_PDF
    )


__all__ = [
    "CATEGORY_OPTIONS",
    "MAX_WORKSPACE_FILES",
    "EditorDecision",
    "UploadedFileLike",
    "build_coverage_confirmation",
    "build_edit_request",
    "editor_rows",
    "source_type_for_document",
    "suggested_coverage",
    "uploaded_documents",
]
