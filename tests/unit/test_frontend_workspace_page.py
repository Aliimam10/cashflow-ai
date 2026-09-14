"""Streamlit interaction tests for the statement workspace page."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

import cashflow_ai.frontend.workspace_page as page
from cashflow_ai.frontend.client import (
    ApiClientError,
    ApiClientErrorCode,
    UploadedDocument,
)
from cashflow_ai.frontend.session import FrontendSessionState
from cashflow_ai.frontend.workspace_page import (
    _balance_confirmation,
    _blank_session,
    _build_source_mapping,
    _render_delete_all_control,
    _render_editor,
    _render_file_upload,
    _render_finalization,
    _render_source_review,
    _render_workspace_controls,
    _render_workspace_start,
    _render_workspace_status,
    _session_with_workspace,
    render_workspace_page,
)
from cashflow_ai.schemas.statements import CoverageStatus, DateRange
from cashflow_ai.schemas.transactions import FinancialRole
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceCoverageConfirmation,
    WorkspaceCsvDownload,
    WorkspaceDeleteAllResult,
    WorkspaceDeleteResult,
    WorkspaceFinalizeResult,
    WorkspaceImportReview,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceSourceFile,
    WorkspaceSourceReviewState,
    WorkspaceSourceType,
    WorkspaceStatus,
    WorkspaceTransactionRow,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _source(
    *,
    state: WorkspaceSourceReviewState = WorkspaceSourceReviewState.READY,
    source_type: WorkspaceSourceType = WorkspaceSourceType.CSV,
    excluded_transaction_rows: int = 0,
) -> WorkspaceSourceFile:
    mapping = state is WorkspaceSourceReviewState.MAPPING_REQUIRED
    return WorkspaceSourceFile(
        source_id="source-1",
        source_type=source_type,
        display_name="fictional.pdf"
        if source_type is WorkspaceSourceType.DIGITAL_PDF
        else "fictional.csv",
        file_hash="a" * 64,
        state=state,
        reason_code="review_ready" if not mapping else "columns_ambiguous",
        guidance="Review this fictional source.",
        row_count=1 if state is WorkspaceSourceReviewState.READY else 0,
        parser_name=("revolut_consolidated_csv" if excluded_transaction_rows else None),
        parser_version="1.0.0" if excluded_transaction_rows else None,
        layout_version=("consolidated_v2_gbp_1" if excluded_transaction_rows else None),
        warning_codes=(
            ("non_gbp_transaction_sections_excluded",)
            if excluded_transaction_rows
            else ()
        ),
        excluded_transaction_rows=excluded_transaction_rows,
        page_count=1 if source_type is WorkspaceSourceType.DIGITAL_PDF else None,
        mapping_structure_digest=(
            "b" * 64
            if mapping and source_type is WorkspaceSourceType.DIGITAL_PDF
            else None
        ),
        mapping_columns=("column_1", "column_2", "column_3", "column_4")
        if mapping
        else (),
        mapping_sample_rows=(("01/08/2026", "SYNTHETIC", "-10.00", "90.00"),)
        if mapping
        else (),
        suggested_period=DateRange(
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31)
        ),
    )


def _row(
    *,
    state: WorkspaceRowReviewState = WorkspaceRowReviewState.READY,
    balance: bool = True,
) -> WorkspaceTransactionRow:
    return WorkspaceTransactionRow(
        row_id="row-1",
        source_id="source-1",
        source_type=WorkspaceSourceType.CSV,
        source_record_number=2,
        transaction_date=date(2026, 8, 1),
        description="SYNTHETIC SHOP",
        amount=Decimal("-10.00"),
        balance_after=Decimal("90.00") if balance else None,
        category_id="other",
        financial_role=FinancialRole.EXPENSE,
        review_state=state,
    )


def _workspace(
    *,
    status: WorkspaceStatus = WorkspaceStatus.DRAFT,
    retention: WorkspaceRetentionMode = WorkspaceRetentionMode.SAVED,
    rows: tuple[WorkspaceTransactionRow, ...] | None = None,
    sources: tuple[WorkspaceSourceFile, ...] | None = None,
) -> StatementWorkspace:
    finalized = status is WorkspaceStatus.FINALIZED
    default_row = _row(state=WorkspaceRowReviewState.CONFIRMED)
    finalized_row = default_row.model_copy(
        update={
            "source_id": None,
            "source_type": None,
            "source_record_number": None,
            "page_number": None,
        }
    )
    return StatementWorkspace(
        workspace_id="workspace-1",
        retention_mode=retention,
        status=status,
        account_name="Fictional account",
        revision=3,
        sources=() if finalized else ((_source(),) if sources is None else sources),
        rows=(
            (finalized_row,)
            if rows is None and finalized
            else ((_row(),) if rows is None else rows)
        ),
        coverage=(
            WorkspaceCoverageConfirmation(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
                status=CoverageStatus.COMPLETE,
                confirmed=True,
            )
            if finalized
            else None
        ),
        created_at=NOW,
        updated_at=NOW,
        finalized_at=NOW if finalized else None,
    )


@pytest.fixture
def ui(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    streamlit = MagicMock()
    monkeypatch.setattr(page, "st", streamlit)
    monkeypatch.setattr(page, "loading_state", lambda message: nullcontext())
    monkeypatch.setattr(page, "render_empty_state", MagicMock())
    display_error = MagicMock()
    monkeypatch.setattr(page, "render_error", display_error)
    streamlit.display_error = display_error
    monkeypatch.setattr(page, "render_page_header", MagicMock())
    monkeypatch.setattr(page, "render_privacy_notice", MagicMock())
    return streamlit


def test_session_helpers_keep_only_workspace_metadata() -> None:
    session = FrontendSessionState()
    workspace = _workspace()
    selected = _session_with_workspace(session, workspace)
    assert selected.workspace_id == "workspace-1"
    assert selected.workspace_revision == 3
    assert selected.workspace_status is WorkspaceStatus.DRAFT
    assert _blank_session(selected).workspace_id is None


def test_workspace_start_creates_resumes_and_handles_empty_or_invalid_state(
    ui: MagicMock,
) -> None:
    client = MagicMock()
    client.create_workspace.return_value = _workspace()
    columns = (MagicMock(), MagicMock())
    ui.columns.return_value = columns
    ui.radio.return_value = WorkspaceRetentionMode.SAVED
    ui.text_input.return_value = "Fictional account"
    columns[0].button.return_value = True
    columns[1].button.return_value = False
    created, session = _render_workspace_start(client, FrontendSessionState())
    assert created == _workspace()
    assert session.workspace_id == "workspace-1"

    columns[0].button.return_value = False
    columns[1].button.return_value = True
    client.latest_saved_workspace.return_value = None
    resumed, session = _render_workspace_start(client, FrontendSessionState())
    assert resumed is None
    assert session.retention_mode is WorkspaceRetentionMode.SAVED
    ui.info.assert_called()

    client.latest_saved_workspace.return_value = _workspace()
    resumed, session = _render_workspace_start(client, FrontendSessionState())
    assert resumed is not None
    assert session.workspace_id == "workspace-1"

    client.latest_saved_workspace.side_effect = ApiClientError(
        ApiClientErrorCode.CONNECTION_FAILED,
        "safe failure",
    )
    resumed, _session = _render_workspace_start(client, FrontendSessionState())
    assert resumed is None
    ui.display_error.assert_called()

    client.latest_saved_workspace.side_effect = None
    columns[0].button.return_value = True
    columns[1].button.return_value = False
    client.create_workspace.side_effect = ValueError("invalid")
    created, _session = _render_workspace_start(client, FrontendSessionState())
    assert created is None
    ui.error.assert_called()

    client.create_workspace.side_effect = None
    columns[0].button.return_value = False
    columns[1].button.return_value = False
    created, idle_session = _render_workspace_start(client, FrontendSessionState())
    assert created is None
    assert idle_session.retention_mode is WorkspaceRetentionMode.SAVED


def test_build_source_mapping_supports_csv_and_pdf_amount_layouts(
    ui: MagicMock,
) -> None:
    ui.checkbox.return_value = True
    ui.radio.return_value = "Signed amount"
    ui.selectbox.side_effect = ("column_1", "column_2", "column_3", "column_4")
    csv_mapping = _build_source_mapping(
        _source(state=WorkspaceSourceReviewState.MAPPING_REQUIRED)
    )
    assert csv_mapping is not None
    assert csv_mapping.csv_mapping is not None
    assert csv_mapping.csv_mapping.signed_amount_column == "column_3"

    ui.selectbox.side_effect = (
        "column_1",
        "column_2",
        "column_3",
        "column_4",
        None,
    )
    ui.radio.return_value = "Separate debit and credit"
    pdf_mapping = _build_source_mapping(
        _source(
            state=WorkspaceSourceReviewState.MAPPING_REQUIRED,
            source_type=WorkspaceSourceType.DIGITAL_PDF,
        )
    )
    assert pdf_mapping is not None
    assert pdf_mapping.pdf_mapping is not None
    assert pdf_mapping.pdf_mapping.debit_amount == "column_3"

    ui.checkbox.return_value = False
    ui.selectbox.side_effect = ("column_1", "column_2", "column_3", None)
    ui.radio.return_value = "Signed amount"
    assert (
        _build_source_mapping(
            _source(state=WorkspaceSourceReviewState.MAPPING_REQUIRED)
        )
        is None
    )

    ui.checkbox.return_value = True
    ui.selectbox.side_effect = ("column_1", "column_1", "column_1", None)
    assert (
        _build_source_mapping(
            _source(state=WorkspaceSourceReviewState.MAPPING_REQUIRED)
        )
        is None
    )
    ui.error.assert_called()

    ui.selectbox.side_effect = (None, None, None, None)
    ui.radio.return_value = "Signed amount"
    assert (
        _build_source_mapping(
            _source(state=WorkspaceSourceReviewState.MAPPING_REQUIRED)
        )
        is None
    )
    ui.error.assert_called_with(
        "Choose the date, description, and amount columns first."
    )


def test_source_review_handles_ready_unsupported_and_mapping_paths(
    ui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    workspace = _workspace()
    assert _render_source_review(client, workspace, _source()) == workspace
    warning_source = _source(excluded_transaction_rows=2)
    assert _render_source_review(client, workspace, warning_source) == workspace
    ui.warning.assert_called_with("Review this fictional source.")
    ui.caption.assert_called_with(
        "Detected adapter: revolut_consolidated_csv / consolidated_v2_gbp_1"
    )
    ui.button.return_value = False
    assert (
        _render_source_review(
            client,
            workspace,
            _source(state=WorkspaceSourceReviewState.UNSUPPORTED),
        )
        == workspace
    )
    ui.warning.assert_called()

    unsupported = _source(state=WorkspaceSourceReviewState.UNSUPPORTED)
    ui.button.return_value = True
    removed = workspace.model_copy(update={"sources": (), "revision": 4})
    client.remove_workspace_source.return_value = removed
    assert _render_source_review(client, workspace, unsupported) == removed
    client.remove_workspace_source.assert_called()

    client.remove_workspace_source.side_effect = ApiClientError(
        ApiClientErrorCode.API_REJECTED_REQUEST,
        "safe remove rejection",
    )
    assert _render_source_review(client, workspace, unsupported) == workspace
    ui.display_error.assert_called()
    client.remove_workspace_source.side_effect = None

    source = _source(state=WorkspaceSourceReviewState.MAPPING_REQUIRED)
    mapping = MagicMock()
    mapping_builder = MagicMock(return_value=mapping)
    monkeypatch.setattr(page, "_build_source_mapping", mapping_builder)
    ui.file_uploader.return_value = MagicMock()
    ui.button.return_value = False
    assert _render_source_review(client, workspace, source) == workspace

    ui.button.return_value = True
    mapping_builder.return_value = None
    assert _render_source_review(client, workspace, source) == workspace
    ui.error.assert_called()

    mapping_builder.return_value = mapping
    document = UploadedDocument("fictional.csv", b"x", "text/csv")
    monkeypatch.setattr(page, "uploaded_documents", MagicMock(return_value=(document,)))
    client.review_workspace_files.return_value = WorkspaceImportReview(
        workspace=workspace,
        accepted_files=1,
        mapping_required_files=0,
        unsupported_files=0,
        exact_duplicates_removed=0,
        probable_duplicates=0,
    )
    assert _render_source_review(client, workspace, source) == workspace
    client.review_workspace_files.assert_called()

    client.review_workspace_files.side_effect = ValueError("safe mapping error")
    assert _render_source_review(client, workspace, source) == workspace
    ui.error.assert_called_with("safe mapping error")

    client.review_workspace_files.side_effect = ApiClientError(
        ApiClientErrorCode.API_REJECTED_REQUEST,
        "safe mapping rejection",
    )
    assert _render_source_review(client, workspace, source) == workspace
    ui.display_error.assert_called()


def test_file_upload_renders_review_metrics_and_safe_errors(
    ui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    workspace = _workspace()
    upload = MagicMock()
    ui.file_uploader.return_value = (upload,)
    ui.button.return_value = False
    assert _render_file_upload(client, workspace) == workspace

    ui.button.return_value = True
    document = UploadedDocument("fictional.csv", b"x", "text/csv")
    monkeypatch.setattr(page, "uploaded_documents", MagicMock(return_value=(document,)))
    updated = workspace.model_copy(update={"revision": 4})
    client.review_workspace_files.return_value = WorkspaceImportReview(
        workspace=updated,
        accepted_files=1,
        mapping_required_files=0,
        unsupported_files=0,
        exact_duplicates_removed=1,
        probable_duplicates=1,
    )
    metrics = tuple(MagicMock() for _index in range(4))
    ui.columns.return_value = metrics
    assert _render_file_upload(client, workspace) == updated
    assert all(metric.metric.called for metric in metrics)
    ui.warning.assert_called()

    client.review_workspace_files.return_value = WorkspaceImportReview(
        workspace=updated,
        accepted_files=1,
        mapping_required_files=0,
        unsupported_files=0,
        exact_duplicates_removed=0,
        probable_duplicates=0,
    )
    client.review_workspace_files.side_effect = None
    assert _render_file_upload(client, workspace) == updated

    client.review_workspace_files.side_effect = ApiClientError(
        ApiClientErrorCode.API_REJECTED_REQUEST,
        "safe rejection",
    )
    assert _render_file_upload(client, workspace) == workspace
    ui.display_error.assert_called()

    client.review_workspace_files.side_effect = ValueError("safe upload error")
    assert _render_file_upload(client, workspace) == workspace
    ui.error.assert_called_with("safe upload error")


def test_normal_uploader_accepts_only_csv_and_digital_pdf_without_ocr_claims(
    ui: MagicMock,
) -> None:
    client = MagicMock()
    workspace = _workspace()
    ui.file_uploader.return_value = ()
    ui.button.return_value = False

    assert _render_file_upload(client, workspace) == workspace

    uploader_call = ui.file_uploader.call_args
    assert uploader_call.args == ("CSV exports or digital PDFs",)
    assert uploader_call.kwargs["type"] == ["csv", "pdf"]
    assert uploader_call.kwargs["accept_multiple_files"] is True
    caption = ui.caption.call_args.args[0]
    assert "Scans and photographs are not accepted." in caption
    assert "OCR" not in caption
    assert "OCR" not in uploader_call.args[0]
    client.review_workspace_files.assert_not_called()


def test_editor_handles_empty_review_success_and_errors(
    ui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    empty = _workspace(rows=())
    assert _render_editor(client, empty) == empty

    workspace = _workspace()
    ui.data_editor.return_value = ({"Decision": "Include"},)
    ui.checkbox.return_value = False
    ui.button.return_value = False
    assert _render_editor(client, workspace) == workspace
    request = MagicMock()
    request_builder = MagicMock(return_value=request)
    monkeypatch.setattr(page, "build_edit_request", request_builder)
    ui.button.return_value = True
    updated = workspace.model_copy(update={"revision": 4})
    client.edit_workspace.return_value = updated
    assert _render_editor(client, workspace) == updated
    ui.success.assert_called()
    assert request_builder.call_args.kwargs["include_all_clean"] is False

    ui.checkbox.return_value = True
    client.edit_workspace.return_value = updated
    assert _render_editor(client, workspace) == updated
    assert request_builder.call_args.kwargs["include_all_clean"] is True


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("safe row error"),
        ApiClientError(
            ApiClientErrorCode.API_REJECTED_REQUEST,
            "safe edit rejection",
        ),
    ],
)
def test_editor_handles_safe_save_failures(
    ui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    workspace = _workspace()
    ui.data_editor.return_value = ({"Decision": "Include"},)
    ui.checkbox.return_value = False
    ui.button.return_value = True
    monkeypatch.setattr(page, "build_edit_request", MagicMock(return_value=MagicMock()))
    client = MagicMock()
    client.edit_workspace.side_effect = failure

    assert _render_editor(client, workspace) == workspace
    if isinstance(failure, ApiClientError):
        ui.display_error.assert_called()
    else:
        ui.error.assert_called_with("safe row error")


def test_balance_confirmation_is_exact_decimal_and_optional() -> None:
    assert (
        _balance_confirmation(
            include_balance=False,
            balance_text="not-used",
            as_of_date=date(2026, 8, 31),
        )
        is None
    )
    balance = _balance_confirmation(
        include_balance=True,
        balance_text="90.00",
        as_of_date=date(2026, 8, 31),
    )
    assert balance is not None
    assert balance.balance == Decimal("90.00")
    with pytest.raises(ValueError, match="as a number"):
        _balance_confirmation(
            include_balance=True,
            balance_text="invalid",
            as_of_date=date(2026, 8, 31),
        )
    with pytest.raises(ValueError, match="fractions of a penny"):
        _balance_confirmation(
            include_balance=True,
            balance_text="90.005",
            as_of_date=date(2026, 8, 31),
        )


def test_finalization_handles_completed_empty_unresolved_and_success(
    ui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    final = _workspace(status=WorkspaceStatus.FINALIZED)
    client.download_workspace_csv.return_value = WorkspaceCsvDownload(
        filename="approved.csv",
        content="date,description\n",
        row_count=1,
    )
    assert _render_finalization(client, final) == final
    ui.download_button.assert_called()

    client.download_workspace_csv.side_effect = ApiClientError(
        ApiClientErrorCode.CONNECTION_FAILED,
        "safe failure",
    )
    assert _render_finalization(client, final) == final
    ui.display_error.assert_called()

    assert _render_finalization(client, _workspace(rows=())) == _workspace(rows=())
    unresolved = _workspace()
    assert _render_finalization(client, unresolved) == unresolved
    ui.info.assert_called()

    unresolved_source = _workspace(
        rows=(_row(state=WorkspaceRowReviewState.CONFIRMED),),
        sources=(_source(state=WorkspaceSourceReviewState.MAPPING_REQUIRED),),
    )
    ui.info.reset_mock()
    ui.date_input.reset_mock()
    assert _render_finalization(client, unresolved_source) == unresolved_source
    ui.info.assert_called_once_with(
        "Resolve or remove 1 statement file(s) before finalising."
    )
    ui.date_input.assert_not_called()
    client.finalize_workspace.assert_not_called()

    confirmed = _workspace(rows=(_row(state=WorkspaceRowReviewState.CONFIRMED),))
    ui.date_input.side_effect = (
        date(2026, 8, 1),
        date(2026, 8, 31),
        date(2026, 8, 1),
    )
    ui.selectbox.return_value = CoverageStatus.COMPLETE
    ui.text_area.return_value = ""
    ui.text_input.return_value = "90.00"
    ui.checkbox.side_effect = (True, True, True, True)
    ui.button.return_value = False
    assert _render_finalization(client, confirmed) == confirmed

    ui.date_input.side_effect = (
        date(2026, 8, 1),
        date(2026, 8, 31),
        date(2026, 8, 1),
    )
    ui.checkbox.side_effect = (False, True, True, True)
    ui.button.return_value = True
    assert _render_finalization(client, confirmed) == confirmed
    ui.error.assert_called()

    monkeypatch.setattr(
        page,
        "build_coverage_confirmation",
        MagicMock(
            return_value=WorkspaceCoverageConfirmation(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
                status=CoverageStatus.COMPLETE,
                confirmed=True,
            )
        ),
    )
    ui.date_input.side_effect = (
        date(2026, 8, 1),
        date(2026, 8, 31),
        date(2026, 8, 1),
    )
    ui.checkbox.side_effect = (True, True, True, True)
    finalized_result = WorkspaceFinalizeResult(
        workspace=final,
        included_rows=1,
        rejected_rows=0,
        persisted=True,
    )
    client.finalize_workspace.side_effect = None
    client.finalize_workspace.return_value = finalized_result
    assert _render_finalization(client, confirmed) == final
    ui.success.assert_called()


def test_finalization_supports_optional_manual_balance_and_safe_failures(
    ui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    confirmed = _workspace(
        rows=(
            _row(
                state=WorkspaceRowReviewState.CONFIRMED,
                balance=False,
            ),
        )
    )
    final = _workspace(status=WorkspaceStatus.FINALIZED)
    coverage = WorkspaceCoverageConfirmation(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        status=CoverageStatus.COMPLETE,
        confirmed=True,
    )
    monkeypatch.setattr(
        page,
        "build_coverage_confirmation",
        MagicMock(return_value=coverage),
    )
    client.finalize_workspace.return_value = WorkspaceFinalizeResult(
        workspace=final,
        included_rows=1,
        rejected_rows=0,
        persisted=True,
    )

    ui.date_input.side_effect = (date(2026, 8, 1), date(2026, 8, 31))
    ui.selectbox.return_value = CoverageStatus.COMPLETE
    ui.text_area.return_value = ""
    ui.checkbox.side_effect = (False, True, True, True)
    ui.button.return_value = True
    assert _render_finalization(client, confirmed) == final
    sent = client.finalize_workspace.call_args.args[1]
    assert sent.balance is None

    ui.date_input.side_effect = (
        date(2026, 8, 1),
        date(2026, 8, 31),
        date(2026, 8, 31),
    )
    ui.text_input.return_value = "90.00"
    ui.checkbox.side_effect = (True, True, True, True)
    client.finalize_workspace.side_effect = ValueError("safe finalize error")
    assert _render_finalization(client, confirmed) == confirmed
    ui.error.assert_called_with("safe finalize error")

    ui.date_input.side_effect = (
        date(2026, 8, 1),
        date(2026, 8, 31),
        date(2026, 8, 31),
    )
    ui.checkbox.side_effect = (True, True, True, True)
    client.finalize_workspace.side_effect = ApiClientError(
        ApiClientErrorCode.API_REJECTED_REQUEST,
        "safe finalize rejection",
    )
    assert _render_finalization(client, confirmed) == confirmed
    ui.display_error.assert_called()


def test_finalization_requires_disclosed_currency_exclusion_confirmation(
    ui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    confirmed = _workspace(
        rows=(_row(state=WorkspaceRowReviewState.CONFIRMED),),
        sources=(_source(excluded_transaction_rows=2),),
    )
    final = _workspace(status=WorkspaceStatus.FINALIZED)
    coverage = WorkspaceCoverageConfirmation(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        status=CoverageStatus.COMPLETE,
        confirmed=True,
    )
    monkeypatch.setattr(
        page,
        "build_coverage_confirmation",
        MagicMock(return_value=coverage),
    )
    client.finalize_workspace.return_value = WorkspaceFinalizeResult(
        workspace=final,
        included_rows=1,
        rejected_rows=0,
        persisted=True,
    )
    ui.date_input.side_effect = (
        date(2026, 8, 1),
        date(2026, 8, 31),
        date(2026, 8, 1),
    )
    ui.selectbox.return_value = CoverageStatus.COMPLETE
    ui.text_area.return_value = ""
    ui.text_input.return_value = "90.00"
    ui.checkbox.side_effect = (True, False, True, True, True)
    ui.button.return_value = True

    assert _render_finalization(client, confirmed) == confirmed
    client.finalize_workspace.assert_not_called()

    ui.date_input.side_effect = (
        date(2026, 8, 1),
        date(2026, 8, 31),
        date(2026, 8, 1),
    )
    ui.checkbox.side_effect = (True, True, True, True, True)
    assert _render_finalization(client, confirmed) == final
    request = client.finalize_workspace.call_args.args[1]
    assert request.source_exclusions_confirmed is True


def test_workspace_controls_discard_drafts_keep_saved_results_and_delete(
    ui: MagicMock,
) -> None:
    client = MagicMock()
    session = _session_with_workspace(FrontendSessionState(), _workspace())
    columns = (MagicMock(), MagicMock())
    ui.columns.return_value = columns
    ui.checkbox.return_value = True
    columns[0].button.return_value = True
    assert _render_workspace_controls(client, _workspace(), session) == _blank_session(
        session
    )
    client.delete_workspace.assert_called()

    client.delete_workspace.reset_mock()
    final = _workspace(status=WorkspaceStatus.FINALIZED)
    final_session = _session_with_workspace(session, final)
    assert _render_workspace_controls(client, final, final_session) == _blank_session(
        final_session
    )
    client.delete_workspace.assert_not_called()

    columns[0].button.return_value = False
    columns[1].button.return_value = True
    ui.checkbox.return_value = True
    client.delete_workspace.return_value = WorkspaceDeleteResult(
        workspace_id="workspace-1", deleted=True
    )
    assert _render_workspace_controls(client, final, final_session) == _blank_session(
        final_session
    )

    client.delete_workspace.side_effect = ApiClientError(
        ApiClientErrorCode.API_REJECTED_REQUEST,
        "safe delete error",
    )
    assert _render_workspace_controls(client, final, final_session) == final_session
    ui.display_error.assert_called()

    columns[0].button.return_value = True
    columns[1].button.return_value = False
    client.delete_workspace.side_effect = ApiClientError(
        ApiClientErrorCode.API_REJECTED_REQUEST,
        "safe draft discard error",
    )
    assert _render_workspace_controls(client, _workspace(), session) == session
    ui.display_error.assert_called()

    client.delete_workspace.side_effect = None
    columns[0].button.return_value = False
    columns[1].button.return_value = False
    ui.checkbox.return_value = False
    assert _render_workspace_controls(client, final, final_session) is None


def test_workspace_delete_confirmation_matches_the_workspace_lifecycle(
    ui: MagicMock,
) -> None:
    client = MagicMock()
    columns = (MagicMock(), MagicMock())
    ui.columns.return_value = columns
    columns[0].button.return_value = False
    columns[1].button.return_value = False
    ui.checkbox.return_value = False

    draft = _workspace()
    draft_session = _session_with_workspace(FrontendSessionState(), draft)
    assert _render_workspace_controls(client, draft, draft_session) is None
    assert (
        ui.checkbox.call_args_list[-1].args[0]
        == "I understand this permanently discards this unfinished workspace."
    )

    final = _workspace(
        status=WorkspaceStatus.FINALIZED,
        retention=WorkspaceRetentionMode.TEMPORARY,
    )
    final_session = _session_with_workspace(FrontendSessionState(), final)
    assert _render_workspace_controls(client, final, final_session) is None
    assert (
        ui.checkbox.call_args_list[-1].args[0]
        == "I understand this permanently deletes this workspace's approved table."
    )


def test_delete_all_control_requires_confirmation_and_handles_safe_failures(
    ui: MagicMock,
) -> None:
    client = MagicMock()
    session = _session_with_workspace(FrontendSessionState(), _workspace())

    ui.checkbox.return_value = False
    ui.button.return_value = False
    assert _render_delete_all_control(client, session) is None
    client.delete_all_workspace_data.assert_not_called()

    ui.checkbox.return_value = True
    ui.button.return_value = True
    client.delete_all_workspace_data.return_value = WorkspaceDeleteAllResult(
        active_workspaces_deleted=2,
        saved_workspaces_deleted=1,
        deleted=True,
    )
    assert _render_delete_all_control(client, session) == _blank_session(session)
    client.delete_all_workspace_data.assert_called_once()
    ui.success.assert_called_with(
        "All statement workspace data was deleted (2 active, 1 saved)."
    )

    client.delete_all_workspace_data.side_effect = ApiClientError(
        ApiClientErrorCode.API_REJECTED_REQUEST,
        "safe global delete failure",
    )
    assert _render_delete_all_control(client, session) == session
    ui.display_error.assert_called()


def test_status_and_page_orchestration_are_workspace_first(
    ui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace()
    _render_workspace_status(workspace)
    draft_status = ui.markdown.call_args.args[0]
    assert "workspace-strip" in draft_status
    assert "<strong>Files</strong> 1" in draft_status

    _render_workspace_status(_workspace(status=WorkspaceStatus.FINALIZED))
    finalized_status = ui.markdown.call_args.args[0]
    assert "<strong>Files</strong>" not in finalized_status
    assert "<strong>Rows</strong> 1" in finalized_status

    client = MagicMock()
    start = MagicMock(return_value=(None, FrontendSessionState()))
    erase_all = MagicMock(return_value=None)
    monkeypatch.setattr(page, "_render_workspace_start", start)
    monkeypatch.setattr(page, "_render_delete_all_control", erase_all)
    result = render_workspace_page(client, FrontendSessionState())
    assert result.workspace_id is None
    erase_all.assert_called_once()

    session = FrontendSessionState(workspace_id="workspace-1")
    client.get_workspace.side_effect = ApiClientError(
        ApiClientErrorCode.API_REJECTED_REQUEST,
        "missing",
    )
    result = render_workspace_page(client, session)
    assert result.workspace_id is None

    client.get_workspace.side_effect = None
    client.get_workspace.return_value = workspace
    controls = MagicMock(return_value=None)
    monkeypatch.setattr(page, "_render_workspace_controls", controls)
    file_upload = MagicMock(return_value=workspace)
    monkeypatch.setattr(page, "_render_file_upload", file_upload)
    source_review = MagicMock(return_value=workspace)
    monkeypatch.setattr(page, "_render_source_review", source_review)
    confirmed = workspace.model_copy(
        update={"rows": (_row(state=WorkspaceRowReviewState.CONFIRMED),)}
    )
    editor = MagicMock(return_value=confirmed)
    finalization = MagicMock(return_value=confirmed)
    monkeypatch.setattr(page, "_render_editor", editor)
    monkeypatch.setattr(page, "_render_finalization", finalization)
    result = render_workspace_page(client, session)
    assert result.workspace_revision == confirmed.revision
    source_review.assert_called_once()

    controls.return_value = _blank_session(session)
    result = render_workspace_page(client, session)
    assert result.workspace_id is None

    controls.return_value = None
    start.return_value = (workspace, _session_with_workspace(session, workspace))
    result = render_workspace_page(client, FrontendSessionState())
    assert result.workspace_id == "workspace-1"

    file_calls = file_upload.call_count
    editor_calls = editor.call_count
    final = _workspace(status=WorkspaceStatus.FINALIZED)
    client.get_workspace.return_value = final
    finalization.return_value = final
    result = render_workspace_page(client, session)
    assert result.workspace_status is WorkspaceStatus.FINALIZED
    assert file_upload.call_count == file_calls
    assert editor.call_count == editor_calls

    navigate = MagicMock()
    render_workspace_page(client, session, navigate=navigate)
    assert any(
        call.args and call.args[0] == "View your overview"
        for call in ui.button.call_args_list
    )
