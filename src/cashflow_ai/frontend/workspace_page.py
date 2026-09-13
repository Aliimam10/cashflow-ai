"""Workspace-first Streamlit statement upload and spreadsheet review page."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Protocol, cast

import streamlit as st

from cashflow_ai.frontend.client import ApiClientError, UploadedDocument
from cashflow_ai.frontend.components import (
    loading_state,
    render_empty_state,
    render_error,
    render_page_header,
    render_privacy_notice,
)
from cashflow_ai.frontend.session import FrontendSessionState
from cashflow_ai.frontend.workspace_workflow import (
    CATEGORY_OPTIONS,
    EditorDecision,
    UploadedFileLike,
    build_coverage_confirmation,
    build_edit_request,
    editor_rows,
    suggested_coverage,
    uploaded_documents,
)
from cashflow_ai.schemas.csv_imports import CsvColumnMapping
from cashflow_ai.schemas.pdf_api import DigitalPdfColumnMapping
from cashflow_ai.schemas.statements import CoverageStatus
from cashflow_ai.schemas.transactions import Currency, FinancialRole
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceBalanceConfirmation,
    WorkspaceCreateRequest,
    WorkspaceCsvDownload,
    WorkspaceDeleteAllRequest,
    WorkspaceDeleteAllResult,
    WorkspaceDeleteRequest,
    WorkspaceDeleteResult,
    WorkspaceEditRequest,
    WorkspaceFileMapping,
    WorkspaceFinalizeRequest,
    WorkspaceFinalizeResult,
    WorkspaceImportReview,
    WorkspaceRetentionMode,
    WorkspaceRowReviewState,
    WorkspaceSourceFile,
    WorkspaceSourceRemoveRequest,
    WorkspaceSourceReviewState,
    WorkspaceSourceType,
    WorkspaceStatus,
)


class WorkspaceApi(Protocol):
    """Narrow local API surface used by the statement workspace page."""

    def create_workspace(self, request: WorkspaceCreateRequest) -> StatementWorkspace:
        """Create an isolated empty workspace."""
        ...

    def latest_saved_workspace(self) -> StatementWorkspace | None:
        """Return the latest saved workspace only after an explicit user action."""
        ...

    def get_workspace(self, workspace_id: str) -> StatementWorkspace:
        """Reload one explicitly selected workspace."""
        ...

    def review_workspace_files(
        self,
        workspace_id: str,
        documents: tuple[UploadedDocument, ...],
        *,
        mappings: tuple[WorkspaceFileMapping, ...] = (),
    ) -> WorkspaceImportReview:
        """Review mixed files without retaining their bytes in the frontend."""
        ...

    def edit_workspace(
        self,
        workspace_id: str,
        request: WorkspaceEditRequest,
    ) -> StatementWorkspace:
        """Apply optimistic edits to server-owned rows."""
        ...

    def remove_workspace_source(
        self,
        workspace_id: str,
        source_id: str,
        request: WorkspaceSourceRemoveRequest,
    ) -> StatementWorkspace:
        """Discard one unusable source without losing the remaining draft."""
        ...

    def finalize_workspace(
        self,
        workspace_id: str,
        request: WorkspaceFinalizeRequest,
    ) -> WorkspaceFinalizeResult:
        """Approve the canonical transaction table."""
        ...

    def delete_workspace(
        self,
        workspace_id: str,
        request: WorkspaceDeleteRequest,
    ) -> WorkspaceDeleteResult:
        """Delete explicitly confirmed local workspace data."""
        ...

    def delete_all_workspace_data(
        self,
        request: WorkspaceDeleteAllRequest,
    ) -> WorkspaceDeleteAllResult:
        """Delete every active and saved statement workspace."""
        ...

    def download_workspace_csv(self, workspace_id: str) -> WorkspaceCsvDownload:
        """Build an in-memory canonical CSV without writing a local file."""
        ...


def _session_with_workspace(
    session: FrontendSessionState,
    workspace: StatementWorkspace,
) -> FrontendSessionState:
    return session.model_copy(
        update={
            "workspace_id": workspace.workspace_id,
            "workspace_revision": workspace.revision,
            "workspace_status": workspace.status,
            "retention_mode": workspace.retention_mode,
        }
    )


def _blank_session(session: FrontendSessionState) -> FrontendSessionState:
    return session.model_copy(
        update={
            "workspace_id": None,
            "workspace_revision": None,
            "workspace_status": None,
        }
    )


def _render_delete_all_control(
    client: WorkspaceApi,
    session: FrontendSessionState,
) -> FrontendSessionState | None:
    """Offer an explicit global erasure path even when no workspace is selected."""
    with st.expander("Privacy and deletion"):
        st.caption(
            "Delete every active and saved statement workspace. Developer demo "
            "databases, downloaded exports, and backups are separate and are not "
            "removed."
        )
        confirmed = st.checkbox(
            "I understand this permanently deletes all statement workspace data.",
            key="workspace_confirm_delete_all",
        )
        if not st.button(
            "Delete all local workspace data",
            disabled=not confirmed,
            key="workspace_delete_all",
        ):
            return None
        try:
            result = client.delete_all_workspace_data(
                WorkspaceDeleteAllRequest(confirmed=True)
            )
        except ApiClientError as error:
            render_error(error)
            return session
        st.success(
            "All statement workspace data was deleted "
            f"({result.active_workspaces_deleted} active, "
            f"{result.saved_workspaces_deleted} saved)."
        )
        return _blank_session(session)


def _render_workspace_start(
    client: WorkspaceApi,
    session: FrontendSessionState,
) -> tuple[StatementWorkspace | None, FrontendSessionState]:
    """Render an intentionally blank start rather than loading legacy data."""
    render_empty_state(
        "No statements loaded",
        "Create a workspace, then add one or more CSV exports or digital PDFs.",
    )
    retention = st.radio(
        "When should the approved table be kept?",
        tuple(WorkspaceRetentionMode),
        index=tuple(WorkspaceRetentionMode).index(session.retention_mode),
        format_func=lambda value: (
            "Saved on this device"
            if value is WorkspaceRetentionMode.SAVED
            else "Temporary — cleared by Start over or when the local API stops"
        ),
        horizontal=True,
    )
    account_name = st.text_input("Account label", value="My account", max_chars=100)
    actions = st.columns(2)
    create = actions[0].button(
        "Create workspace",
        type="primary",
        use_container_width=True,
    )
    resume = actions[1].button(
        "Resume saved workspace",
        use_container_width=True,
    )
    try:
        if create:
            with loading_state("Creating a private statement workspace…"):
                workspace = client.create_workspace(
                    WorkspaceCreateRequest(
                        retention_mode=retention,
                        account_name=account_name,
                        currency=Currency.GBP,
                    )
                )
            st.success("Workspace ready. Your previous demo database was not loaded.")
            return workspace, _session_with_workspace(session, workspace)
        if resume:
            with loading_state("Looking for an approved saved workspace…"):
                saved_workspace = client.latest_saved_workspace()
            if saved_workspace is None:
                st.info("There is no saved statement workspace to resume.")
                return None, session.model_copy(update={"retention_mode": retention})
            return saved_workspace, _session_with_workspace(session, saved_workspace)
    except (ApiClientError, ValueError) as error:
        if isinstance(error, ApiClientError):
            render_error(error)
        else:
            st.error("Enter a short account label before creating the workspace.")
    return None, session.model_copy(update={"retention_mode": retention})


def _mapping_choice(
    label: str,
    source: WorkspaceSourceFile,
    *,
    optional: bool,
    key: str,
) -> str | None:
    options: tuple[str | None, ...] = (None, *source.mapping_columns)
    empty_label = "Not provided" if optional else "Choose a column"
    return st.selectbox(
        label,
        options,
        format_func=lambda value: empty_label if value is None else value,
        key=key,
    )


def _build_source_mapping(source: WorkspaceSourceFile) -> WorkspaceFileMapping | None:
    """Collect explicit per-file column choices from bounded mapping evidence."""
    key = f"workspace_mapping_{source.source_id}"
    st.dataframe(
        [
            dict(zip(source.mapping_columns, values, strict=True))
            for values in source.mapping_sample_rows
        ],
        hide_index=True,
        use_container_width=True,
    )
    transaction_date = _mapping_choice(
        "Transaction date", source, optional=False, key=f"{key}_date"
    )
    description = _mapping_choice(
        "Description", source, optional=False, key=f"{key}_description"
    )
    layout = st.radio(
        "Amount layout",
        ("Signed amount", "Separate debit and credit"),
        horizontal=True,
        key=f"{key}_layout",
    )
    signed_amount: str | None = None
    debit_amount: str | None = None
    credit_amount: str | None = None
    if layout == "Signed amount":
        signed_amount = _mapping_choice(
            "Signed amount", source, optional=False, key=f"{key}_amount"
        )
    else:
        debit_amount = _mapping_choice(
            "Money out / debit", source, optional=False, key=f"{key}_debit"
        )
        credit_amount = _mapping_choice(
            "Money in / credit", source, optional=False, key=f"{key}_credit"
        )
    balance = _mapping_choice(
        "Running balance", source, optional=True, key=f"{key}_balance"
    )
    if not st.checkbox(
        "I checked these columns against this exact statement.",
        key=f"{key}_confirmed",
    ):
        return None
    amount_missing = (
        signed_amount is None
        if layout == "Signed amount"
        else debit_amount is None or credit_amount is None
    )
    if transaction_date is None or description is None or amount_missing:
        st.error("Choose the date, description, and amount columns first.")
        return None
    try:
        if source.source_type is WorkspaceSourceType.CSV:
            return WorkspaceFileMapping(
                file_hash=source.file_hash,
                source_type=source.source_type,
                csv_mapping=CsvColumnMapping(
                    transaction_date_column=transaction_date,
                    description_column=description,
                    signed_amount_column=signed_amount,
                    debit_amount_column=debit_amount,
                    credit_amount_column=credit_amount,
                    running_balance_column=balance,
                ),
            )
        return WorkspaceFileMapping(
            file_hash=source.file_hash,
            source_type=source.source_type,
            pdf_mapping=DigitalPdfColumnMapping(
                file_hash=source.file_hash,
                structure_digest=cast(str, source.mapping_structure_digest),
                transaction_date=transaction_date,
                description=description,
                signed_amount=signed_amount,
                debit_amount=debit_amount,
                credit_amount=credit_amount,
                running_balance=balance,
            ),
        )
    except ValueError:
        st.error("Give every selected column one clear and non-overlapping meaning.")
        return None


def _render_source_review(
    client: WorkspaceApi,
    workspace: StatementWorkspace,
    source: WorkspaceSourceFile,
) -> StatementWorkspace:
    """Show one safe source summary and resolve manual mapping when requested."""
    st.markdown(
        (
            '<div class="cf-file-review">'
            f"<strong>{escape(source.display_name)}</strong><br>"
            f"<span>{escape(source.source_type.value.replace('_', ' ').title())} · "
            f"{source.row_count} rows · {escape(source.state.value.replace('_', ' '))}"
            "</span></div>"
        ),
        unsafe_allow_html=True,
    )
    if source.state is WorkspaceSourceReviewState.READY:
        return workspace
    if source.state is WorkspaceSourceReviewState.UNSUPPORTED:
        st.warning(source.guidance)
        if st.button(
            "Remove unsupported file",
            key=f"workspace_remove_source_{source.source_id}",
        ):
            try:
                updated = client.remove_workspace_source(
                    workspace.workspace_id,
                    source.source_id,
                    WorkspaceSourceRemoveRequest(
                        expected_workspace_revision=workspace.revision
                    ),
                )
            except ApiClientError as error:
                render_error(error)
                return workspace
            st.success("Unsupported file removed; the other statements are unchanged.")
            return updated
        return workspace

    st.warning(source.guidance)
    mapping = _build_source_mapping(source)
    replacement = cast(
        UploadedFileLike | None,
        st.file_uploader(
            "Re-select this exact file to apply its mapping",
            type=["csv"] if source.source_type is WorkspaceSourceType.CSV else ["pdf"],
            accept_multiple_files=False,
            key=f"workspace_mapping_file_{source.source_id}",
        ),
    )
    if not st.button("Apply mapping", key=f"workspace_apply_{source.source_id}"):
        return workspace
    if mapping is None or replacement is None:
        st.error("Confirm the mapping and re-select the exact source file first.")
        return workspace
    try:
        documents = uploaded_documents((replacement,))
        with loading_state("Rechecking the exact file with your mapping…"):
            result = client.review_workspace_files(
                workspace.workspace_id,
                documents,
                mappings=(mapping,),
            )
    except (ApiClientError, ValueError) as error:
        if isinstance(error, ApiClientError):
            render_error(error)
        else:
            st.error(str(error))
        return workspace
    return result.workspace


def _render_file_upload(
    client: WorkspaceApi,
    workspace: StatementWorkspace,
) -> StatementWorkspace:
    st.subheader("Upload bank statements")
    st.caption(
        "Select several files together. CSV and selectable-text digital PDFs may be "
        "mixed. All files must belong to the same GBP personal current or savings "
        "account. Scans and photographs are not accepted."
    )
    uploads = cast(
        Sequence[UploadedFileLike],
        st.file_uploader(
            "CSV exports or digital PDFs",
            type=["csv", "pdf"],
            accept_multiple_files=True,
            # A successful review advances the server revision. The next rerun
            # therefore receives a new widget key and Streamlit releases the old
            # UploadedFile objects instead of retaining source bytes in session
            # widget state for the life of the workspace.
            key=f"workspace_upload_{workspace.workspace_id}_{workspace.revision}",
        )
        or (),
    )
    if not st.button(
        "Review selected files",
        type="primary",
        disabled=not uploads,
    ):
        return workspace
    try:
        documents = uploaded_documents(uploads)
        with loading_state("Extracting and combining statement rows locally…"):
            review = client.review_workspace_files(workspace.workspace_id, documents)
    except (ApiClientError, ValueError) as error:
        if isinstance(error, ApiClientError):
            render_error(error)
        else:
            st.error(str(error))
        return workspace
    metrics = st.columns(4)
    metrics[0].metric("Accepted files", review.accepted_files)
    metrics[1].metric("Needs mapping", review.mapping_required_files)
    metrics[2].metric("Unsupported", review.unsupported_files)
    metrics[3].metric("Duplicates removed", review.exact_duplicates_removed)
    if review.probable_duplicates:
        st.warning(
            f"{review.probable_duplicates} possible duplicate row(s) need a decision."
        )
    return review.workspace


def _render_editor(
    client: WorkspaceApi,
    workspace: StatementWorkspace,
) -> StatementWorkspace:
    if not workspace.rows:
        render_empty_state(
            "Your combined table will appear here",
            "Upload statements above. No analytics will run before you approve it.",
        )
        return workspace

    st.subheader("Review the combined transaction table")
    st.caption(
        "Edit cells like a spreadsheet. Mark every usable row Include or Exclude, "
        "then save your review."
    )
    clean_count = sum(
        row.review_state is WorkspaceRowReviewState.READY
        and row.probable_duplicate_of is None
        and not row.issue_codes
        for row in workspace.rows
    )
    include_all_clean = (
        st.checkbox(
            f"Include all {clean_count} clean extracted row(s) when I save",
            disabled=clean_count == 0,
            help=(
                "This applies only to rows with no extraction warning or duplicate "
                "flag. Uncertain rows still need individual decisions."
            ),
        )
        is True
    )
    category_options = tuple(
        dict.fromkeys((*CATEGORY_OPTIONS, *(row.category_id for row in workspace.rows)))
    )
    edited = st.data_editor(
        editor_rows(workspace),
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        disabled=("Source", "Source location", "Review note"),
        column_config={
            "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
            "Description": st.column_config.TextColumn("Description", max_chars=500),
            "Amount": st.column_config.TextColumn(
                "Amount", help="Use a signed decimal, for example -12.50 or 900.00."
            ),
            "Balance": st.column_config.TextColumn(
                "Balance", help="Optional running balance as an exact decimal."
            ),
            "Category": st.column_config.SelectboxColumn(
                "Category", options=category_options, required=True
            ),
            "Financial role": st.column_config.SelectboxColumn(
                "Financial role",
                options=tuple(role.value for role in FinancialRole),
                required=True,
            ),
            "Decision": st.column_config.SelectboxColumn(
                "Decision",
                options=tuple(EditorDecision),
                required=True,
            ),
        },
        key=f"workspace_editor_{workspace.workspace_id}_{workspace.revision}",
    )
    if not st.button("Save row review", type="primary"):
        return workspace
    try:
        request = build_edit_request(
            workspace,
            cast(Sequence[dict[str, object]], edited),
            include_all_clean=include_all_clean,
        )
        with loading_state("Saving your reviewed transaction table…"):
            updated = client.edit_workspace(workspace.workspace_id, request)
    except (ApiClientError, ValueError) as error:
        if isinstance(error, ApiClientError):
            render_error(error)
        else:
            st.error(str(error))
        return workspace
    st.success("Row decisions saved. Source provenance remains attached on the server.")
    return updated


def _balance_confirmation(
    *,
    include_balance: bool,
    balance_text: str,
    as_of_date: date,
) -> WorkspaceBalanceConfirmation | None:
    if not include_balance:
        return None
    try:
        parsed = Decimal(balance_text)
        balance = parsed.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("enter the confirmed latest balance as a number") from error
    if parsed != balance:
        raise ValueError("the confirmed balance cannot contain fractions of a penny")
    return WorkspaceBalanceConfirmation(
        balance=balance,
        as_of_date=as_of_date,
        currency=Currency.GBP,
        confirmed=True,
    )


def _render_finalization(
    client: WorkspaceApi,
    workspace: StatementWorkspace,
) -> StatementWorkspace:
    if workspace.status is WorkspaceStatus.FINALIZED:
        st.success(
            "This canonical table is finalised. Later pages may now use only these "
            "approved rows."
        )
        try:
            export = client.download_workspace_csv(workspace.workspace_id)
        except ApiClientError as error:
            render_error(error)
            return workspace
        st.download_button(
            "Download final CSV",
            data=export.content.encode("utf-8"),
            file_name=export.filename,
            mime="text/csv",
        )
        return workspace
    unresolved_sources = sum(
        source.state is not WorkspaceSourceReviewState.READY
        for source in workspace.sources
    )
    if unresolved_sources:
        st.info(
            f"Resolve or remove {unresolved_sources} statement file(s) before "
            "finalising."
        )
        return workspace
    if not workspace.rows:
        return workspace
    if workspace.unresolved_row_count:
        st.info(
            f"Review {workspace.unresolved_row_count} remaining row decision(s) before "
            "finalising."
        )
        return workspace

    st.subheader("Final checks")
    start_default, end_default = suggested_coverage(workspace)
    start = st.date_input("Statement coverage starts", value=start_default)
    end = st.date_input("Statement coverage ends", value=end_default)
    coverage_status = st.selectbox(
        "Coverage quality",
        tuple(CoverageStatus),
        index=tuple(CoverageStatus).index(CoverageStatus.UNKNOWN),
        format_func=lambda value: value.value.replace("_", " ").title(),
    )
    missing_periods = st.text_area(
        "Missing periods (one YYYY-MM-DD,YYYY-MM-DD range per line)",
        help="Missing dates remain unknown; they are never counted as zero spending.",
    )
    balance_rows = tuple(
        row
        for row in workspace.rows
        if row.transaction_date is not None and row.balance_after is not None
    )
    suggested_balance_row = (
        None
        if not balance_rows
        else max(balance_rows, key=lambda row: row.transaction_date or date.min)
    )
    balance_evidence_present = suggested_balance_row is not None
    if balance_evidence_present:
        st.info(
            "This table contains running balances. Check the latest balance against "
            "the statement before finalising."
        )
        include_balance = True
    else:
        include_balance = st.checkbox("I can confirm a latest account balance")
    balance_text = ""
    balance_date = end
    balance_confirmed = not balance_evidence_present
    if include_balance:
        balance_text = st.text_input(
            "Latest balance",
            value=(
                ""
                if suggested_balance_row is None
                or suggested_balance_row.balance_after is None
                else str(suggested_balance_row.balance_after)
            ),
        )
        balance_date = st.date_input(
            "Balance date",
            value=(
                end
                if suggested_balance_row is None
                or suggested_balance_row.transaction_date is None
                else suggested_balance_row.transaction_date
            ),
        )
        if balance_evidence_present:
            balance_confirmed = st.checkbox(
                "I checked this latest balance against the statement."
            )
    statement_confirmed = st.checkbox(
        "I checked the combined rows against every source statement."
    )
    dates_confirmed = st.checkbox(
        "I confirm the displayed dates use the correct day/month interpretation."
    )
    signs_confirmed = st.checkbox(
        "I confirm money in is positive and money out is negative."
    )
    if not st.button("Finalise statement table", type="primary"):
        return workspace
    if (
        not statement_confirmed
        or not dates_confirmed
        or not signs_confirmed
        or not balance_confirmed
    ):
        st.error(
            "Confirm the source rows, date interpretation, amount signs, and any "
            "running-balance evidence before finalising."
        )
        return workspace
    try:
        request = WorkspaceFinalizeRequest(
            expected_workspace_revision=workspace.revision,
            statement_confirmed=True,
            date_interpretation_confirmed=True,
            sign_convention_confirmed=True,
            coverage=build_coverage_confirmation(
                start_date=start,
                end_date=end,
                status=coverage_status,
                missing_periods_text=missing_periods,
            ),
            balance=_balance_confirmation(
                include_balance=include_balance,
                balance_text=balance_text,
                as_of_date=balance_date,
            ),
        )
        with loading_state("Finalising the approved canonical table…"):
            result = client.finalize_workspace(workspace.workspace_id, request)
    except (ApiClientError, ValueError) as error:
        if isinstance(error, ApiClientError):
            render_error(error)
        else:
            st.error(str(error))
        return workspace
    st.success(
        f"Finalised {result.included_rows} row(s); "
        f"{result.rejected_rows} row(s) were excluded."
    )
    return result.workspace


def _render_workspace_controls(
    client: WorkspaceApi,
    workspace: StatementWorkspace,
    session: FrontendSessionState,
) -> FrontendSessionState | None:
    should_delete_on_start = (
        workspace.status is WorkspaceStatus.DRAFT
        or workspace.retention_mode is WorkspaceRetentionMode.TEMPORARY
    )
    discard_confirmed = not should_delete_on_start or (
        st.checkbox(
            "I understand Start blank discards this unfinished or temporary workspace.",
            key=f"workspace_confirm_start_{workspace.workspace_id}",
        )
        is True
    )
    actions = st.columns(2)
    if actions[0].button(
        "Start a blank workspace",
        disabled=not discard_confirmed,
        use_container_width=True,
    ):
        if should_delete_on_start:
            try:
                client.delete_workspace(
                    workspace.workspace_id,
                    WorkspaceDeleteRequest(confirmed=True),
                )
            except ApiClientError as error:
                render_error(error)
                return session
            st.info("The unfinished or temporary workspace data was discarded.")
        else:
            st.info(
                "The approved saved workspace remains available until you explicitly "
                "delete it."
            )
        return _blank_session(session)
    delete_confirmation = (
        "I understand this permanently discards this unfinished workspace."
        if workspace.status is WorkspaceStatus.DRAFT
        else "I understand this permanently deletes this workspace's approved table."
    )
    confirm_delete = st.checkbox(delete_confirmation)
    if actions[1].button(
        "Delete workspace data",
        disabled=not confirm_delete,
        use_container_width=True,
    ):
        try:
            client.delete_workspace(
                workspace.workspace_id,
                WorkspaceDeleteRequest(confirmed=True),
            )
        except ApiClientError as error:
            render_error(error)
            return session
        st.success("Workspace data deleted from this device.")
        return _blank_session(session)
    return None


def _render_workspace_status(workspace: StatementWorkspace) -> None:
    source_chip = (
        ""
        if workspace.status is WorkspaceStatus.FINALIZED
        else (
            '<span class="cf-workspace-chip"><strong>Files</strong> '
            f"{len(workspace.sources)}</span>"
        )
    )
    st.markdown(
        (
            '<div class="cf-workspace-strip">'
            '<span class="cf-workspace-chip"><strong>Mode</strong> '
            f"{escape(workspace.retention_mode.value)}</span>"
            '<span class="cf-workspace-chip"><strong>Status</strong> '
            f"{escape(workspace.status.value)}</span>"
            f"{source_chip}"
            '<span class="cf-workspace-chip"><strong>Rows</strong> '
            f"{len(workspace.rows)}</span>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def render_workspace_page(
    client: WorkspaceApi,
    session: FrontendSessionState,
) -> FrontendSessionState:
    """Render one workspace without retaining uploads or rows in session metadata."""
    render_page_header(
        "Private statement workspace",
        "Upload bank statements",
        "Combine CSV exports and digital PDFs, correct the extracted table, and "
        "approve exactly what later calculations may use.",
    )
    render_privacy_notice()

    workspace: StatementWorkspace | None = None
    current_session = session
    if session.workspace_id is None:
        workspace, current_session = _render_workspace_start(client, session)
        if workspace is None:
            erased_session = _render_delete_all_control(client, current_session)
            return erased_session or current_session
    else:
        try:
            workspace = client.get_workspace(session.workspace_id)
        except ApiClientError as error:
            render_error(error)
            st.caption("Start a blank workspace or explicitly resume a saved one.")
            return _blank_session(session)

    current_session = _session_with_workspace(current_session, workspace)
    _render_workspace_status(workspace)
    control_result = _render_workspace_controls(
        client,
        workspace,
        current_session,
    )
    if control_result is not None:
        return control_result

    if workspace.status is WorkspaceStatus.DRAFT:
        workspace = _render_file_upload(client, workspace)
        for source in workspace.sources:
            workspace = _render_source_review(client, workspace, source)
        workspace = _render_editor(client, workspace)
    workspace = _render_finalization(client, workspace)
    return _session_with_workspace(current_session, workspace)


__all__ = ["WorkspaceApi", "render_workspace_page"]
