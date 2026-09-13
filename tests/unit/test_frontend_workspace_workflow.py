"""Pure presentation-workflow tests for the editable statement table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

import cashflow_ai.frontend.workspace_workflow as workflow
from cashflow_ai.frontend.client import UploadedDocument
from cashflow_ai.frontend.workspace_workflow import (
    EditorDecision,
    build_coverage_confirmation,
    build_edit_request,
    editor_rows,
    source_type_for_document,
    suggested_coverage,
    uploaded_documents,
)
from cashflow_ai.schemas.statements import CoverageStatus, DateRange
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceSourceFile,
    WorkspaceSourceReviewState,
    WorkspaceSourceType,
    WorkspaceStatus,
    WorkspaceTransactionRow,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


@dataclass
class Upload:
    name: str
    content: bytes

    def getvalue(self) -> bytes:
        return self.content


def _row(
    row_id: str,
    *,
    state: WorkspaceRowReviewState = WorkspaceRowReviewState.READY,
    probable: str | None = None,
) -> WorkspaceTransactionRow:
    return WorkspaceTransactionRow(
        row_id=row_id,
        source_id="source-1",
        source_type=WorkspaceSourceType.CSV,
        source_record_number=1,
        transaction_date=date(2026, 8, 2),
        description="SYNTHETIC SHOP",
        amount=Decimal("-10.00"),
        balance_after=Decimal("90.00"),
        category_id="other",
        financial_role=FinancialRole.UNKNOWN,
        review_state=state,
        probable_duplicate_of=probable,
        issue_codes=("probable_duplicate",) if probable else (),
    )


def _workspace(
    *,
    rows: tuple[WorkspaceTransactionRow, ...] = (),
    sources: tuple[WorkspaceSourceFile, ...] = (),
) -> StatementWorkspace:
    return StatementWorkspace(
        workspace_id="workspace-1",
        retention_mode=WorkspaceRetentionMode.SAVED,
        status=WorkspaceStatus.DRAFT,
        account_name="Fictional account",
        revision=2,
        sources=sources,
        rows=rows,
        created_at=NOW,
        updated_at=NOW,
    )


def test_uploaded_documents_accepts_mixed_files_without_retaining_widgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = uploaded_documents(
        (
            Upload("folder/fictional.csv", b"csv"),
            Upload("fictional.PDF", b"pdf"),
        )
    )
    assert tuple(item.filename for item in documents) == (
        "fictional.csv",
        "fictional.PDF",
    )
    assert tuple(item.mime_type for item in documents) == (
        "text/csv",
        "application/pdf",
    )

    with pytest.raises(ValueError, match="at least one"):
        uploaded_documents(())
    with pytest.raises(ValueError, match="no more than"):
        uploaded_documents(tuple(Upload(f"{i}.csv", b"x") for i in range(21)))
    with pytest.raises(ValueError, match="only CSV"):
        uploaded_documents((Upload("notes.txt", b"x"),))
    with pytest.raises(ValueError, match="empty"):
        uploaded_documents((Upload("empty.csv", b""),))
    one_megabyte = 1024 * 1024
    oversized = b"x" * (one_megabyte + 1)
    monkeypatch.setattr(workflow, "DEFAULT_MAX_CSV_BYTES", one_megabyte)
    with pytest.raises(ValueError, match="CSV statement must be 1 MB"):
        uploaded_documents((Upload("large.csv", oversized),))
    monkeypatch.setattr(workflow, "DEFAULT_MAX_PDF_BYTES", one_megabyte)
    with pytest.raises(ValueError, match="PDF statement must be 1 MB"):
        uploaded_documents((Upload("large.pdf", oversized),))


def test_editor_projection_uses_plain_decisions_and_safe_source_fallbacks() -> None:
    source = WorkspaceSourceFile(
        source_id="source-1",
        source_type=WorkspaceSourceType.CSV,
        display_name="fictional.csv",
        file_hash="a" * 64,
        state=WorkspaceSourceReviewState.READY,
        reason_code="ready",
        guidance="Review.",
        row_count=3,
    )
    rows = (
        _row("ready"),
        _row("confirmed", state=WorkspaceRowReviewState.CONFIRMED),
        _row("rejected", state=WorkspaceRowReviewState.REJECTED),
    )
    projected = editor_rows(_workspace(rows=rows, sources=(source,)))
    assert [item["Decision"] for item in projected] == [
        EditorDecision.REVIEW.value,
        EditorDecision.INCLUDE.value,
        EditorDecision.EXCLUDE.value,
    ]
    assert all(item["Source"] == "fictional.csv" for item in projected)
    assert all(item["Source location"] == "Row 1" for item in projected)

    detached = rows[0].model_copy(
        update={
            "source_id": None,
            "source_type": None,
            "source_record_number": None,
        }
    )
    unknown = rows[0].model_copy(update={"source_id": "missing"})
    sources = editor_rows(_workspace(rows=(detached, unknown), sources=(source,)))
    assert [item["Source"] for item in sources] == ["Manual edit", "Unknown source"]
    assert [item["Source location"] for item in sources] == [
        "Manual row",
        "Row 1",
    ]

    pdf_row = rows[0].model_copy(
        update={
            "source_type": WorkspaceSourceType.DIGITAL_PDF,
            "page_number": 2,
        }
    )
    page_only = pdf_row.model_copy(update={"source_record_number": None})
    pdf_source = source.model_copy(
        update={
            "source_type": WorkspaceSourceType.DIGITAL_PDF,
            "display_name": "fictional.pdf",
            "page_count": 2,
        }
    )
    locations = editor_rows(
        _workspace(rows=(pdf_row, page_only), sources=(pdf_source,))
    )
    assert [item["Source location"] for item in locations] == [
        "Page 2, row 1",
        "Page 2",
    ]


def test_build_edit_request_parses_values_and_duplicate_decisions() -> None:
    probable = _row("probable", probable="original")
    regular = _row("regular")
    workspace = _workspace(rows=(probable, regular))
    request = build_edit_request(
        workspace,
        (
            {
                "Date": datetime(2026, 8, 3, tzinfo=UTC),
                "Description": " EDITED SHOP ",
                "Amount": "-12.34",
                "Balance": "",
                "Category": "shopping",
                "Financial role": "expense",
                "Decision": "Include",
            },
            {
                "Date": "2026-08-04",
                "Description": "SYNTHETIC REJECT",
                "Amount": "-1.00",
                "Balance": None,
                "Category": "other",
                "Financial role": "unknown",
                "Decision": "Exclude",
            },
        ),
    )
    assert request.rows[0].transaction_date == date(2026, 8, 3)
    assert request.rows[0].amount == Decimal("-12.34")
    duplicate_decision = request.rows[0].duplicate_decision
    assert duplicate_decision is not None
    assert duplicate_decision.value == "keep"
    assert request.rows[1].review_state is WorkspaceRowReviewState.REJECTED

    excluded_duplicate = build_edit_request(
        _workspace(rows=(probable,)),
        (
            {
                "Decision": "Exclude",
                "Financial role": "unknown",
                "Category": "other",
            },
        ),
    )
    rejected_decision = excluded_duplicate.rows[0].duplicate_decision
    assert rejected_decision is not None
    assert rejected_decision.value == "reject"


def test_bulk_include_confirms_only_clean_rows() -> None:
    clean = _row("clean")
    uncertain = _row("uncertain").model_copy(
        update={
            "review_state": WorkspaceRowReviewState.NEEDS_REVIEW,
            "issue_codes": ("uncertain_amount",),
        }
    )
    workspace = _workspace(rows=(clean, uncertain))
    values = tuple(
        {
            **item,
            "Financial role": "expense",
        }
        for item in editor_rows(workspace)
    )

    request = build_edit_request(workspace, values, include_all_clean=True)

    assert tuple(row.row_id for row in request.rows) == ("clean",)
    assert request.rows[0].review_state is WorkspaceRowReviewState.CONFIRMED


def test_build_edit_request_rejects_invalid_or_unchanged_editor_shapes() -> None:
    workspace = _workspace(rows=(_row("row-1"),))
    with pytest.raises(ValueError, match="shape changed"):
        build_edit_request(workspace, ())
    with pytest.raises(ValueError, match="choose Include"):
        build_edit_request(workspace, ({"Decision": "Maybe"},))
    with pytest.raises(ValueError, match="mark at least one"):
        build_edit_request(workspace, ({"Decision": "Review needed"},))
    with pytest.raises(ValueError, match="check each included"):
        build_edit_request(
            workspace,
            (
                {
                    "Decision": "Include",
                    "Date": "31/08/2026",
                    "Description": "SYNTHETIC",
                    "Amount": "-1.00",
                    "Category": "other",
                    "Financial role": "expense",
                },
            ),
        )
    with pytest.raises(ValueError, match="amounts must be numbers"):
        workflow._optional_decimal("not-money")
    with pytest.raises(ValueError, match="fractions of a penny"):
        workflow._optional_decimal("1.001")
    with pytest.raises(ValueError, match="check each included"):
        build_edit_request(
            workspace,
            (
                {
                    "Decision": "Include",
                    "Date": object(),
                    "Description": "SYNTHETIC",
                    "Amount": "not-money",
                    "Category": "other",
                    "Financial role": "expense",
                },
            ),
        )
    with pytest.raises(ValueError, match="check each included"):
        build_edit_request(
            workspace,
            (
                {
                    "Decision": "Include",
                    "Date": date(2026, 8, 1),
                    "Description": "SYNTHETIC",
                    "Amount": True,
                    "Category": "other",
                    "Financial role": "expense",
                },
            ),
        )


def test_coverage_suggestions_and_confirmation_keep_gaps_explicit() -> None:
    source = WorkspaceSourceFile(
        source_id="source-1",
        source_type=WorkspaceSourceType.CSV,
        display_name="fictional.csv",
        file_hash="a" * 64,
        state=WorkspaceSourceReviewState.READY,
        reason_code="ready",
        guidance="Review.",
        row_count=1,
        suggested_period=DateRange(
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
        ),
    )
    assert suggested_coverage(_workspace(rows=(_row("row"),), sources=(source,))) == (
        date(2026, 7, 1),
        date(2026, 8, 2),
    )
    today = date.today()
    assert suggested_coverage(_workspace()) == (today, today)

    coverage = build_coverage_confirmation(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        status=CoverageStatus.GAPPED,
        missing_periods_text="\n2026-08-10, 2026-08-11\n",
    )
    assert coverage.missing_periods[0].start_date == date(2026, 8, 10)
    with pytest.raises(ValueError, match="one start,end"):
        build_coverage_confirmation(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            status=CoverageStatus.GAPPED,
            missing_periods_text="2026-08-10",
        )
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        build_coverage_confirmation(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            status=CoverageStatus.GAPPED,
            missing_periods_text="10/08/2026,11/08/2026",
        )


def test_source_type_uses_validated_mime_type() -> None:
    csv_document = UploadedDocument("fictional.csv", b"x", "text/csv")
    pdf_document = UploadedDocument("fictional.pdf", b"x", "application/pdf")
    assert source_type_for_document(csv_document) is WorkspaceSourceType.CSV
    assert source_type_for_document(pdf_document) is WorkspaceSourceType.DIGITAL_PDF
