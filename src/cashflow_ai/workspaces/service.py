"""Review, edit and finalize isolated multi-statement workspaces."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from io import StringIO
from pathlib import PurePath
from typing import cast

from sqlalchemy.orm import Session, sessionmaker

from cashflow_ai.imports import (
    CsvImportError,
    PdfImportError,
    PdfImportErrorCode,
    SpatialColumnMapping,
    SpatialPdfError,
    TransactionNormalisationError,
    calculate_file_hash,
    extract_text_pdf,
    map_csv_row,
    normalise_transaction,
    parse_csv_document,
    parse_date_value,
    prepare_statement_review,
    reconstruct_spatial_pdf,
    validate_csv_import_plan,
)
from cashflow_ai.persistence.base import new_id, utc_now
from cashflow_ai.persistence.database import session_scope
from cashflow_ai.persistence.models import (
    SavedWorkspaceBalanceRecord,
    SavedWorkspaceCoverageRecord,
    SavedWorkspaceRecord,
    SavedWorkspaceTransactionRecord,
)
from cashflow_ai.persistence.workspaces import (
    SavedWorkspaceRecords,
    SavedWorkspaceRepository,
)
from cashflow_ai.schemas.csv_imports import CsvColumnMapping, CsvDocument, CsvImportPlan
from cashflow_ai.schemas.pdf_api import DigitalPdfColumnMapping
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    DateRange,
    ImportContext,
    StatementCoverage,
)
from cashflow_ai.schemas.transactions import (
    Currency,
    Direction,
    FinancialRole,
    TransactionDraft,
)
from cashflow_ai.schemas.workspaces import (
    StatementWorkspace,
    WorkspaceBalanceConfirmation,
    WorkspaceCoverageConfirmation,
    WorkspaceCreateRequest,
    WorkspaceCsvDownload,
    WorkspaceDeleteAllResult,
    WorkspaceDeleteResult,
    WorkspaceDuplicateDecision,
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
    WorkspaceTransactionRow,
)
from cashflow_ai.workspaces.store import WorkspaceStore

MAX_WORKSPACE_FILES = 20
MAX_WORKSPACE_UPLOAD_BYTES = 50 * 1024 * 1024
MAPPING_SAMPLE_ROWS = 20
MAPPING_SAMPLE_COLUMNS = 20
MAPPING_SAMPLE_CELL_CHARACTERS = 1_000


class WorkspaceErrorCode(StrEnum):
    """Privacy-safe failures from the workspace boundary."""

    NOT_FOUND = "workspace_not_found"
    NOT_DRAFT = "workspace_not_draft"
    NOT_FINALIZED = "workspace_not_finalized"
    REVISION_CONFLICT = "workspace_revision_conflict"
    INVALID_UPLOAD = "workspace_invalid_upload"
    UPLOAD_TOO_LARGE = "workspace_upload_too_large"
    INVALID_MAPPING = "workspace_invalid_mapping"
    REVIEW_REQUIRED = "workspace_review_required"
    DUPLICATE_DECISION_REQUIRED = "workspace_duplicate_decision_required"
    INVALID_COVERAGE = "workspace_invalid_coverage"


class WorkspaceError(ValueError):
    """Controlled workspace error that never contains source values."""

    def __init__(self, code: WorkspaceErrorCode, message: str) -> None:
        """Create a stable, source-data-free workspace failure."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class WorkspaceUpload:
    """Ephemeral source bytes accepted only for one review request."""

    filename: str
    content: bytes
    mime_type: str


def create_workspace(
    store: WorkspaceStore,
    request: WorkspaceCreateRequest,
    *,
    now: datetime | None = None,
) -> StatementWorkspace:
    """Create a blank workspace without consulting legacy profile data."""
    created_at = now or utc_now()
    return store.add(
        StatementWorkspace(
            workspace_id=new_id(),
            retention_mode=request.retention_mode,
            status=WorkspaceStatus.DRAFT,
            account_name=request.account_name,
            currency=request.currency,
            revision=1,
            created_at=created_at,
            updated_at=created_at,
        )
    )


def _require_memory_workspace(
    store: WorkspaceStore, workspace_id: str
) -> StatementWorkspace:
    workspace = store.get(workspace_id)
    if workspace is None:
        raise WorkspaceError(
            WorkspaceErrorCode.NOT_FOUND,
            "the requested statement workspace does not exist",
        )
    return workspace


def _require_draft(store: WorkspaceStore, workspace_id: str) -> StatementWorkspace:
    return _require_draft_workspace(_require_memory_workspace(store, workspace_id))


def _require_draft_workspace(workspace: StatementWorkspace) -> StatementWorkspace:
    """Validate an already-locked workspace snapshot as an editable draft."""
    if workspace.status is not WorkspaceStatus.DRAFT:
        raise WorkspaceError(
            WorkspaceErrorCode.NOT_DRAFT,
            "a finalized workspace cannot accept new source changes",
        )
    return workspace


def _replace_draft(
    store: WorkspaceStore,
    workspace: StatementWorkspace,
    *,
    expected_revision: int,
) -> StatementWorkspace:
    """Translate compare-and-swap races into controlled workspace errors."""
    try:
        return store.replace(workspace, expected_revision=expected_revision)
    except KeyError as error:
        raise WorkspaceError(
            WorkspaceErrorCode.NOT_FOUND,
            "the requested statement workspace does not exist",
        ) from error
    except RuntimeError as error:
        raise WorkspaceError(
            WorkspaceErrorCode.REVISION_CONFLICT,
            "the workspace changed; refresh it before trying again",
        ) from error


