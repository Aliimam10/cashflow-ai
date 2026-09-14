"""Behaviour tests for the isolated multi-statement workspace service."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast

import pymupdf
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import cashflow_ai.workspaces.service as service
from cashflow_ai.imports import (
    PdfImportError,
    PdfImportErrorCode,
    SpatialPdfError,
    SpatialPdfErrorCode,
    calculate_file_hash,
)
from cashflow_ai.imports.spatial_pdf import (
    SpatialPdfCell,
    SpatialPdfColumn,
    SpatialPdfRecord,
    SpatialPdfTable,
)
from cashflow_ai.persistence import (
    Base,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from cashflow_ai.persistence.models import (
    SavedWorkspaceRecord,
    SavedWorkspaceTransactionRecord,
)
from cashflow_ai.persistence.workspaces import (
    SavedWorkspaceRecords,
    SavedWorkspaceRepository,
)
from cashflow_ai.schemas.csv_imports import CsvColumnMapping
from cashflow_ai.schemas.pdf_api import DigitalPdfColumnMapping
from cashflow_ai.schemas.statements import CoverageStatus, DateRange
from cashflow_ai.schemas.transactions import Currency, FinancialRole
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceBalanceConfirmation,
    WorkspaceCoverageConfirmation,
    WorkspaceCreateRequest,
    WorkspaceDuplicateDecision,
    WorkspaceEditRequest,
    WorkspaceFileMapping,
    WorkspaceFinalizeRequest,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceRowRevision,
    WorkspaceSourceRemoveRequest,
    WorkspaceSourceReviewState,
    WorkspaceSourceType,
    WorkspaceStatus,
    WorkspaceTransactionRow,
)
from cashflow_ai.workspaces import (
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceStore,
    WorkspaceUpload,
    create_workspace,
    delete_all_workspace_data,
    delete_workspace,
    edit_workspace_rows,
    finalize_workspace,
    get_latest_saved_workspace,
    get_workspace,
    remove_workspace_source,
    review_workspace_uploads,
    workspace_csv_download,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
CSV_ONE = (
    b"Date,Posting Date,Description,Amount,Balance,Transaction ID,Transaction Type\n"
    b"2026-08-01,2026-08-02,SYNTHETIC RENT,-400.00,600.00,rent-1,transfer\n"
    b"2026-08-15,2026-08-15,SYNTHETIC PAY,1000.00,1600.00,pay-1,credit\n"
)
CSV_TWO = (
    b"Date,Description,Amount,Balance,Transaction ID\n"
    b"2026-08-20,SYNTHETIC GROCER, -25.00,1575.00,grocery-1\n"
)
CSV_SIMILAR = (
    b"Date,Description,Amount,Transaction ID\n"
    b"2026-08-20,SYNTHETIC GROCER,-25.00,grocery-2\n"
)
CSV_AMBIGUOUS = b"When,What,Money\n01/08/2026,SYNTHETIC SHOP,-10.00\n"
CSV_PARTIAL_DEBIT = b"Date,Description,Debit\nnot-a-date,SYNTHETIC ROW,10.00\n"
CSV_DEBIT_CREDIT = (
    b"Date,Description,Debit,Credit\n"
    b"2026-08-03,SYNTHETIC BILL,12.00,\n"
    b"2026-08-04,SYNTHETIC REFUND,,2.00\n"
)
CSV_TOO_WIDE = (
    (",".join(f"Field {index}" for index in range(21)))
    + "\n"
    + (",".join("SYNTHETIC" for _index in range(21)))
    + "\n"
).encode()


def _fictional_consolidated_gbp_csv() -> bytes:
    def padded(*values: str) -> list[str]:
        return [*values, *("" for _ in range(13 - len(values)))]

    rows = [
        padded("Fictional consolidated statement"),
        padded(),
        padded(
            "Date",
            "Description",
            "Category",
            "Money in/out",
            "Balance",
            "Tax withheld",
            "Other taxes",
            "Fees",
        ),
        padded(
            "Aug 1, 2026",
            "SYNTHETIC RENT",
            "Bills",
            "-£400.00",
            "£600.00",
            "£0.00",
            "£0.00",
            "£0.00",
        ),
        padded(
            "Aug 15, 2026",
            "SYNTHETIC PAY",
            "Income",
            "£1,000.00",
            "£1,600.00",
            "£0.00",
            "£0.00",
            "£0.00",
        ),
        padded("Total", "", "", "£600.00"),
        padded(),
        padded(
            "Date",
            "Description",
            "Category",
            "Money in/out",
            "Money in/out",
            "Balance",
            "Balance",
            "Tax withheld",
            "Tax withheld",
            "Other taxes",
            "Other taxes",
            "Fees",
            "Fees",
        ),
        padded(
            "Aug 20, 2026",
            "SYNTHETIC FOREIGN PURCHASE",
            "Card",
            "-€10.00",
            "-£8.50",
            "€90.00",
            "£76.50",
            "€0.00",
            "£0.00",
            "€0.00",
            "£0.00",
            "€0.00",
            "£0.00",
        ),
        padded("Total", "", "", "-€10.00"),
    ]
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode()


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _upload(name: str, content: bytes, mime_type: str = "text/csv") -> WorkspaceUpload:
    return WorkspaceUpload(filename=name, content=content, mime_type=mime_type)


def _fictional_digital_statement() -> bytes:
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


def _workspace(
    store: WorkspaceStore,
    *,
    retention: WorkspaceRetentionMode = WorkspaceRetentionMode.SAVED,
) -> str:
    return create_workspace(
        store,
        WorkspaceCreateRequest(
            retention_mode=retention,
            account_name="Fictional current account",
        ),
        now=NOW,
    ).workspace_id


def _confirm_rows(store: WorkspaceStore, workspace_id: str) -> None:
    workspace = store.get(workspace_id)
    assert workspace is not None
    revisions = []
    for row in workspace.rows:
        role = (
            FinancialRole.INCOME
            if row.amount and row.amount > 0
            else FinancialRole.EXPENSE
        )
        revisions.append(
            WorkspaceRowRevision(
                row_id=row.row_id,
                expected_revision=row.revision,
                transaction_date=row.transaction_date,
                description=row.description,
                amount=row.amount,
                balance_after=row.balance_after,
                category_id="income" if role is FinancialRole.INCOME else "other",
                financial_role=role,
                review_state=WorkspaceRowReviewState.CONFIRMED,
                duplicate_decision=(
                    WorkspaceDuplicateDecision.KEEP
                    if row.probable_duplicate_of is not None
                    else None
                ),
            )
        )
    edit_workspace_rows(
        store,
        workspace_id,
        WorkspaceEditRequest(
            expected_workspace_revision=workspace.revision,
            rows=tuple(revisions),
        ),
        now=NOW,
    )


def _finalize_request(
    revision: int, *, balance: bool = True
) -> WorkspaceFinalizeRequest:
    return WorkspaceFinalizeRequest(
        expected_workspace_revision=revision,
        statement_confirmed=True,
        date_interpretation_confirmed=True,
        sign_convention_confirmed=True,
        coverage=WorkspaceCoverageConfirmation(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            status=CoverageStatus.COMPLETE,
            confirmed=True,
        ),
        balance=(
            WorkspaceBalanceConfirmation(
                balance=Decimal("1600.00"),
                as_of_date=date(2026, 8, 31),
                currency=Currency.GBP,
                confirmed=True,
            )
            if balance
            else None
        ),
    )


def test_mixed_batches_append_and_only_exact_source_identity_is_removed(
    factory: sessionmaker[Session],
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)

    first = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("one.csv", CSV_ONE), _upload("two.csv", CSV_TWO)),
        now=NOW,
    )
    assert first.accepted_files == 2
    assert len(first.workspace.rows) == 3
    assert first.workspace.rows[0].posting_date == date(2026, 8, 2)
    assert first.workspace.rows[0].external_id == "rent-1"
    assert first.workspace.rows[0].transaction_type == "transfer"

    duplicate_file = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("renamed-copy.csv", CSV_ONE),),
        now=NOW,
    )
    assert duplicate_file.exact_duplicates_removed == 2
    assert len(duplicate_file.workspace.sources) == 2
    assert len(duplicate_file.workspace.rows) == 3

    similar = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("similar.csv", CSV_SIMILAR),),
        now=NOW,
    )
    assert similar.probable_duplicates == 1
    assert len(similar.workspace.rows) == 4
    assert (
        similar.workspace.rows[-1].review_state is WorkspaceRowReviewState.NEEDS_REVIEW
    )


def test_consolidated_gbp_csv_is_selected_with_original_row_provenance(
    factory: sessionmaker[Session],
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)

    review = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("fictional-consolidated.csv", _fictional_consolidated_gbp_csv()),),
        now=NOW,
    )

    assert review.accepted_files == 1
    assert review.workspace.sources[0].reason_code == ("consolidated_csv_review_ready")
    assert "1 transaction row(s) from non-GBP sections were excluded" in (
        review.workspace.sources[0].guidance
    )
    assert review.workspace.sources[0].parser_name == "revolut_consolidated_csv"
    assert review.workspace.sources[0].parser_version == "1.0.0"
    assert review.workspace.sources[0].layout_version == "consolidated_v2_gbp_1"
    assert review.workspace.sources[0].warning_codes == (
        "non_gbp_transaction_sections_excluded",
    )
    assert review.workspace.sources[0].excluded_transaction_rows == 1
    assert [row.source_record_number for row in review.workspace.rows] == [4, 5]
    assert [row.amount for row in review.workspace.rows] == [
        Decimal("-400.00"),
        Decimal("1000.00"),
    ]
    assert all(
        row.review_state is WorkspaceRowReviewState.READY
        for row in review.workspace.rows
    )
    assert [row.transaction_type for row in review.workspace.rows] == [
        "Bills",
        "Income",
    ]

    _confirm_rows(store, workspace_id)
    reviewed = store.get(workspace_id)
    assert reviewed is not None
    with pytest.raises(WorkspaceError, match="non-GBP source exclusions"):
        finalize_workspace(
            store,
            factory,
            workspace_id,
            _finalize_request(reviewed.revision),
        )
    finalized = finalize_workspace(
        store,
        factory,
        workspace_id,
        _finalize_request(reviewed.revision).model_copy(
            update={"source_exclusions_confirmed": True}
        ),
    )
    assert finalized.workspace.status is WorkspaceStatus.FINALIZED


def test_mapping_one_file_preserves_ready_files_rows_and_user_decisions(
    factory: sessionmaker[Session],
) -> None:
    del factory
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    initial = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("ready.csv", CSV_ONE), _upload("unknown.csv", CSV_AMBIGUOUS)),
    )
    assert initial.mapping_required_files == 1
    mapping_source = next(
        source
        for source in initial.workspace.sources
        if source.state is WorkspaceSourceReviewState.MAPPING_REQUIRED
    )
    first_ready = initial.workspace.rows[0]
    edit_workspace_rows(
        store,
        workspace_id,
        WorkspaceEditRequest(
            expected_workspace_revision=initial.workspace.revision,
            rows=(
                WorkspaceRowRevision(
                    row_id=first_ready.row_id,
                    expected_revision=first_ready.revision,
                    transaction_date=first_ready.transaction_date,
                    description="EDITED SYNTHETIC RENT",
                    amount=first_ready.amount,
                    balance_after=first_ready.balance_after,
                    category_id="housing",
                    financial_role=FinancialRole.EXPENSE,
                    review_state=WorkspaceRowReviewState.CONFIRMED,
                ),
            ),
        ),
    )
    before_mapping = store.get(workspace_id)
    assert before_mapping is not None
    mapping = WorkspaceFileMapping(
        file_hash=mapping_source.file_hash,
        source_type=WorkspaceSourceType.CSV,
        csv_mapping=CsvColumnMapping(
            transaction_date_column="When",
            description_column="What",
            signed_amount_column="Money",
        ),
    )
    mapped = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("unknown.csv", CSV_AMBIGUOUS),),
        mappings=(mapping,),
    )

    assert mapped.mapping_required_files == 0
    assert len(mapped.workspace.sources) == 2
    assert len(mapped.workspace.rows) == 3
    preserved = next(
        row for row in mapped.workspace.rows if row.row_id == first_ready.row_id
    )
    assert preserved.description == "EDITED SYNTHETIC RENT"
    assert preserved.merchant is None
    assert preserved.review_state is WorkspaceRowReviewState.CONFIRMED


def test_finalize_saved_workspace_round_trips_canonical_only_and_deletes(
    factory: sessionmaker[Session],
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    review_workspace_uploads(store, workspace_id, (_upload("one.csv", CSV_ONE),))
    _confirm_rows(store, workspace_id)
    reviewed = store.get(workspace_id)
    assert reviewed is not None

    with pytest.raises(WorkspaceError) as missing_balance:
        finalize_workspace(
            store,
            factory,
            workspace_id,
            _finalize_request(reviewed.revision, balance=False),
        )
    assert missing_balance.value.code is WorkspaceErrorCode.REVIEW_REQUIRED

    result = finalize_workspace(
        store,
        factory,
        workspace_id,
        _finalize_request(reviewed.revision),
        now=NOW,
    )
    assert result.persisted is True
    assert result.included_rows == 2
    assert result.rejected_rows == 0
    assert result.workspace.status is WorkspaceStatus.FINALIZED
    assert result.workspace.sources == ()
    assert all(
        row.source_id is None and row.page_number is None
        for row in result.workspace.rows
    )

    download = workspace_csv_download(store, factory, workspace_id)
    assert download.row_count == 2
    assert "posting_date" in download.content
    assert "rent-1" in download.content

    store.clear()
    restored = get_latest_saved_workspace(store, factory)
    assert restored == result.workspace
    assert get_latest_saved_workspace(store, factory) == restored
    assert get_workspace(store, factory, workspace_id) == restored

    deleted = delete_workspace(store, factory, workspace_id)
    assert deleted.deleted is True
    with pytest.raises(WorkspaceError) as missing:
        get_workspace(store, factory, workspace_id)
    assert missing.value.code is WorkspaceErrorCode.NOT_FOUND
    with pytest.raises(WorkspaceError) as missing_delete:
        delete_workspace(store, factory, workspace_id)
    assert missing_delete.value.code is WorkspaceErrorCode.NOT_FOUND


def test_temporary_finalization_never_writes_sqlite(
    factory: sessionmaker[Session],
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store, retention=WorkspaceRetentionMode.TEMPORARY)
    review_workspace_uploads(store, workspace_id, (_upload("one.csv", CSV_ONE),))
    _confirm_rows(store, workspace_id)
    reviewed = store.get(workspace_id)
    assert reviewed is not None
    result = finalize_workspace(
        store,
        factory,
        workspace_id,
        _finalize_request(reviewed.revision),
    )
    assert result.persisted is False
    with session_scope(factory) as session:
        assert (
            session.scalar(select(func.count()).select_from(SavedWorkspaceRecord)) == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(SavedWorkspaceTransactionRecord)
            )
            == 0
        )
    store.clear()
    with pytest.raises(WorkspaceError):
        get_workspace(store, factory, workspace_id)


def test_workspace_csv_neutralises_formula_leading_text(
    factory: sessionmaker[Session],
) -> None:
    row = WorkspaceTransactionRow(
        row_id="safe-export-row",
        transaction_date=date(2026, 8, 1),
        description='=HYPERLINK("unsafe")',
        merchant="+SYNTHETIC",
        amount=Decimal("-1.00"),
        category_id="other",
        financial_role=FinancialRole.EXPENSE,
        external_id="-external",
        transaction_type="@formula",
        review_state=WorkspaceRowReviewState.CONFIRMED,
    )
    finalized = StatementWorkspace(
        workspace_id="safe-export-workspace",
        retention_mode=WorkspaceRetentionMode.TEMPORARY,
        status=WorkspaceStatus.FINALIZED,
        account_name="Fictional account",
        revision=2,
        rows=(row,),
        coverage=WorkspaceCoverageConfirmation(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            status=CoverageStatus.COMPLETE,
            confirmed=True,
        ),
        created_at=NOW,
        updated_at=NOW,
        finalized_at=NOW,
    )
    store = WorkspaceStore()
    store.add(finalized)

    download = workspace_csv_download(store, factory, finalized.workspace_id)
    exported = next(iter(csv.DictReader(StringIO(download.content))))

    assert exported["description"] == '\'=HYPERLINK("unsafe")'
    assert exported["merchant"] == "'+SYNTHETIC"
    assert exported["external_id"] == "'-external"
    assert exported["transaction_type"] == "'@formula"
    assert exported["category"] == "other"


def test_delete_all_workspace_data_clears_active_and_saved_state(
    factory: sessionmaker[Session],
) -> None:
    store = WorkspaceStore()
    saved_id = _workspace(store)
    review_workspace_uploads(store, saved_id, (_upload("saved.csv", CSV_ONE),))
    _confirm_rows(store, saved_id)
    saved = store.get(saved_id)
    assert saved is not None
    finalize_workspace(
        store,
        factory,
        saved_id,
        _finalize_request(saved.revision),
        now=NOW,
    )
    temporary_id = _workspace(store, retention=WorkspaceRetentionMode.TEMPORARY)

    result = delete_all_workspace_data(store, factory)

    assert result.active_workspaces_deleted == 2
    assert result.saved_workspaces_deleted == 1
    assert store.count() == 0
    with session_scope(factory) as session:
        assert (
            session.scalar(select(func.count()).select_from(SavedWorkspaceRecord)) == 0
        )
    with pytest.raises(WorkspaceError):
        get_workspace(store, factory, saved_id)
    with pytest.raises(WorkspaceError):
        get_workspace(store, factory, temporary_id)


def test_delete_keeps_active_workspace_when_database_deletion_fails(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)

    def fail_delete(repository: SavedWorkspaceRepository, selected_id: str) -> bool:
        del repository, selected_id
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(SavedWorkspaceRepository, "delete", fail_delete)
    with pytest.raises(RuntimeError, match="synthetic database failure"):
        delete_workspace(store, factory, workspace_id)
    assert store.get(workspace_id) is not None


def test_saved_finalization_serializes_persistence_and_memory_replacement(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    review_workspace_uploads(store, workspace_id, (_upload("one.csv", CSV_ONE),))
    _confirm_rows(store, workspace_id)
    reviewed = store.get(workspace_id)
    assert reviewed is not None

    state = {"locked": False}
    original_locked = store.locked
    original_replace = store.replace
    original_save = SavedWorkspaceRepository.save

    @contextmanager
    def tracked_lock(
        selected_workspace_id: str,
    ) -> Iterator[StatementWorkspace | None]:
        with original_locked(selected_workspace_id) as workspace:
            state["locked"] = True
            try:
                yield workspace
            finally:
                state["locked"] = False

    def tracked_replace(
        workspace: StatementWorkspace,
        *,
        expected_revision: int,
    ) -> StatementWorkspace:
        if workspace.status is WorkspaceStatus.FINALIZED:
            assert state["locked"] is True
        return original_replace(workspace, expected_revision=expected_revision)

    def tracked_save(
        repository: SavedWorkspaceRepository,
        records: SavedWorkspaceRecords,
    ) -> SavedWorkspaceRecords:
        assert state["locked"] is True
        return original_save(repository, records)

    monkeypatch.setattr(store, "locked", tracked_lock)
    monkeypatch.setattr(store, "replace", tracked_replace)
    monkeypatch.setattr(SavedWorkspaceRepository, "save", tracked_save)

    result = finalize_workspace(
        store,
        factory,
        workspace_id,
        _finalize_request(reviewed.revision),
        now=NOW,
    )

    assert result.persisted is True
    assert state["locked"] is False


def test_finalization_reports_a_missing_workspace_safely(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(WorkspaceError) as missing:
        finalize_workspace(
            WorkspaceStore(),
            factory,
            "missing-workspace",
            _finalize_request(1),
        )
    assert missing.value.code is WorkspaceErrorCode.NOT_FOUND


def test_empty_saved_lookup_and_draft_download_are_review_gated(
    factory: sessionmaker[Session],
) -> None:
    store = WorkspaceStore()
    with pytest.raises(WorkspaceError) as missing:
        get_latest_saved_workspace(store, factory)
    assert missing.value.code is WorkspaceErrorCode.NOT_FOUND

    workspace_id = _workspace(store)
    with pytest.raises(WorkspaceError) as draft_download:
        workspace_csv_download(store, factory, workspace_id)
    assert draft_download.value.code is WorkspaceErrorCode.NOT_FINALIZED


def test_invalid_rows_and_unsupported_inputs_remain_visible_and_safe() -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    malformed_row = (
        b"Date,Description,Amount\n"
        b"not-a-date,SYNTHETIC BROKEN,-10.00\n"
        b"2026-08-02,SYNTHETIC OK,-2.00\n"
    )
    review = review_workspace_uploads(
        store,
        workspace_id,
        (
            _upload("rows.csv", malformed_row),
            _upload("notes.txt", b"fictional", "text/plain"),
            _upload("\x00.csv", b"broken"),
        ),
    )
    assert review.accepted_files == 1
    assert review.unsupported_files == 2
    assert len(review.workspace.rows) == 2
    assert review.workspace.rows[0].review_state is WorkspaceRowReviewState.NEEDS_REVIEW
    assert review.workspace.rows[0].issue_codes == ("invalid_source_row",)
    assert review.workspace.sources[-1].display_name == "statement"
    assert review.workspace.sources[1].source_type is WorkspaceSourceType.UNSUPPORTED


def test_workspace_rejects_invalid_limits_mappings_revisions_and_coverage(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    with pytest.raises(WorkspaceError, match="between 1"):
        review_workspace_uploads(store, workspace_id, ())
    with pytest.raises(WorkspaceError, match="between 1"):
        review_workspace_uploads(
            store,
            workspace_id,
            tuple(_upload(f"{index}.csv", CSV_TWO) for index in range(21)),
        )
    monkeypatch.setattr(service, "MAX_WORKSPACE_UPLOAD_BYTES", 1)
    with pytest.raises(WorkspaceError, match="too large"):
        review_workspace_uploads(store, workspace_id, (_upload("one.csv", CSV_ONE),))
    monkeypatch.setattr(service, "MAX_WORKSPACE_UPLOAD_BYTES", 50 * 1024 * 1024)

    ambiguous = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("unknown.csv", CSV_AMBIGUOUS),),
    )
    source = ambiguous.workspace.sources[0]
    mapping = WorkspaceFileMapping(
        file_hash=source.file_hash,
        source_type=WorkspaceSourceType.CSV,
        csv_mapping=CsvColumnMapping(
            transaction_date_column="When",
            description_column="What",
            signed_amount_column="Money",
        ),
    )
    with pytest.raises(WorkspaceError, match="only one mapping"):
        review_workspace_uploads(
            store,
            workspace_id,
            (_upload("unknown.csv", CSV_AMBIGUOUS),),
            mappings=(mapping, mapping),
        )
    with pytest.raises(WorkspaceError, match="not uploaded"):
        review_workspace_uploads(
            store,
            workspace_id,
            (_upload("changed.csv", CSV_AMBIGUOUS + b"\n"),),
            mappings=(mapping,),
        )

    current = store.get(workspace_id)
    assert current is not None
    with pytest.raises(WorkspaceError) as bad_revision:
        edit_workspace_rows(
            store,
            workspace_id,
            WorkspaceEditRequest(
                expected_workspace_revision=current.revision + 1,
                rows=(
                    WorkspaceRowRevision(
                        row_id="missing-row",
                        expected_revision=1,
                        review_state=WorkspaceRowReviewState.REJECTED,
                    ),
                ),
            ),
        )
    assert bad_revision.value.code is WorkspaceErrorCode.REVISION_CONFLICT

    with pytest.raises(WorkspaceError) as not_ready:
        finalize_workspace(
            store,
            factory,
            workspace_id,
            _finalize_request(current.revision),
        )
    assert not_ready.value.code is WorkspaceErrorCode.REVIEW_REQUIRED


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (PdfImportErrorCode.OCR_REQUIRED, "image_only_or_scanned_pdf"),
        (PdfImportErrorCode.MALFORMED_PDF, "unsupported_pdf_layout"),
    ],
)
def test_pdf_failures_are_returned_as_csv_guidance(
    code: PdfImportErrorCode,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PdfImportError(code, "private source details must not escape")

    monkeypatch.setattr(service, "extract_text_pdf", fail)
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    result = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("fictional.pdf", b"%PDF-fictional", "application/pdf"),),
    )
    assert result.unsupported_files == 1
    assert result.workspace.sources[0].reason_code == reason
    assert "CSV" in result.workspace.sources[0].guidance


def test_store_enforces_identity_and_revision() -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    workspace = store.get(workspace_id)
    assert workspace is not None
    with pytest.raises(ValueError, match="already exists"):
        store.add(workspace)
    with pytest.raises(KeyError):
        store.replace(
            workspace.model_copy(update={"workspace_id": "missing", "revision": 2}),
            expected_revision=1,
        )
    with pytest.raises(RuntimeError, match="revision changed"):
        store.replace(workspace.model_copy(update={"revision": 2}), expected_revision=2)
    with pytest.raises(ValueError, match="advance exactly once"):
        store.replace(workspace.model_copy(update={"revision": 3}), expected_revision=1)
    with store.locked(workspace_id) as locked:
        assert locked == workspace
        store.replace(workspace.model_copy(update={"revision": 2}), expected_revision=1)
    with store.locked("missing-workspace") as missing:
        assert missing is None
    assert store.delete("missing") is False
    assert store.delete(workspace_id) is True
    store.clear()


def test_draft_compare_and_swap_failures_are_controlled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    workspace = store.get(workspace_id)
    assert workspace is not None
    updated = workspace.model_copy(update={"revision": 2})

    def missing(
        replacement: StatementWorkspace,
        *,
        expected_revision: int,
    ) -> StatementWorkspace:
        del replacement, expected_revision
        raise KeyError("simulated deletion")

    monkeypatch.setattr(store, "replace", missing)
    with pytest.raises(WorkspaceError) as not_found:
        service._replace_draft(store, updated, expected_revision=1)
    assert not_found.value.code is WorkspaceErrorCode.NOT_FOUND

    def changed(
        replacement: StatementWorkspace,
        *,
        expected_revision: int,
    ) -> StatementWorkspace:
        del replacement, expected_revision
        raise RuntimeError("simulated concurrent edit")

    monkeypatch.setattr(store, "replace", changed)
    with pytest.raises(WorkspaceError) as conflict:
        service._replace_draft(store, updated, expected_revision=1)
    assert conflict.value.code is WorkspaceErrorCode.REVISION_CONFLICT


def test_mixed_csv_and_real_selectable_text_pdf_are_combined_in_memory() -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)

    review = review_workspace_uploads(
        store,
        workspace_id,
        (
            _upload("fictional.csv", CSV_TWO),
            _upload(
                "fictional.pdf",
                _fictional_digital_statement(),
                "application/pdf",
            ),
        ),
        now=NOW,
    )

    assert review.accepted_files == 2
    assert review.mapping_required_files == 0
    assert review.unsupported_files == 0
    assert len(review.workspace.sources) == 2
    assert {row.source_type for row in review.workspace.rows} == {
        WorkspaceSourceType.CSV,
        WorkspaceSourceType.DIGITAL_PDF,
    }
    pdf_rows = tuple(
        row
        for row in review.workspace.rows
        if row.source_type is WorkspaceSourceType.DIGITAL_PDF
    )
    assert tuple(row.page_number for row in pdf_rows) == (1, 1)
    assert review.workspace.sources[1].suggested_period == DateRange(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
    )


def test_csv_partial_mapping_and_all_invalid_dates_remain_review_gated() -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    first = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("partial.csv", CSV_PARTIAL_DEBIT),),
    )
    assert first.mapping_required_files == 1
    source = first.workspace.sources[0]
    assert source.suggested_period is None

    valid_mapping = WorkspaceFileMapping(
        file_hash=source.file_hash,
        source_type=WorkspaceSourceType.CSV,
        csv_mapping=CsvColumnMapping(
            transaction_date_column="Date",
            description_column="Description",
            signed_amount_column="Debit",
        ),
    )
    mapped = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("partial.csv", CSV_PARTIAL_DEBIT),),
        mappings=(valid_mapping,),
    )
    assert mapped.accepted_files == 1
    assert mapped.workspace.sources[0].suggested_period is None
    assert mapped.workspace.rows[0].issue_codes == ("invalid_source_row",)

    separate_store = WorkspaceStore()
    separate_id = _workspace(separate_store)
    separate = review_workspace_uploads(
        separate_store,
        separate_id,
        (_upload("debit-credit.csv", CSV_DEBIT_CREDIT),),
    )
    assert separate.accepted_files == 1
    assert tuple(row.amount for row in separate.workspace.rows) == (
        Decimal("-12.00"),
        Decimal("2.00"),
    )

    wide_store = WorkspaceStore()
    wide_id = _workspace(wide_store)
    wide = review_workspace_uploads(
        wide_store,
        wide_id,
        (_upload("wide.csv", CSV_TOO_WIDE),),
    )
    assert wide.unsupported_files == 1
    assert wide.workspace.sources[0].reason_code == "csv_mapping_too_wide"


def test_pdf_mapping_is_bound_to_exact_bytes_and_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"fictional selectable PDF bytes"
    file_hash = calculate_file_hash(content)
    columns = (
        SpatialPdfColumn("column_1", 0, 10, "Date"),
        SpatialPdfColumn("column_2", 10, 20, "Description"),
        SpatialPdfColumn("column_3", 20, 30, "Amount"),
    )
    long_description = "SYNTHETIC SHOP " + ("X" * 1_100)
    record = SpatialPdfRecord(
        page_number=1,
        page_record_number=1,
        source_line_numbers=(1,),
        cells=(
            SpatialPdfCell("column_1", "01/08/2026"),
            SpatialPdfCell("column_2", long_description),
            SpatialPdfCell("column_3", "-1.00"),
        ),
    )
    table = SpatialPdfTable(
        page_count=1,
        columns=columns,
        records=(record,),
        structure_digest="b" * 64,
    )
    mapping = DigitalPdfColumnMapping(
        file_hash=file_hash,
        structure_digest=table.structure_digest,
        transaction_date="column_1",
        description="column_2",
        signed_amount="column_3",
    )

    with pytest.raises(WorkspaceError, match="PDF changed"):
        service._pdf_spatial_mapping(
            content,
            mapping.model_copy(update={"file_hash": "c" * 64}),
        )

    def reconstruction_error(content: bytes) -> None:
        del content
        raise SpatialPdfError(SpatialPdfErrorCode.MALFORMED_PDF, "safe")

    monkeypatch.setattr(service, "reconstruct_spatial_pdf", reconstruction_error)
    with pytest.raises(WorkspaceError, match="no longer provides"):
        service._pdf_spatial_mapping(content, mapping)

    monkeypatch.setattr(
        service,
        "reconstruct_spatial_pdf",
        lambda _content: SimpleNamespace(table=None),
    )
    with pytest.raises(WorkspaceError, match="table changed"):
        service._pdf_spatial_mapping(content, mapping)

    changed_table = SpatialPdfTable(
        page_count=1,
        columns=columns,
        records=(record,),
        structure_digest="d" * 64,
    )
    monkeypatch.setattr(
        service,
        "reconstruct_spatial_pdf",
        lambda _content: SimpleNamespace(table=changed_table),
    )
    with pytest.raises(WorkspaceError, match="table changed"):
        service._pdf_spatial_mapping(content, mapping)

    monkeypatch.setattr(
        service,
        "reconstruct_spatial_pdf",
        lambda _content: SimpleNamespace(table=table),
    )
    selected = service._pdf_spatial_mapping(content, mapping)
    assert selected.signed_amount == "column_3"

    upload = _upload("fictional.pdf", content, "application/pdf")
    source = service._pdf_mapping_source(
        upload,
        file_hash=file_hash,
        reason_code="pdf_columns_ambiguous",
    )
    assert source.state is WorkspaceSourceReviewState.MAPPING_REQUIRED
    assert source.mapping_sample_rows[0][1].startswith("SYNTHETIC SHOP")
    assert len(source.mapping_sample_rows[0][1]) == 1_000

    monkeypatch.setattr(
        service,
        "reconstruct_spatial_pdf",
        lambda _content: SimpleNamespace(table=None),
    )
    unsupported = service._pdf_mapping_source(
        upload,
        file_hash=file_hash,
        reason_code="pdf_columns_ambiguous",
    )
    assert unsupported.state is WorkspaceSourceReviewState.UNSUPPORTED

    wide_columns = tuple(
        SpatialPdfColumn(f"column_{index}", index, index + 1, f"Field {index}")
        for index in range(21)
    )
    wide_record = SpatialPdfRecord(
        page_number=1,
        page_record_number=1,
        source_line_numbers=(1,),
        cells=tuple(
            SpatialPdfCell(column.column_id, "SYNTHETIC") for column in wide_columns
        ),
    )
    wide_table = SpatialPdfTable(
        page_count=1,
        columns=wide_columns,
        records=(wide_record,),
        structure_digest="e" * 64,
    )
    monkeypatch.setattr(
        service,
        "reconstruct_spatial_pdf",
        lambda _content: SimpleNamespace(table=wide_table),
    )
    wide_source = service._pdf_mapping_source(
        upload,
        file_hash=file_hash,
        reason_code="pdf_columns_ambiguous",
    )
    assert wide_source.state is WorkspaceSourceReviewState.UNSUPPORTED


def test_no_transaction_pdf_falls_back_to_safe_csv_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_transactions(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PdfImportError(PdfImportErrorCode.NO_TRANSACTIONS, "private details")

    def no_spatial_table(content: bytes) -> None:
        del content
        raise SpatialPdfError(SpatialPdfErrorCode.MALFORMED_PDF, "safe")

    monkeypatch.setattr(service, "extract_text_pdf", no_transactions)
    monkeypatch.setattr(service, "reconstruct_spatial_pdf", no_spatial_table)
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    result = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("fictional.pdf", b"%PDF synthetic", "application/pdf"),),
    )
    assert result.unsupported_files == 1
    assert result.workspace.sources[0].reason_code == "unsupported_pdf_layout"


def test_rededuplication_cleans_stale_links_and_preserves_terminal_decisions() -> None:
    ready = WorkspaceTransactionRow(
        row_id="row-1",
        transaction_date=date(2026, 8, 1),
        description="SYNTHETIC SHOP",
        amount=Decimal("-1.00"),
        financial_role=FinancialRole.EXPENSE,
        review_state=WorkspaceRowReviewState.READY,
    )
    stale = ready.model_copy(
        update={
            "row_id": "row-2",
            "review_state": WorkspaceRowReviewState.NEEDS_REVIEW,
            "probable_duplicate_of": "removed-row",
            "issue_codes": ("probable_duplicate",),
        }
    )
    retained, exact_count, probable_count = service._deduplicate_rows([stale], {})
    assert exact_count == 0
    assert probable_count == 0
    assert retained[0].review_state is WorkspaceRowReviewState.READY
    assert retained[0].probable_duplicate_of is None

    still_uncertain = stale.model_copy(
        update={"issue_codes": ("probable_duplicate", "invalid_source_row")}
    )
    cleaned, _, _ = service._deduplicate_rows([still_uncertain], {})
    assert cleaned[0].review_state is WorkspaceRowReviewState.NEEDS_REVIEW
    assert cleaned[0].issue_codes == ("invalid_source_row",)

    confirmed = ready.model_copy(
        update={
            "row_id": "row-confirmed",
            "review_state": WorkspaceRowReviewState.CONFIRMED,
        }
    )
    rejected = ready.model_copy(
        update={
            "row_id": "row-rejected",
            "review_state": WorkspaceRowReviewState.REJECTED,
        }
    )
    terminal, _, probable_count = service._deduplicate_rows(
        [ready, confirmed, rejected],
        {},
    )
    assert probable_count == 0
    assert tuple(row.review_state for row in terminal) == (
        WorkspaceRowReviewState.READY,
        WorkspaceRowReviewState.CONFIRMED,
        WorkspaceRowReviewState.REJECTED,
    )

    external_first = ready.model_copy(
        update={"row_id": "external-1", "external_id": "reference-1"}
    )
    external_second = ready.model_copy(
        update={"row_id": "external-2", "external_id": "REFERENCE-1"}
    )
    external, exact_count, _ = service._deduplicate_rows(
        [external_first, external_second],
        {},
    )
    assert len(external) == 1
    assert exact_count == 1


def test_mapping_scope_and_combined_file_limit_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    pending = review_workspace_uploads(
        store,
        workspace_id,
        (_upload("unknown.csv", CSV_AMBIGUOUS),),
    ).workspace.sources[0]
    pdf_mapping = WorkspaceFileMapping(
        file_hash=pending.file_hash,
        source_type=WorkspaceSourceType.DIGITAL_PDF,
        pdf_mapping=DigitalPdfColumnMapping(
            file_hash=pending.file_hash,
            structure_digest="b" * 64,
            transaction_date="column_1",
            description="column_2",
            signed_amount="column_3",
        ),
    )
    with pytest.raises(WorkspaceError, match="awaiting review"):
        review_workspace_uploads(
            store,
            workspace_id,
            (_upload("unknown.pdf", CSV_AMBIGUOUS, "application/pdf"),),
            mappings=(pdf_mapping,),
        )

    csv_mapping = WorkspaceFileMapping(
        file_hash=pending.file_hash,
        source_type=WorkspaceSourceType.CSV,
        csv_mapping=CsvColumnMapping(
            transaction_date_column="When",
            description_column="What",
            signed_amount_column="Money",
        ),
    )
    with pytest.raises(WorkspaceError, match="does not match"):
        review_workspace_uploads(
            store,
            workspace_id,
            (_upload("unknown.pdf", CSV_AMBIGUOUS, "application/pdf"),),
            mappings=(csv_mapping,),
        )

    limit_store = WorkspaceStore()
    limit_id = _workspace(limit_store)
    monkeypatch.setattr(service, "MAX_WORKSPACE_FILES", 1)
    review_workspace_uploads(
        limit_store,
        limit_id,
        (_upload("one.csv", CSV_ONE),),
    )
    with pytest.raises(WorkspaceError, match="no more than 1"):
        review_workspace_uploads(
            limit_store,
            limit_id,
            (_upload("two.csv", CSV_TWO),),
        )


def test_unusable_source_can_be_removed_without_losing_valid_rows() -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    review = review_workspace_uploads(
        store,
        workspace_id,
        (
            _upload("one.csv", CSV_ONE),
            _upload("unsupported.txt", b"fictional", "text/plain"),
        ),
    )
    unsupported = next(
        source
        for source in review.workspace.sources
        if source.state is WorkspaceSourceReviewState.UNSUPPORTED
    )
    with pytest.raises(WorkspaceError, match="workspace changed"):
        remove_workspace_source(
            store,
            workspace_id,
            unsupported.source_id,
            WorkspaceSourceRemoveRequest(
                expected_workspace_revision=review.workspace.revision + 1
            ),
        )
    with pytest.raises(WorkspaceError, match="source does not exist"):
        remove_workspace_source(
            store,
            workspace_id,
            "missing-source",
            WorkspaceSourceRemoveRequest(
                expected_workspace_revision=review.workspace.revision
            ),
        )

    updated = remove_workspace_source(
        store,
        workspace_id,
        unsupported.source_id,
        WorkspaceSourceRemoveRequest(
            expected_workspace_revision=review.workspace.revision
        ),
        now=NOW,
    )
    assert len(updated.sources) == 1
    assert updated.sources[0].state is WorkspaceSourceReviewState.READY
    assert len(updated.rows) == 2
    assert {row.description for row in updated.rows} == {
        "SYNTHETIC RENT",
        "SYNTHETIC PAY",
    }


def test_row_edit_conflicts_probable_decisions_and_finalized_guard(
    factory: sessionmaker[Session],
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store, retention=WorkspaceRetentionMode.TEMPORARY)
    review_workspace_uploads(
        store,
        workspace_id,
        (_upload("two.csv", CSV_TWO), _upload("similar.csv", CSV_SIMILAR)),
    )
    workspace = store.get(workspace_id)
    assert workspace is not None
    base = workspace.rows[0]

    with pytest.raises(WorkspaceError, match="no longer exists"):
        edit_workspace_rows(
            store,
            workspace_id,
            WorkspaceEditRequest(
                expected_workspace_revision=workspace.revision,
                rows=(
                    WorkspaceRowRevision(
                        row_id="missing-row",
                        expected_revision=1,
                        transaction_date=base.transaction_date,
                        description=base.description,
                        amount=base.amount,
                        financial_role=FinancialRole.EXPENSE,
                        review_state=WorkspaceRowReviewState.CONFIRMED,
                    ),
                ),
            ),
        )

    with pytest.raises(WorkspaceError, match="row changed"):
        edit_workspace_rows(
            store,
            workspace_id,
            WorkspaceEditRequest(
                expected_workspace_revision=workspace.revision,
                rows=(
                    WorkspaceRowRevision(
                        row_id=base.row_id,
                        expected_revision=base.revision + 1,
                        transaction_date=base.transaction_date,
                        description=base.description,
                        amount=base.amount,
                        financial_role=FinancialRole.EXPENSE,
                        review_state=WorkspaceRowReviewState.CONFIRMED,
                    ),
                ),
            ),
        )

    with pytest.raises(WorkspaceError, match="only to flagged"):
        edit_workspace_rows(
            store,
            workspace_id,
            WorkspaceEditRequest(
                expected_workspace_revision=workspace.revision,
                rows=(
                    WorkspaceRowRevision(
                        row_id=base.row_id,
                        expected_revision=base.revision,
                        transaction_date=base.transaction_date,
                        description=base.description,
                        amount=base.amount,
                        financial_role=FinancialRole.EXPENSE,
                        review_state=WorkspaceRowReviewState.CONFIRMED,
                        duplicate_decision=WorkspaceDuplicateDecision.KEEP,
                    ),
                ),
            ),
        )

    probable = workspace.rows[1]
    assert probable.probable_duplicate_of is not None
    with pytest.raises(WorkspaceError, match="explicit keep or reject"):
        edit_workspace_rows(
            store,
            workspace_id,
            WorkspaceEditRequest(
                expected_workspace_revision=workspace.revision,
                rows=(
                    WorkspaceRowRevision(
                        row_id=probable.row_id,
                        expected_revision=probable.revision,
                        transaction_date=probable.transaction_date,
                        description=probable.description,
                        amount=probable.amount,
                        financial_role=FinancialRole.EXPENSE,
                        review_state=WorkspaceRowReviewState.CONFIRMED,
                    ),
                ),
            ),
        )

    _confirm_rows(store, workspace_id)
    reviewed = store.get(workspace_id)
    assert reviewed is not None
    finalized = finalize_workspace(
        store,
        factory,
        workspace_id,
        _finalize_request(reviewed.revision),
    )
    with pytest.raises(WorkspaceError, match="cannot accept"):
        review_workspace_uploads(
            store,
            workspace_id,
            (_upload("extra.csv", CSV_ONE),),
        )
    assert finalized.workspace.status is WorkspaceStatus.FINALIZED


def test_finalization_rejects_stale_empty_unresolved_and_invalid_evidence(
    factory: sessionmaker[Session],
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store, retention=WorkspaceRetentionMode.TEMPORARY)
    review_workspace_uploads(store, workspace_id, (_upload("one.csv", CSV_ONE),))
    draft = store.get(workspace_id)
    assert draft is not None

    with pytest.raises(WorkspaceError, match="refresh it"):
        finalize_workspace(
            store,
            factory,
            workspace_id,
            _finalize_request(draft.revision + 1),
        )
    with pytest.raises(WorkspaceError, match="confirm or reject"):
        finalize_workspace(
            store,
            factory,
            workspace_id,
            _finalize_request(draft.revision),
        )

    reject_revisions = tuple(
        WorkspaceRowRevision(
            row_id=row.row_id,
            expected_revision=row.revision,
            review_state=WorkspaceRowReviewState.REJECTED,
        )
        for row in draft.rows
    )
    rejected = edit_workspace_rows(
        store,
        workspace_id,
        WorkspaceEditRequest(
            expected_workspace_revision=draft.revision,
            rows=reject_revisions,
        ),
    )
    with pytest.raises(WorkspaceError, match="confirm or reject"):
        finalize_workspace(
            store,
            factory,
            workspace_id,
            _finalize_request(rejected.revision),
        )

    store = WorkspaceStore()
    workspace_id = _workspace(store, retention=WorkspaceRetentionMode.TEMPORARY)
    review_workspace_uploads(store, workspace_id, (_upload("one.csv", CSV_ONE),))
    _confirm_rows(store, workspace_id)
    reviewed = store.get(workspace_id)
    assert reviewed is not None

    outside_coverage = _finalize_request(reviewed.revision).model_copy(
        update={
            "coverage": WorkspaceCoverageConfirmation(
                start_date=date(2026, 8, 2),
                end_date=date(2026, 8, 31),
                status=CoverageStatus.COMPLETE,
                confirmed=True,
            )
        }
    )
    with pytest.raises(WorkspaceError, match="fall within"):
        finalize_workspace(store, factory, workspace_id, outside_coverage)

    gapped = _finalize_request(reviewed.revision).model_copy(
        update={
            "coverage": WorkspaceCoverageConfirmation(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
                status=CoverageStatus.GAPPED,
                missing_periods=(
                    DateRange(
                        start_date=date(2026, 8, 1),
                        end_date=date(2026, 8, 1),
                    ),
                ),
                confirmed=True,
            )
        }
    )
    with pytest.raises(WorkspaceError, match="missing period"):
        finalize_workspace(store, factory, workspace_id, gapped)

    balance_outside = _finalize_request(reviewed.revision).model_copy(
        update={
            "balance": WorkspaceBalanceConfirmation(
                balance=Decimal("1600.00"),
                as_of_date=date(2026, 9, 1),
                currency=Currency.GBP,
                confirmed=True,
            )
        }
    )
    with pytest.raises(WorkspaceError, match="balance date"):
        finalize_workspace(store, factory, workspace_id, balance_outside)


def test_saved_projection_defenses_and_serialized_restore(
    factory: sessionmaker[Session],
) -> None:
    store = WorkspaceStore()
    workspace_id = _workspace(store)
    draft = store.get(workspace_id)
    assert draft is not None
    incomplete = draft.model_copy(
        update={"status": WorkspaceStatus.FINALIZED, "finalized_at": NOW}
    )
    with pytest.raises(RuntimeError, match="projection is incomplete"):
        service._saved_records(incomplete, ())

    broken_records = SavedWorkspaceRecords(
        workspace=SavedWorkspaceRecord(
            id="broken-workspace",
            account_name="Fictional",
            retention_mode="saved",
            status="finalized",
            revision=1,
            created_at=NOW,
            finalized_at=NOW,
        ),
        transactions=(),
        coverage=None,
        balance=None,
    )
    with pytest.raises(RuntimeError, match="missing confirmed coverage"):
        service._workspace_from_records(broken_records)

    review_workspace_uploads(store, workspace_id, (_upload("one.csv", CSV_ONE),))
    _confirm_rows(store, workspace_id)
    reviewed = store.get(workspace_id)
    assert reviewed is not None
    finalize_workspace(
        store,
        factory,
        workspace_id,
        _finalize_request(reviewed.revision),
        now=NOW,
    )
    store.clear()
    restored = get_workspace(store, factory, workspace_id)
    assert restored.workspace_id == workspace_id

    latest_store = WorkspaceStore()
    latest = get_latest_saved_workspace(latest_store, factory)
    assert latest.workspace_id == workspace_id

    missing_store = WorkspaceStore()
    with pytest.raises(WorkspaceError, match="does not exist"):
        review_workspace_uploads(
            missing_store,
            "missing-workspace",
            (_upload("one.csv", CSV_ONE),),
        )
