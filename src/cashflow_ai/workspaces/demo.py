"""Run a private, entirely fictional mixed-statement workspace walkthrough."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pymupdf
from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.persistence import (
    Base,
    SavedWorkspaceRepository,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.schemas.statements import CoverageStatus
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspaces import (
    WorkspaceBalanceConfirmation,
    WorkspaceCoverageConfirmation,
    WorkspaceCreateRequest,
    WorkspaceEditRequest,
    WorkspaceFinalizeRequest,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceRowRevision,
)
from cashflow_ai.workspaces.service import (
    WorkspaceUpload,
    create_workspace,
    delete_workspace,
    edit_workspace_rows,
    finalize_workspace,
    get_latest_saved_workspace,
    review_workspace_uploads,
    workspace_csv_download,
)
from cashflow_ai.workspaces.store import WorkspaceStore

_NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
_CSV = (
    b"Date,Description,Amount,Balance,Transaction ID\n"
    b"20/08/2026,SYNTHETIC GROCER,-25.00,85.00,grocery-1\n"
)
_TEMPORARY_CSV = b"Date,Description,Amount\n25/08/2026,SYNTHETIC BOOK,-8.00\n"


def _digital_pdf() -> bytes:
    document: Any = pymupdf.open()  # type: ignore[no-untyped-call]
    page = document.new_page(width=595, height=842)
    lines = (
        "Fictional current account statement",
        "Statement period: 01 August 2026 to 31 August 2026",
        "Opening balance: GBP 100.00",
        "Date | Description | Amount | Balance",
        "05/08/2026 | SYNTHETIC CAFE | -10.00 | 90.00",
        "06/08/2026 | SYNTHETIC REFUND | +20.00 | 110.00",
        "Closing balance: GBP 110.00",
    )
    for index, line in enumerate(lines):
        page.insert_text((35, 45 + index * 24), line, fontsize=9)
    content = cast(bytes, document.tobytes())
    document.close()
    return content


def _confirm_every_row(store: WorkspaceStore, workspace_id: str) -> None:
    workspace = store.get(workspace_id)
    if workspace is None:
        raise RuntimeError("synthetic workspace unexpectedly disappeared")
    revisions = tuple(
        WorkspaceRowRevision(
            row_id=row.row_id,
            expected_revision=row.revision,
            transaction_date=row.transaction_date,
            description=row.description,
            amount=row.amount,
            balance_after=row.balance_after,
            category_id=(
                "groceries"
                if row.description is not None and "GROCER" in row.description
                else "other"
            ),
            financial_role=(
                FinancialRole.REFUND
                if row.amount is not None and row.amount > 0
                else FinancialRole.EXPENSE
            ),
            review_state=WorkspaceRowReviewState.CONFIRMED,
        )
        for row in workspace.rows
    )
    edit_workspace_rows(
        store,
        workspace_id,
        WorkspaceEditRequest(
            expected_workspace_revision=workspace.revision,
            rows=revisions,
        ),
        now=_NOW,
    )


def _finalize(
    store: WorkspaceStore,
    workspace_id: str,
    factory: sessionmaker[Session],
    *,
    balance: WorkspaceBalanceConfirmation | None,
) -> None:
    workspace = store.get(workspace_id)
    if workspace is None:
        raise RuntimeError("synthetic workspace unexpectedly disappeared")
    finalize_workspace(
        store,
        factory,
        workspace_id,
        WorkspaceFinalizeRequest(
            expected_workspace_revision=workspace.revision,
            statement_confirmed=True,
            date_interpretation_confirmed=True,
            sign_convention_confirmed=True,
            coverage=WorkspaceCoverageConfirmation(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
                status=CoverageStatus.COMPLETE,
                confirmed=True,
            ),
            balance=balance,
        ),
        now=_NOW,
    )


def main() -> None:
    """Demonstrate mixed review, minimal persistence, and temporary cleanup."""
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    store = WorkspaceStore()

    saved = create_workspace(
        store,
        WorkspaceCreateRequest(account_name="Fictional current account"),
        now=_NOW,
    )
    review = review_workspace_uploads(
        store,
        saved.workspace_id,
        (
            WorkspaceUpload("fictional.csv", _CSV, "text/csv"),
            WorkspaceUpload("fictional.pdf", _digital_pdf(), "application/pdf"),
        ),
        now=_NOW,
    )
    _confirm_every_row(store, saved.workspace_id)
    _finalize(
        store,
        saved.workspace_id,
        factory,
        balance=WorkspaceBalanceConfirmation(
            balance=Decimal("85.00"),
            as_of_date=date(2026, 8, 20),
            confirmed=True,
        ),
    )
    download = workspace_csv_download(store, factory, saved.workspace_id)
    store.clear()
    restored = get_latest_saved_workspace(store, factory)
    delete_workspace(store, factory, saved.workspace_id)

    temporary = create_workspace(
        store,
        WorkspaceCreateRequest(
            retention_mode=WorkspaceRetentionMode.TEMPORARY,
            account_name="Fictional temporary account",
        ),
        now=_NOW,
    )
    review_workspace_uploads(
        store,
        temporary.workspace_id,
        (WorkspaceUpload("temporary.csv", _TEMPORARY_CSV, "text/csv"),),
        now=_NOW,
    )
    _confirm_every_row(store, temporary.workspace_id)
    _finalize(store, temporary.workspace_id, factory, balance=None)
    store.clear()  # This is the same cleanup performed when the local API stops.
    with session_scope(factory) as session:
        temporary_persisted = (
            SavedWorkspaceRepository(session).get(temporary.workspace_id) is not None
        )

    print("CashFlow AI synthetic statement-workspace check")
    print(f"mixed sources accepted: {review.accepted_files}")
    print(f"combined canonical rows: {download.row_count}")
    print(f"saved rows restored: {len(restored.rows)}")
    print("original CSV/PDF bytes persisted: no")
    print(f"temporary workspace persisted: {('no', 'yes')[temporary_persisted]}")
    print(
        "temporary workspace in memory after API-stop cleanup: "
        f"{('no', 'yes')[store.get(temporary.workspace_id) is not None]}"
    )
    engine.dispose()


__all__ = ["main"]