def _safe_display_name(filename: str) -> str:
    value = PurePath(filename.replace("\\", "/")).name.strip()
    if not value or len(value) > 255 or not all(char.isprintable() for char in value):
        return "statement"
    return value


def _single(values: tuple[str, ...]) -> str | None:
    return values[0] if len(values) == 1 else None


def _mapping_sample_cell(value: str) -> str:
    """Bound client-visible mapping evidence without changing source parsing."""
    return value[:MAPPING_SAMPLE_CELL_CHARACTERS]


def _automatic_csv_mapping(document: CsvDocument) -> CsvColumnMapping | None:
    suggestions = document.suggestions
    transaction_date = _single(suggestions.transaction_date)
    description = _single(suggestions.description)
    signed_amount = _single(suggestions.signed_amount)
    debit_amount = _single(suggestions.debit_amount)
    credit_amount = _single(suggestions.credit_amount)
    if transaction_date is None or description is None:
        return None
    if signed_amount is not None:
        debit_amount = credit_amount = None
    elif debit_amount is None or credit_amount is None:
        return None
    return CsvColumnMapping(
        transaction_date_column=transaction_date,
        description_column=description,
        signed_amount_column=signed_amount,
        debit_amount_column=debit_amount,
        credit_amount_column=credit_amount,
        posting_date_column=_single(suggestions.posting_date),
        running_balance_column=_single(suggestions.running_balance),
        currency_column=_single(suggestions.currency),
        external_id_column=_single(suggestions.external_id),
        transaction_type_column=_single(suggestions.transaction_type),
    )


def _csv_plan(
    workspace: StatementWorkspace,
    mapping: CsvColumnMapping,
) -> CsvImportPlan:
    coverage = StatementCoverage(
        statement_start_date=date(1900, 1, 1),
        statement_end_date=date(2100, 12, 31),
        status=CoverageStatus.UNKNOWN,
    )
    return CsvImportPlan(
        account_id=workspace.workspace_id,
        account_currency=workspace.currency,
        statement_context=ImportContext(
            account_id=workspace.workspace_id,
            coverage=coverage,
        ),
        mapping=mapping,
    )


def _row_from_draft(
    *,
    source_id: str,
    source_type: WorkspaceSourceType,
    source_record_number: int | None,
    page_number: int | None,
    draft: TransactionDraft,
    needs_review: bool,
    issue_codes: tuple[str, ...],
) -> WorkspaceTransactionRow:
    return WorkspaceTransactionRow(
        row_id=new_id(),
        source_id=source_id,
        source_type=source_type,
        source_record_number=source_record_number,
        page_number=page_number,
        transaction_date=draft.transaction_date,
        posting_date=draft.posting_date,
        description=draft.description,
        merchant=draft.merchant,
        amount=draft.amount,
        balance_after=draft.balance_after,
        currency=Currency.GBP,
        category_id=draft.category_id or "other",
        financial_role=draft.financial_role or FinancialRole.UNKNOWN,
        external_id=draft.external_id,
        transaction_type=draft.transaction_type,
        review_state=(
            WorkspaceRowReviewState.NEEDS_REVIEW
            if needs_review
            else WorkspaceRowReviewState.READY
        ),
        issue_codes=issue_codes,
    )


def _review_csv(
    workspace: StatementWorkspace,
    upload: WorkspaceUpload,
    mapping: WorkspaceFileMapping | None,
) -> tuple[WorkspaceSourceFile, list[WorkspaceTransactionRow]]:
    document = parse_csv_document(upload.content, upload.filename)
    selected = (
        None if mapping is None else mapping.csv_mapping
    ) or _automatic_csv_mapping(document)
    source_id = new_id()
    display_name = _safe_display_name(upload.filename)
    suggested_period = None
    suggested_date_column = _single(document.suggestions.transaction_date)
    if suggested_date_column is not None:
        date_index = document.columns.index(suggested_date_column)
        suggested_dates: list[date] = []
        for row in document.rows:
            try:
                suggested_dates.append(parse_date_value(row.values[date_index]))
            except TransactionNormalisationError:
                continue
        if suggested_dates:
            suggested_period = DateRange(
                start_date=min(suggested_dates),
                end_date=max(suggested_dates),
            )
    if selected is None:
        if len(document.columns) > MAPPING_SAMPLE_COLUMNS:
            return (
                WorkspaceSourceFile(
                    source_id=source_id,
                    source_type=WorkspaceSourceType.CSV,
                    display_name=display_name,
                    file_hash=document.file_hash,
                    state=WorkspaceSourceReviewState.UNSUPPORTED,
                    reason_code="csv_mapping_too_wide",
                    guidance=(
                        "Use a bank CSV export with 20 or fewer columns before "
                        "mapping it."
                    ),
                    row_count=0,
                    suggested_period=suggested_period,
                ),
                [],
            )
        return (
            WorkspaceSourceFile(
                source_id=source_id,
                source_type=WorkspaceSourceType.CSV,
                display_name=display_name,
                file_hash=document.file_hash,
                state=WorkspaceSourceReviewState.MAPPING_REQUIRED,
                reason_code="csv_columns_ambiguous",
                guidance="Match the CSV columns before reviewing its rows.",
                row_count=0,
                mapping_columns=document.columns,
                mapping_sample_rows=tuple(
                    tuple(_mapping_sample_cell(value) for value in row.values)
                    for row in document.rows[:MAPPING_SAMPLE_ROWS]
                ),
                suggested_period=suggested_period,
            ),
            [],
        )
    plan = _csv_plan(workspace, selected)
    validate_csv_import_plan(document, plan)
    rows: list[WorkspaceTransactionRow] = []
    parsed_dates: list[date] = []
    for row in document.rows:
        try:
            original, identity = map_csv_row(
                document.columns,
                row,
                plan,
                source_document_hash=document.file_hash,
            )
            normalised = normalise_transaction(
                original,
                account_id=workspace.workspace_id,
                account_currency=workspace.currency,
                source_identity=identity,
            )
        except TransactionNormalisationError:
            rows.append(
                WorkspaceTransactionRow(
                    row_id=new_id(),
                    source_id=source_id,
                    source_type=WorkspaceSourceType.CSV,
                    source_record_number=row.source_row_number,
                    review_state=WorkspaceRowReviewState.NEEDS_REVIEW,
                    issue_codes=("invalid_source_row",),
                )
            )
            continue
        parsed_dates.append(cast(date, normalised.draft.transaction_date))
        rows.append(
            _row_from_draft(
                source_id=source_id,
                source_type=WorkspaceSourceType.CSV,
                source_record_number=row.source_row_number,
                page_number=None,
                draft=normalised.draft,
                needs_review=False,
                issue_codes=(),
            )
        )
    if parsed_dates:
        suggested_period = DateRange(
            start_date=min(parsed_dates), end_date=max(parsed_dates)
        )
    return (
        WorkspaceSourceFile(
            source_id=source_id,
            source_type=WorkspaceSourceType.CSV,
            display_name=display_name,
            file_hash=document.file_hash,
            state=WorkspaceSourceReviewState.READY,
            reason_code="review_ready",
            guidance="Review every row, sign and coverage before finalizing.",
            row_count=len(rows),
            suggested_period=suggested_period,
        ),
        rows,
    )


def _pdf_spatial_mapping(
    content: bytes,
    mapping: DigitalPdfColumnMapping,
) -> SpatialColumnMapping:
    if calculate_file_hash(content) != mapping.file_hash:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_MAPPING,
            "the PDF changed after its columns were mapped",
        )
    try:
        reconstruction = reconstruct_spatial_pdf(content)
    except SpatialPdfError as error:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_MAPPING,
            "the PDF no longer provides the mapped table structure",
        ) from error
    if (
        reconstruction.table is None
        or reconstruction.table.structure_digest != mapping.structure_digest
    ):
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_MAPPING,
            "the PDF table changed after its columns were mapped",
        )
    return SpatialColumnMapping(
        transaction_date=mapping.transaction_date,
        description=mapping.description,
        signed_amount=mapping.signed_amount,
        debit_amount=mapping.debit_amount,
        credit_amount=mapping.credit_amount,
        running_balance=mapping.running_balance,
    )


def _pdf_mapping_source(
    upload: WorkspaceUpload,
    *,
    file_hash: str,
    reason_code: str,
) -> WorkspaceSourceFile:
    reconstruction = reconstruct_spatial_pdf(upload.content)
    table = reconstruction.table
    if (
        table is None
        or not table.records
        or len(table.columns) > MAPPING_SAMPLE_COLUMNS
    ):
        return WorkspaceSourceFile(
            source_id=new_id(),
            source_type=WorkspaceSourceType.DIGITAL_PDF,
            display_name=_safe_display_name(upload.filename),
            file_hash=file_hash,
            state=WorkspaceSourceReviewState.UNSUPPORTED,
            reason_code="unsafe_pdf_layout",
            guidance="Use the bank's CSV export for this statement.",
            row_count=0,
        )
    return WorkspaceSourceFile(
        source_id=new_id(),
        source_type=WorkspaceSourceType.DIGITAL_PDF,
        display_name=_safe_display_name(upload.filename),
        file_hash=file_hash,
        state=WorkspaceSourceReviewState.MAPPING_REQUIRED,
        reason_code=reason_code,
        guidance="Match the reconstructed columns, then review this PDF again.",
        row_count=0,
        page_count=table.page_count,
        mapping_structure_digest=table.structure_digest,
        mapping_columns=tuple(column.column_id for column in table.columns),
        mapping_sample_rows=tuple(
            tuple(
                _mapping_sample_cell(record.value(column.column_id))
                for column in table.columns
            )
            for record in table.records[:MAPPING_SAMPLE_ROWS]
        ),
    )


def _review_pdf(
    workspace: StatementWorkspace,
    upload: WorkspaceUpload,
    mapping: WorkspaceFileMapping | None,
) -> tuple[WorkspaceSourceFile, list[WorkspaceTransactionRow]]:
    file_hash = calculate_file_hash(upload.content)
    spatial_mapping = (
        None
        if mapping is None or mapping.pdf_mapping is None
        else _pdf_spatial_mapping(upload.content, mapping.pdf_mapping)
    )
    try:
        preview = extract_text_pdf(
            upload.content,
            upload.filename,
            mime_type=upload.mime_type,
            account_id=workspace.workspace_id,
            account_currency=workspace.currency,
            spatial_mapping=spatial_mapping,
        )
    except PdfImportError as error:
        if error.code is PdfImportErrorCode.NO_TRANSACTIONS:
            try:
                return (
                    _pdf_mapping_source(
                        upload,
                        file_hash=file_hash,
                        reason_code="pdf_columns_ambiguous",
                    ),
                    [],
                )
            except SpatialPdfError:
                pass
        reason = (
            "image_only_or_scanned_pdf"
            if error.code is PdfImportErrorCode.OCR_REQUIRED
            else "unsupported_pdf_layout"
        )
        return (
            WorkspaceSourceFile(
                source_id=new_id(),
                source_type=WorkspaceSourceType.DIGITAL_PDF,
                display_name=_safe_display_name(upload.filename),
                file_hash=file_hash,
                state=WorkspaceSourceReviewState.UNSUPPORTED,
                reason_code=reason,
                guidance="Use a selectable-text PDF or the bank's CSV export.",
                row_count=0,
            ),
            [],
        )
    review = prepare_statement_review(preview)
    source_id = new_id()
    rows = [
        _row_from_draft(
            source_id=source_id,
            source_type=WorkspaceSourceType.DIGITAL_PDF,
            source_record_number=row.source_identity.page_record_number,
            page_number=row.source_identity.page_number,
            draft=row.working_draft,
            needs_review=row.requires_review,
            issue_codes=tuple(
                dict.fromkeys(
                    [
                        *(reason.value for reason in row.review_reasons),
                        *(issue.code for issue in row.issues),
                    ]
                )
            ),
        )
        for row in review.rows
    ]
    suggested_period = (
        None
        if review.statement_coverage is None
        else DateRange(
            start_date=review.statement_coverage.statement_start_date,
            end_date=review.statement_coverage.statement_end_date,
        )
    )
    return (
        WorkspaceSourceFile(
            source_id=source_id,
            source_type=WorkspaceSourceType.DIGITAL_PDF,
            display_name=_safe_display_name(upload.filename),
            file_hash=file_hash,
            state=WorkspaceSourceReviewState.READY,
            reason_code="review_ready",
            guidance="Review every extracted value, sign and coverage.",
            row_count=len(rows),
            page_count=preview.page_count,
            suggested_period=suggested_period,
        ),
        rows,
    )


def _classify_upload(upload: WorkspaceUpload) -> WorkspaceSourceType | None:
    filename = upload.filename.casefold()
    if filename.endswith(".csv"):
        return WorkspaceSourceType.CSV
    if filename.endswith(".pdf"):
        return WorkspaceSourceType.DIGITAL_PDF
    return None


def _deduplicate_rows(
    rows: list[WorkspaceTransactionRow],
    source_hashes: dict[str, str],
) -> tuple[tuple[WorkspaceTransactionRow, ...], int, int]:
    exact_sources: set[tuple[object, ...]] = set()
    similar_transactions: dict[tuple[object, ...], str] = {}
    retained: list[WorkspaceTransactionRow] = []
    exact_count = 0
    probable_count = 0
    for original_row in rows:
        row = original_row
        if row.probable_duplicate_of is not None:
            remaining_issues = tuple(
                issue for issue in row.issue_codes if issue != "probable_duplicate"
            )
            review_state = row.review_state
            if (
                review_state is WorkspaceRowReviewState.NEEDS_REVIEW
                and not remaining_issues
            ):
                review_state = WorkspaceRowReviewState.READY
            row = row.model_copy(
                update={
                    "probable_duplicate_of": None,
                    "issue_codes": remaining_issues,
                    "review_state": review_state,
                }
            )
        source_hash = (
            None if row.source_id is None else source_hashes.get(row.source_id)
        )
        source_key = (
            source_hash,
            row.source_type,
            row.source_record_number,
            row.page_number,
        )
        external_key = (
            None
            if row.external_id is None
            else ("external_id", row.external_id.casefold())
        )
        if (source_hash is not None and source_key in exact_sources) or (
            external_key is not None and external_key in exact_sources
        ):
            exact_count += 1
            continue
        if source_hash is not None:
            exact_sources.add(source_key)
        if external_key is not None:
            exact_sources.add(external_key)
        if (
            row.transaction_date is None
            or row.amount is None
            or row.description is None
        ):
            retained.append(row)
            continue
        normal_description = " ".join(row.description.casefold().split())
        similarity_key = (row.transaction_date, row.amount, normal_description)
        probable_of = similar_transactions.get(similarity_key)
        if probable_of is not None and row.review_state not in {
            WorkspaceRowReviewState.CONFIRMED,
            WorkspaceRowReviewState.REJECTED,
        }:
            probable_count += 1
            row = row.model_copy(
                update={
                    "review_state": WorkspaceRowReviewState.NEEDS_REVIEW,
                    "probable_duplicate_of": probable_of,
                    "issue_codes": tuple(
                        dict.fromkeys((*row.issue_codes, "probable_duplicate"))
                    ),
                }
            )
        if row.review_state is not WorkspaceRowReviewState.REJECTED:
            similar_transactions.setdefault(similarity_key, row.row_id)
        retained.append(row)
    return tuple(retained), exact_count, probable_count


def review_workspace_uploads(
    store: WorkspaceStore,
    workspace_id: str,
    uploads: tuple[WorkspaceUpload, ...],
    *,
    mappings: tuple[WorkspaceFileMapping, ...] = (),
    now: datetime | None = None,
) -> WorkspaceImportReview:
    """Parse a mixed batch in memory and replace the combined draft atomically."""
    workspace = _require_draft(store, workspace_id)
    if not uploads or len(uploads) > MAX_WORKSPACE_FILES:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_UPLOAD,
            f"upload between 1 and {MAX_WORKSPACE_FILES} statement files",
        )
    if sum(len(upload.content) for upload in uploads) > MAX_WORKSPACE_UPLOAD_BYTES:
        raise WorkspaceError(
            WorkspaceErrorCode.UPLOAD_TOO_LARGE,
            "the combined statement upload is too large",
        )
    mapping_by_hash = {mapping.file_hash: mapping for mapping in mappings}
    if len(mapping_by_hash) != len(mappings):
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_MAPPING,
            "each uploaded file may have only one mapping",
        )
    if mappings:
        eligible_mappings = {
            source.file_hash: source.source_type
            for source in workspace.sources
            if source.state is WorkspaceSourceReviewState.MAPPING_REQUIRED
        }
        if any(
            eligible_mappings.get(mapping.file_hash) is not mapping.source_type
            for mapping in mappings
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_MAPPING,
                "mappings may resolve only files awaiting review in this workspace",
            )
    sources: list[WorkspaceSourceFile] = []
    candidate_rows: list[WorkspaceTransactionRow] = []
    supplied_hashes: set[str] = set()
    for upload in uploads:
        source_type = _classify_upload(upload)
        file_hash = hashlib.sha256(upload.content).hexdigest()
        supplied_hashes.add(file_hash)
        mapping = mapping_by_hash.get(file_hash)
        if mapping is not None and mapping.source_type is not source_type:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_MAPPING,
                "a file mapping does not match its uploaded source type",
            )
        if source_type is None:
            sources.append(
                WorkspaceSourceFile(
                    source_id=new_id(),
                    source_type=WorkspaceSourceType.UNSUPPORTED,
                    display_name=_safe_display_name(upload.filename),
                    file_hash=file_hash,
                    state=WorkspaceSourceReviewState.UNSUPPORTED,
                    reason_code="unsupported_file_type",
                    guidance="Upload only a bank CSV export or digital PDF.",
                    row_count=0,
                )
            )
            continue
        try:
            source, rows = (
                _review_csv(workspace, upload, mapping)
                if source_type is WorkspaceSourceType.CSV
                else _review_pdf(workspace, upload, mapping)
            )
        except (CsvImportError, SpatialPdfError):
            source = WorkspaceSourceFile(
                source_id=new_id(),
                source_type=source_type,
                display_name=_safe_display_name(upload.filename),
                file_hash=file_hash,
                state=WorkspaceSourceReviewState.UNSUPPORTED,
                reason_code="unsafe_statement_structure",
                guidance="Use a supported digital statement or bank CSV export.",
                row_count=0,
            )
            rows = []
        sources.append(source)
        candidate_rows.extend(rows)
    if set(mapping_by_hash) - supplied_hashes:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_MAPPING,
            "a mapping references a file that was not uploaded",
        )
    all_sources: list[WorkspaceSourceFile]
    if mappings:
        replaced_source_ids = {
            source.source_id
            for source in workspace.sources
            if source.file_hash in supplied_hashes
        }
        all_sources = [
            source
            for source in workspace.sources
            if source.source_id not in replaced_source_ids
        ] + sources
        candidate_rows = [
            row for row in workspace.rows if row.source_id not in replaced_source_ids
        ] + candidate_rows
    else:
        all_sources = [*workspace.sources, *sources]
        candidate_rows = [*workspace.rows, *candidate_rows]
    unique_sources: dict[tuple[str, WorkspaceSourceType], WorkspaceSourceFile] = {}
    for source in all_sources:
        unique_sources.setdefault((source.file_hash, source.source_type), source)
    sources = list(unique_sources.values())
    if len(sources) > MAX_WORKSPACE_FILES:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_UPLOAD,
            f"a workspace can contain no more than {MAX_WORKSPACE_FILES} files",
        )
    source_hashes = {source.source_id: source.file_hash for source in all_sources}
    deduplicated_rows, exact_count, probable_count = _deduplicate_rows(
        candidate_rows,
        source_hashes,
    )
    changed_at = now or utc_now()
    updated = workspace.model_copy(
        update={
            "revision": workspace.revision + 1,
            "sources": tuple(sources),
            "rows": deduplicated_rows,
            "coverage": None,
            "balance": None,
            "updated_at": changed_at,
        }
    )
    _replace_draft(store, updated, expected_revision=workspace.revision)
    return WorkspaceImportReview(
        workspace=updated,
        accepted_files=sum(
            source.state is WorkspaceSourceReviewState.READY for source in sources
        ),
        mapping_required_files=sum(
            source.state is WorkspaceSourceReviewState.MAPPING_REQUIRED
            for source in sources
        ),
        unsupported_files=sum(
            source.state is WorkspaceSourceReviewState.UNSUPPORTED for source in sources
        ),
        exact_duplicates_removed=exact_count,
        probable_duplicates=probable_count,
    )


def edit_workspace_rows(
    store: WorkspaceStore,
    workspace_id: str,
    request: WorkspaceEditRequest,
    *,
    now: datetime | None = None,
) -> StatementWorkspace:
    """Apply spreadsheet edits without accepting changes to source provenance."""
    workspace = _require_draft(store, workspace_id)
    if workspace.revision != request.expected_workspace_revision:
        raise WorkspaceError(
            WorkspaceErrorCode.REVISION_CONFLICT,
            "the workspace changed; refresh it before applying edits",
        )
    revisions = {revision.row_id: revision for revision in request.rows}
    existing_ids = {row.row_id for row in workspace.rows}
    if set(revisions) - existing_ids:
        raise WorkspaceError(
            WorkspaceErrorCode.NOT_FOUND,
            "an edited workspace row no longer exists",
        )
    updated_rows: list[WorkspaceTransactionRow] = []
    for row in workspace.rows:
        revision = revisions.get(row.row_id)
        if revision is None:
            updated_rows.append(row)
            continue
        if row.revision != revision.expected_revision:
            raise WorkspaceError(
                WorkspaceErrorCode.REVISION_CONFLICT,
                "a workspace row changed; refresh before editing it",
            )
        probable = row.probable_duplicate_of is not None
        if probable and revision.duplicate_decision is None:
            raise WorkspaceError(
                WorkspaceErrorCode.DUPLICATE_DECISION_REQUIRED,
                "probable duplicate rows require an explicit keep or reject decision",
            )
        if not probable and revision.duplicate_decision is not None:
            raise WorkspaceError(
                WorkspaceErrorCode.DUPLICATE_DECISION_REQUIRED,
                "duplicate decisions apply only to flagged probable duplicates",
            )
        rejected = (
            revision.review_state is WorkspaceRowReviewState.REJECTED
            or revision.duplicate_decision is WorkspaceDuplicateDecision.REJECT
        )
        updated_rows.append(
            row.model_copy(
                update={
                    "transaction_date": revision.transaction_date,
                    "description": revision.description,
                    "merchant": (
                        row.merchant
                        if revision.description == row.description
                        else None
                    ),
                    "amount": revision.amount,
                    "balance_after": revision.balance_after,
                    "category_id": revision.category_id,
                    "financial_role": revision.financial_role,
                    "review_state": (
                        WorkspaceRowReviewState.REJECTED
                        if rejected
                        else WorkspaceRowReviewState.CONFIRMED
                    ),
                    "probable_duplicate_of": None,
                    "issue_codes": (),
                    "revision": row.revision + 1,
                }
            )
        )
    changed_at = now or utc_now()
    updated = workspace.model_copy(
        update={
            "revision": workspace.revision + 1,
            "rows": tuple(updated_rows),
            "updated_at": changed_at,
        }
    )
    return _replace_draft(store, updated, expected_revision=workspace.revision)


def remove_workspace_source(
    store: WorkspaceStore,
    workspace_id: str,
    source_id: str,
    request: WorkspaceSourceRemoveRequest,
    *,
    now: datetime | None = None,
) -> StatementWorkspace:
    """Remove one draft source and its rows without disturbing other files."""
    workspace = _require_draft(store, workspace_id)
    if workspace.revision != request.expected_workspace_revision:
        raise WorkspaceError(
            WorkspaceErrorCode.REVISION_CONFLICT,
            "the workspace changed; refresh it before removing this source",
        )
    remaining_sources = tuple(
        source for source in workspace.sources if source.source_id != source_id
    )
    if len(remaining_sources) == len(workspace.sources):
        raise WorkspaceError(
            WorkspaceErrorCode.NOT_FOUND,
            "the requested workspace source does not exist",
        )
    remaining_rows = [row for row in workspace.rows if row.source_id != source_id]
    source_hashes = {source.source_id: source.file_hash for source in remaining_sources}
    deduplicated_rows, _, _ = _deduplicate_rows(remaining_rows, source_hashes)
    changed_at = now or utc_now()
    updated = workspace.model_copy(
        update={
            "revision": workspace.revision + 1,
            "sources": remaining_sources,
            "rows": deduplicated_rows,
            "coverage": None,
            "balance": None,
            "updated_at": changed_at,
        }
    )
    return _replace_draft(store, updated, expected_revision=workspace.revision)


def _validate_finalize(
    workspace: StatementWorkspace, request: WorkspaceFinalizeRequest
) -> tuple[WorkspaceTransactionRow, ...]:
    if workspace.revision != request.expected_workspace_revision:
        raise WorkspaceError(
            WorkspaceErrorCode.REVISION_CONFLICT,
            "the workspace changed; refresh it before finalizing",
        )
    if not workspace.sources or any(
        source.state is not WorkspaceSourceReviewState.READY
        for source in workspace.sources
    ):
        raise WorkspaceError(
            WorkspaceErrorCode.REVIEW_REQUIRED,
            "every uploaded statement must be ready before finalization",
        )
    included = tuple(
        row
        for row in workspace.rows
        if row.review_state is not WorkspaceRowReviewState.REJECTED
    )
    if not included or any(
        row.review_state is not WorkspaceRowReviewState.CONFIRMED
        or row.probable_duplicate_of is not None
        for row in included
    ):
        raise WorkspaceError(
            WorkspaceErrorCode.REVIEW_REQUIRED,
            "confirm or reject every transaction row before finalization",
        )
    coverage = request.coverage
    for row in included:
        transaction_date = row.transaction_date
        if transaction_date is None or not (
            coverage.start_date <= transaction_date <= coverage.end_date
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_COVERAGE,
                "confirmed transactions must fall within workspace coverage",
            )
        if any(
            gap.start_date <= transaction_date <= gap.end_date
            for gap in coverage.missing_periods
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_COVERAGE,
                "a confirmed transaction cannot fall inside a missing period",
            )
    if request.balance is not None and not (
        coverage.start_date <= request.balance.as_of_date <= coverage.end_date
    ):
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_COVERAGE,
            "the confirmed balance date must fall within workspace coverage",
        )
    if request.balance is None and any(
        row.balance_after is not None for row in included
    ):
        raise WorkspaceError(
            WorkspaceErrorCode.REVIEW_REQUIRED,
            "confirm the latest balance before finalizing rows with balance evidence",
        )
    return included


def _saved_records(
    workspace: StatementWorkspace,
    rows: tuple[WorkspaceTransactionRow, ...],
) -> SavedWorkspaceRecords:
    finalized_at = workspace.finalized_at
    coverage = workspace.coverage
    if finalized_at is None or coverage is None:
        raise RuntimeError("finalized workspace projection is incomplete")
    return SavedWorkspaceRecords(
        workspace=SavedWorkspaceRecord(
            id=workspace.workspace_id,
            account_name=workspace.account_name,
            retention_mode=workspace.retention_mode.value,
            status=workspace.status.value,
            revision=workspace.revision,
            finalized_at=finalized_at,
            created_at=workspace.created_at,
        ),
        transactions=tuple(
            SavedWorkspaceTransactionRecord(
                id=row.row_id,
                workspace_id=workspace.workspace_id,
                position=position,
                account_id=workspace.workspace_id,
                transaction_date=cast(date, row.transaction_date),
                posting_date=row.posting_date,
                description=cast(str, row.description),
                merchant=row.merchant,
                amount=cast(Decimal, row.amount),
                balance_after=row.balance_after,
                currency=row.currency.value,
                external_id=row.external_id,
                transaction_type=row.transaction_type,
                direction=(
                    Direction.INFLOW.value
                    if row.amount is not None and row.amount > 0
                    else Direction.OUTFLOW.value
                ),
                category_id=row.category_id,
                financial_role=row.financial_role.value,
            )
            for position, row in enumerate(rows, start=1)
        ),
        coverage=SavedWorkspaceCoverageRecord(
            workspace_id=workspace.workspace_id,
            statement_start_date=coverage.start_date,
            statement_end_date=coverage.end_date,
            coverage_status=coverage.status.value,
            missing_periods_json=[
                period.model_dump(mode="json") for period in coverage.missing_periods
            ],
        ),
        balance=(
            None
            if workspace.balance is None
            else SavedWorkspaceBalanceRecord(
                workspace_id=workspace.workspace_id,
                balance=workspace.balance.balance,
                currency=workspace.balance.currency.value,
                as_of_date=workspace.balance.as_of_date,
            )
        ),
    )


def finalize_workspace(
    store: WorkspaceStore,
    factory: sessionmaker[Session],
    workspace_id: str,
    request: WorkspaceFinalizeRequest,
    *,
    now: datetime | None = None,
) -> WorkspaceFinalizeResult:
    """Finalize rows without racing SQLite persistence against concurrent edits."""
    with store.locked(workspace_id) as locked_workspace:
        if locked_workspace is None:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "the requested statement workspace does not exist",
            )
        workspace = _require_draft_workspace(locked_workspace)
        included = _validate_finalize(workspace, request)
        canonical_rows = tuple(
            row.model_copy(
                update={
                    "source_id": None,
                    "source_type": None,
                    "source_record_number": None,
                    "page_number": None,
                    "issue_codes": (),
                    "revision": 1,
                }
            )
            for row in included
        )
        finalized_at = now or utc_now()
        finalized = workspace.model_copy(
            update={
                "status": WorkspaceStatus.FINALIZED,
                "revision": workspace.revision + 1,
                "rows": canonical_rows,
                "coverage": request.coverage,
                "balance": request.balance,
                "updated_at": finalized_at,
                "finalized_at": finalized_at,
                # Source names, hashes, mapping samples and text are draft-only.
                "sources": (),
            }
        )
        persisted = finalized.retention_mode is WorkspaceRetentionMode.SAVED
        if persisted:
            with session_scope(factory) as session:
                SavedWorkspaceRepository(session).save(
                    _saved_records(finalized, canonical_rows)
                )
        store.replace(finalized, expected_revision=workspace.revision)
    return WorkspaceFinalizeResult(
        workspace=finalized,
        included_rows=len(canonical_rows),
        rejected_rows=len(workspace.rows) - len(included),
        persisted=persisted,
    )


def _workspace_from_records(records: SavedWorkspaceRecords) -> StatementWorkspace:
    workspace = records.workspace
    coverage_record = records.coverage
    if coverage_record is None:
        raise RuntimeError("saved workspace is missing confirmed coverage")
    coverage = WorkspaceCoverageConfirmation(
        start_date=coverage_record.statement_start_date,
        end_date=coverage_record.statement_end_date,
        status=CoverageStatus(coverage_record.coverage_status),
        missing_periods=tuple(
            DateRange.model_validate(item)
            for item in coverage_record.missing_periods_json
        ),
        confirmed=True,
    )
    balance_record = records.balance
    balance = (
        None
        if balance_record is None
        else WorkspaceBalanceConfirmation(
            balance=balance_record.balance,
            as_of_date=balance_record.as_of_date,
            currency=Currency.GBP,
            confirmed=True,
        )
    )
    rows = tuple(
        WorkspaceTransactionRow(
            row_id=record.id,
            transaction_date=record.transaction_date,
            posting_date=record.posting_date,
            description=record.description,
            merchant=record.merchant,
            amount=record.amount,
            balance_after=record.balance_after,
            currency=Currency.GBP,
            category_id=record.category_id or "other",
            financial_role=FinancialRole(record.financial_role),
            external_id=record.external_id,
            transaction_type=record.transaction_type,
            review_state=WorkspaceRowReviewState.CONFIRMED,
        )
        for record in records.transactions
    )
    return StatementWorkspace(
        workspace_id=workspace.id,
        retention_mode=WorkspaceRetentionMode(workspace.retention_mode),
        status=WorkspaceStatus(workspace.status),
        account_name=workspace.account_name,
        currency=Currency.GBP,
        revision=workspace.revision,
        rows=rows,
        coverage=coverage,
        balance=balance,
        created_at=workspace.created_at,
        updated_at=workspace.finalized_at,
        finalized_at=workspace.finalized_at,
    )


def get_workspace(
    store: WorkspaceStore,
    factory: sessionmaker[Session],
    workspace_id: str,
) -> StatementWorkspace:
    """Read active state or explicitly load one saved canonical workspace."""
    with store.locked(workspace_id) as active:
        if active is not None:
            return active
        with session_scope(factory) as session:
            records = SavedWorkspaceRepository(session).get(workspace_id)
        if records is None:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "the requested statement workspace does not exist",
            )
        return store.add(_workspace_from_records(records))


def get_latest_saved_workspace(
    store: WorkspaceStore,
    factory: sessionmaker[Session],
) -> StatementWorkspace:
    """Load saved data only after the user explicitly asks to resume it."""
    with store.exclusive():
        with session_scope(factory) as session:
            records = SavedWorkspaceRepository(session).get_latest()
        if records is None:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "no saved statement workspace is available",
            )
        restored = _workspace_from_records(records)
        existing = store.get(restored.workspace_id)
        return existing if existing is not None else store.add(restored)


def delete_workspace(
    store: WorkspaceStore,
    factory: sessionmaker[Session],
    workspace_id: str,
) -> WorkspaceDeleteResult:
    """Delete active state and any explicitly saved canonical projection."""
    with store.locked(workspace_id):
        with session_scope(factory) as session:
            saved_deleted = SavedWorkspaceRepository(session).delete(workspace_id)
        memory_deleted = store.delete(workspace_id)
        if not memory_deleted and not saved_deleted:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "the requested statement workspace does not exist",
            )
    return WorkspaceDeleteResult(workspace_id=workspace_id, deleted=True)


def delete_all_workspace_data(
    store: WorkspaceStore,
    factory: sessionmaker[Session],
) -> WorkspaceDeleteAllResult:
    """Atomically erase all active and saved statement-workspace data."""
    with store.exclusive():
        with session_scope(factory) as session:
            saved_count = SavedWorkspaceRepository(session).delete_all()
        active_count = store.count()
        store.clear()
    return WorkspaceDeleteAllResult(
        active_workspaces_deleted=active_count,
        saved_workspaces_deleted=saved_count,
        deleted=True,
    )


def _spreadsheet_safe_text(value: str | None) -> str:
    """Neutralise formula-leading user text in downloadable spreadsheet data."""
    if value is None:
        return ""
    stripped = value.lstrip()
    if stripped and stripped[0] in "=+-@":
        return f"'{value}"
    return value


def workspace_csv_download(
    store: WorkspaceStore,
    factory: sessionmaker[Session],
    workspace_id: str,
) -> WorkspaceCsvDownload:
    """Render finalized canonical rows entirely in memory."""
    workspace = get_workspace(store, factory, workspace_id)
    if workspace.status is not WorkspaceStatus.FINALIZED:
        raise WorkspaceError(
            WorkspaceErrorCode.NOT_FINALIZED,
            "finalize the workspace before downloading its canonical CSV",
        )
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "date",
            "posting_date",
            "description",
            "merchant",
            "amount_gbp",
            "balance_after_gbp",
            "category",
            "financial_role",
            "external_id",
            "transaction_type",
        ]
    )
    for row in workspace.rows:
        writer.writerow(
            [
                row.transaction_date.isoformat() if row.transaction_date else "",
                row.posting_date.isoformat() if row.posting_date else "",
                _spreadsheet_safe_text(row.description),
                _spreadsheet_safe_text(row.merchant),
                format(row.amount, ".2f") if row.amount is not None else "",
                (
                    format(row.balance_after, ".2f")
                    if row.balance_after is not None
                    else ""
                ),
                _spreadsheet_safe_text(row.category_id),
                row.financial_role.value,
                _spreadsheet_safe_text(row.external_id),
                _spreadsheet_safe_text(row.transaction_type),
            ]
        )
    return WorkspaceCsvDownload(
        filename="cashflow-approved-transactions.csv",
        content=output.getvalue(),
        row_count=len(workspace.rows),
    )


__all__ = [
    "MAX_WORKSPACE_FILES",
    "MAX_WORKSPACE_UPLOAD_BYTES",
    "WorkspaceError",
    "WorkspaceErrorCode",
    "WorkspaceUpload",
    "create_workspace",
    "delete_all_workspace_data",
    "delete_workspace",
    "edit_workspace_rows",
    "finalize_workspace",
    "get_latest_saved_workspace",
    "get_workspace",
    "remove_workspace_source",
    "review_workspace_uploads",
    "workspace_csv_download",
]
