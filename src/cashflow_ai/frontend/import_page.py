"""Thin Streamlit account-onboarding and statement-import page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePath
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
from cashflow_ai.frontend.import_workflow import (
    UploadKind,
    balances_confirmed_from_review,
    build_import_context,
    build_statement_balances,
    build_statement_coverage,
    corrected_row_review,
    csv_preview_rows,
    optional_text,
    pdf_mapping_rows,
    pdf_review_csv_bytes,
    pdf_review_rows,
    suggested_column_index,
    suggested_csv_statement_period,
)
from cashflow_ai.frontend.session import FrontendSessionState
from cashflow_ai.schemas.accounts import AccountType
from cashflow_ai.schemas.api import (
    AccountCreate,
    AccountResponse,
    Page,
    UserProfileCreate,
    UserProfileResponse,
)
from cashflow_ai.schemas.csv_imports import (
    CsvColumnMapping,
    CsvImportConfirmation,
    CsvImportPlan,
    CsvImportSummary,
    CsvPreview,
)
from cashflow_ai.schemas.pdf_api import (
    DigitalPdfColumnMapping,
    DigitalPdfColumnRole,
    DigitalPdfMappingPreview,
    DigitalPdfReviewResult,
    DigitalPdfReviewState,
)
from cashflow_ai.schemas.pdf_persistence import PdfImportSummary
from cashflow_ai.schemas.reconciliation import (
    AmountSignConvention,
    DateFormat,
    ReconciliationStatus,
    RowDecision,
    RowReview,
    StatementApproval,
    StatementReview,
    StatementReviewRow,
)
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    StatementCoverage,
    StatementFlag,
)
from cashflow_ai.schemas.transactions import Currency


class ImportApi(Protocol):
    """Narrow local API surface used by the import page."""

    def current_profile(self) -> UserProfileResponse:
        """Return the single local profile."""
        ...

    def create_profile(self, request: UserProfileCreate) -> UserProfileResponse:
        """Create the local profile."""
        ...

    def list_accounts(self, profile_id: str) -> Page[AccountResponse]:
        """List profile accounts."""
        ...

    def create_account(
        self, profile_id: str, request: AccountCreate
    ) -> AccountResponse:
        """Create one account."""
        ...

    def preview_csv(self, document: UploadedDocument) -> CsvPreview:
        """Preview CSV structure."""
        ...

    def confirm_csv(
        self,
        document: UploadedDocument,
        *,
        plan: CsvImportPlan,
        confirmation: CsvImportConfirmation,
    ) -> CsvImportSummary:
        """Persist one explicitly confirmed CSV."""
        ...

    def prepare_pdf_review(
        self,
        document: UploadedDocument,
        *,
        account_id: str,
        account_currency: Currency,
        mapping: DigitalPdfColumnMapping | None = None,
    ) -> DigitalPdfReviewResult:
        """Prepare one non-persistent digital-PDF review."""
        ...

    def confirm_pdf(
        self,
        document: UploadedDocument,
        *,
        account_id: str,
        account_currency: Currency,
        approval: StatementApproval,
        mapping: DigitalPdfColumnMapping | None = None,
    ) -> PdfImportSummary:
        """Confirm and atomically persist one digital PDF."""
        ...


class UploadedFileLike(Protocol):
    """Streamlit upload surface needed without retaining widget objects."""

    name: str

    def getvalue(self) -> bytes:
        """Return the uploaded bytes held by the current widget run."""
        ...


@dataclass(frozen=True, slots=True, repr=False)
class _PendingRowDecision:
    row: StatementReviewRow
    decision: RowDecision
    transaction_date_text: str
    posting_date_text: str
    description: str
    amount_text: str
    balance_after_text: str


_IMPORT_GUIDANCE = {
    "ocr_required": (
        "Scanned and image-only PDFs are outside Version 1. Download a CSV export "
        "from the bank instead."
    ),
    "preview_changed": "The file changed after preview. Review the current file again.",
    "file_changed": "The PDF changed after review. Review the current file again.",
    "account_currency_mismatch": "Choose an account using the statement currency.",
    "transaction_outside_coverage": (
        "Use the date range detected from the CSV, or correct the statement range "
        "so it includes every transaction outside a declared gap."
    ),
}


def _render_import_error(error: ApiClientError) -> None:
    render_error(error)
    guidance = _IMPORT_GUIDANCE.get(error.problem_code or "")
    if guidance is not None:
        st.caption(guidance)


def _load_or_create_profile(client: ImportApi) -> UserProfileResponse | None:
    try:
        profile = client.current_profile()
    except ApiClientError as error:
        if error.problem_code != "profile_not_found":
            _render_import_error(error)
            return None
    else:
        st.caption(
            f"Local profile: {profile.display_name or 'Unnamed user'} · "
            f"{profile.base_currency.value} · {profile.timezone}"
        )
        return profile

    st.subheader("Set up your profile")
    st.caption(
        "Choose local preferences. You will never be asked for bank login details."
    )
    with st.form("profile_setup"):
        display_name = st.text_input("Display name (optional)", max_chars=100)
        base_currency = st.selectbox(
            "Base currency", tuple(Currency), format_func=lambda x: x.value
        )
        timezone = st.text_input("Timezone", value="UTC", max_chars=100)
        submitted = st.form_submit_button("Create local profile", type="primary")
    if not submitted:
        return None
    try:
        request = UserProfileCreate(
            display_name=optional_text(display_name),
            base_currency=base_currency,
            timezone=timezone,
        )
        profile = client.create_profile(request)
    except (ApiClientError, ValueError) as error:
        if isinstance(error, ApiClientError):
            _render_import_error(error)
        else:
            st.error("Check the profile name, currency, and IANA timezone.")
        return None
    st.success("Local profile created.")
    return profile


def _account_label(account: AccountResponse) -> str:
    return f"{account.name} · {account.account_type.value} · {account.currency.value}"


def _select_or_create_account(
    client: ImportApi,
    profile: UserProfileResponse,
    current_account_id: str | None,
) -> AccountResponse | None:
    st.subheader("Choose an account")
    try:
        accounts = list(client.list_accounts(profile.profile_id).items)
    except ApiClientError as error:
        _render_import_error(error)
        return None

    if not accounts:
        render_empty_state(
            "No accounts yet",
            "Add current/checking or savings metadata before importing a statement.",
        )
    with st.expander(
        "Add another account" if accounts else "Add your first account",
        expanded=not accounts,
    ):
        with st.form("account_setup"):
            name = st.text_input("Account name", max_chars=100)
            account_type = st.selectbox(
                "Account type",
                tuple(AccountType),
                format_func=lambda item: item.value.replace("_", " ").title(),
            )
            currency = st.selectbox(
                "Account currency",
                tuple(Currency),
                format_func=lambda item: item.value,
            )
            institution = st.text_input("Institution label (optional)", max_chars=100)
            submitted = st.form_submit_button("Add account", type="primary")
        if submitted:
            try:
                created = client.create_account(
                    profile.profile_id,
                    AccountCreate(
                        name=name,
                        account_type=account_type,
                        currency=currency,
                        institution_label=optional_text(institution),
                    ),
                )
            except (ApiClientError, ValueError) as error:
                if isinstance(error, ApiClientError):
                    _render_import_error(error)
                else:
                    st.error("Enter a name and supported account details.")
            else:
                accounts.append(created)
                current_account_id = created.account_id
                st.success("Account added without storing bank credentials or numbers.")

    if not accounts:
        return None
    account_ids = tuple(item.account_id for item in accounts)
    index = (
        account_ids.index(current_account_id)
        if current_account_id in account_ids
        else 0
    )
    selected_id = st.selectbox(
        "Destination account",
        account_ids,
        index=index,
        format_func=lambda value: _account_label(
            next(item for item in accounts if item.account_id == value)
        ),
    )
    return next(item for item in accounts if item.account_id == selected_id)


def _column_choice(
    label: str,
    preview: CsvPreview,
    suggestions: tuple[str, ...],
    *,
    optional: bool,
    key: str,
) -> str | None:
    options: tuple[str | None, ...] = (
        (None, *preview.columns) if optional else preview.columns
    )
    return st.selectbox(
        label,
        options,
        index=suggested_column_index(
            preview.columns,
            suggestions,
            optional=optional,
        ),
        format_func=lambda value: "Not provided" if value is None else value,
        key=key,
    )


def _render_coverage(coverage: StatementCoverage) -> None:
    st.caption(
        f"Statement dates: {coverage.statement_start_date.isoformat()} to "
        f"{coverage.statement_end_date.isoformat()} · {coverage.status.value}"
    )
    if coverage.missing_periods:
        st.warning(
            "Missing dates remain unknown and will not be treated as zero spending: "
            + ", ".join(
                f"{item.start_date.isoformat()} to {item.end_date.isoformat()}"
                for item in coverage.missing_periods
            )
        )


def _render_csv_result(result: CsvImportSummary) -> None:
    st.success("CSV confirmation completed and every source row was accounted for.")
    columns = st.columns(4)
    columns[0].metric("New transactions", result.new_transactions)
    columns[1].metric("Exact duplicates", result.exact_duplicates_skipped)
    columns[2].metric("Needs duplicate review", result.probable_duplicates)
    columns[3].metric("Rejected rows", result.rejected_rows)
    if result.repeated_file:
        st.info("This exact file was already imported; no second copy was created.")
    if result.coverage.new_missing_periods or result.coverage.disconnected_range:
        st.warning("The confirmed statement leaves a gap or disconnected date range.")
    if result.coverage.overlap_periods:
        st.info("Part of this statement covers dates you previously imported.")


def _render_csv_workflow(
    client: ImportApi,
    account: AccountResponse,
    document: UploadedDocument,
) -> None:
    try:
        with loading_state("Validating the CSV locally…"):
            preview = client.preview_csv(document)
    except ApiClientError as error:
        _render_import_error(error)
        return

    st.success(
        f"Previewed {preview.total_data_rows} rows using {preview.encoding.value}; "
        "nothing has been imported yet."
    )
    st.dataframe(csv_preview_rows(preview), hide_index=True, use_container_width=True)

    with st.form("csv_review"):
        st.subheader("Check the columns and statement details")
        transaction_date_column = _column_choice(
            "Transaction date",
            preview,
            preview.suggestions.transaction_date,
            optional=False,
            key="csv_transaction_date",
        )
        description_column = _column_choice(
            "Description",
            preview,
            preview.suggestions.description,
            optional=False,
            key="csv_description",
        )
        default_layout = (
            "Separate debit and credit"
            if not preview.suggestions.signed_amount
            and preview.suggestions.debit_amount
            and preview.suggestions.credit_amount
            else "Signed amount"
        )
        amount_layout = st.radio(
            "Amount layout",
            ("Signed amount", "Separate debit and credit"),
            index=0 if default_layout == "Signed amount" else 1,
            horizontal=True,
        )
        signed_amount_column: str | None = None
        debit_amount_column: str | None = None
        credit_amount_column: str | None = None
        if amount_layout == "Signed amount":
            signed_amount_column = _column_choice(
                "Signed amount",
                preview,
                preview.suggestions.signed_amount,
                optional=False,
                key="csv_signed_amount",
            )
        else:
            debit_amount_column = _column_choice(
                "Debit amount",
                preview,
                preview.suggestions.debit_amount,
                optional=False,
                key="csv_debit_amount",
            )
            credit_amount_column = _column_choice(
                "Credit amount",
                preview,
                preview.suggestions.credit_amount,
                optional=False,
                key="csv_credit_amount",
            )
        posting_date_column = _column_choice(
            "Posting date (optional)",
            preview,
            preview.suggestions.posting_date,
            optional=True,
            key="csv_posting_date",
        )
        running_balance_column = _column_choice(
            "Running balance (optional)",
            preview,
            preview.suggestions.running_balance,
            optional=True,
            key="csv_running_balance",
        )
        currency_column = _column_choice(
            "Currency column (optional)",
            preview,
            preview.suggestions.currency,
            optional=True,
            key="csv_currency",
        )
        external_id_column = _column_choice(
            "Bank transaction ID (optional)",
            preview,
            preview.suggestions.external_id,
            optional=True,
            key="csv_external_id",
        )
        transaction_type_column = _column_choice(
            "Transaction type (optional)",
            preview,
            preview.suggestions.transaction_type,
            optional=True,
            key="csv_transaction_type",
        )

        today = date.today()
        suggested_start, suggested_end, dates_detected = suggested_csv_statement_period(
            preview,
            cast(str, transaction_date_column),
            fallback_date=today,
        )
        date_key = f"{preview.file_hash[:12]}_{transaction_date_column}"
        start_date = st.date_input(
            "Statement start",
            value=suggested_start,
            key=f"csv_statement_start_v2_{date_key}",
        )
        end_date = st.date_input(
            "Statement end",
            value=suggested_end,
            key=f"csv_statement_end_v2_{date_key}",
        )
        if dates_detected:
            st.caption(
                "Detected from every readable transaction date in the uploaded "
                "CSV. Check these dates against the statement before importing."
            )
        else:
            st.warning(
                "A complete date range could not be detected for this column. "
                "Enter the statement dates before importing."
            )
        coverage_status = st.selectbox(
            "Coverage status",
            tuple(CoverageStatus),
            format_func=lambda value: value.value.replace("_", " ").title(),
        )
        missing_periods = st.text_area(
            "Known missing periods (one YYYY-MM-DD,YYYY-MM-DD range per line)",
            help="Choose Gapped if dates or statement pages are missing.",
        )
        include_balances = st.checkbox("The CSV reports statement balances")
        opening_balance = st.text_input("Opening balance (optional)")
        closing_balance = st.text_input("Closing balance (optional)")
        flags = cast(
            list[StatementFlag],
            st.multiselect(
                "Structured statement flags (optional)",
                tuple(StatementFlag),
                format_func=lambda value: value.value.replace("_", " ").title(),
            ),
        )
        note = st.text_area(
            "Statement note (optional)",
            max_chars=2_000,
            help="Reference only: notes never assign categories or affect forecasts.",
        )
        explicit_confirmation = st.checkbox(
            "I reviewed the file, mapping, dates, signs, gaps, balances and flags."
        )
        submitted = st.form_submit_button("Confirm and import CSV", type="primary")

    try:
        coverage = build_statement_coverage(
            start_date=start_date,
            end_date=end_date,
            status=coverage_status,
            missing_periods_text=missing_periods,
        )
        _render_coverage(coverage)
    except ValueError:
        coverage = None
        st.error("Check the statement dates, status, and missing-period ranges.")

    if not submitted:
        return
    if not explicit_confirmation:
        st.error("Explicit confirmation is required before importing a CSV.")
        return
    if coverage is None:
        return
    try:
        mapping = CsvColumnMapping(
            transaction_date_column=cast(str, transaction_date_column),
            description_column=cast(str, description_column),
            signed_amount_column=signed_amount_column,
            debit_amount_column=debit_amount_column,
            credit_amount_column=credit_amount_column,
            posting_date_column=posting_date_column,
            running_balance_column=running_balance_column,
            currency_column=currency_column,
            external_id_column=external_id_column,
            transaction_type_column=transaction_type_column,
        )
        balances = (
            build_statement_balances(
                currency=account.currency,
                opening_balance_text=opening_balance,
                closing_balance_text=closing_balance,
            )
            if include_balances
            else None
        )
        context = build_import_context(
            account_id=account.account_id,
            coverage=coverage,
            balances=balances,
            flags=tuple(flags),
            note=note,
        )
        plan = CsvImportPlan(
            account_id=account.account_id,
            account_currency=account.currency,
            statement_context=context,
            mapping=mapping,
        )
        confirmation = CsvImportConfirmation(
            preview_file_hash=preview.file_hash,
            user_confirmed=True,
            confirmed_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        with loading_state("Revalidating and importing the exact CSV…"):
            result = client.confirm_csv(
                document,
                plan=plan,
                confirmation=confirmation,
            )
    except ApiClientError as error:
        _render_import_error(error)
        return
    except ValueError:
        st.error("Check the mapping, balances, flags, and optional note.")
        return
    _render_csv_result(result)


def _default_gap_text(coverage: StatementCoverage | None) -> str:
    if coverage is None:
        return ""
    return "\n".join(
        f"{item.start_date.isoformat()},{item.end_date.isoformat()}"
        for item in coverage.missing_periods
    )


def _render_pdf_evidence(review: StatementReview) -> None:
    columns = st.columns(3)
    columns[0].metric("Extracted rows", len(review.rows))
    columns[1].metric("Targeted review", len(review.uncertain_rows))
    columns[2].metric(
        "Reconciliation", review.reconciliation.status.value.replace("_", " ")
    )
    st.dataframe(pdf_review_rows(review), hide_index=True, use_container_width=True)
    if review.statement_coverage is not None:
        _render_coverage(review.statement_coverage)
    if review.balances is not None:
        st.caption(
            "Extracted balances: opening "
            f"{review.balances.opening_balance} · closing "
            f"{review.balances.closing_balance} {review.balances.currency.value}"
        )
    if review.reconciliation.status is ReconciliationStatus.MISMATCH:
        st.warning(
            "Opening balance plus extracted transactions does not match the closing "
            f"balance. Difference: {review.reconciliation.unexplained_difference}."
        )
    elif review.reconciliation.status is ReconciliationStatus.UNAVAILABLE:
        st.warning(
            "Balance reconciliation is unavailable; do not infer that it passed."
        )
    for issue in review.document_issues:
        st.warning(f"Document issue `{issue.code}`: {issue.message}")


def _row_decision_fields(
    row: StatementReviewRow,
    *,
    index: int,
) -> _PendingRowDecision:
    raw_amount = (
        row.original.signed_amount_text
        or row.original.debit_amount_text
        or row.original.credit_amount_text
    )
    st.markdown(
        f"**Targeted review row {index} · page {row.source_identity.page_number}**"
    )
    st.caption(
        f"Recognised source: {row.original.transaction_date_text} · "
        f"{row.original.description_text} · "
        f"{raw_amount}"
    )
    for confidence in row.field_confidences:
        st.caption(f"{confidence.field.value}: {confidence.confidence:.0%} confidence")
    for issue in row.issues:
        st.warning(f"Row issue `{issue.code}`: {issue.message}")
    decision = st.radio(
        "Decision",
        tuple(RowDecision),
        format_func=lambda value: value.value.title(),
        horizontal=True,
        key=f"pdf_row_{index}_decision",
    )
    draft = row.working_draft
    return _PendingRowDecision(
        row=row,
        decision=decision,
        transaction_date_text=st.text_input(
            "Transaction date (YYYY-MM-DD)",
            value=""
            if draft.transaction_date is None
            else draft.transaction_date.isoformat(),
            key=f"pdf_row_{index}_date",
        ),
        posting_date_text=st.text_input(
            "Posting date (optional, YYYY-MM-DD)",
            value="" if draft.posting_date is None else draft.posting_date.isoformat(),
            key=f"pdf_row_{index}_posting_date",
        ),
        description=st.text_input(
            "Description",
            value=draft.description or "",
            max_chars=500,
            key=f"pdf_row_{index}_description",
        ),
        amount_text=st.text_input(
            "Signed amount",
            value="" if draft.amount is None else str(draft.amount),
            key=f"pdf_row_{index}_amount",
        ),
        balance_after_text=st.text_input(
            "Running balance (optional)",
            value="" if draft.balance_after is None else str(draft.balance_after),
            key=f"pdf_row_{index}_balance",
        ),
    )


def _pdf_coverage_fields(
    review: StatementReview,
) -> tuple[bool, date, date, CoverageStatus, str]:
    extracted_dates = tuple(
        row.working_draft.transaction_date
        for row in review.rows
        if row.working_draft.transaction_date is not None
    )
    fallback = date.today()
    default_start = (
        review.statement_coverage.statement_start_date
        if review.statement_coverage is not None
        else min(extracted_dates, default=fallback)
    )
    default_end = (
        review.statement_coverage.statement_end_date
        if review.statement_coverage is not None
        else max(extracted_dates, default=fallback)
    )
    st.checkbox(
        "Include this statement's confirmed date coverage",
        value=True,
        disabled=True,
        help=(
            "PDF coverage is required so missing dates are never treated as zero "
            "activity."
        ),
    )
    start = st.date_input("Confirmed statement start", value=default_start)
    end = st.date_input("Confirmed statement end", value=default_end)
    default_status = (
        review.statement_coverage.status
        if review.statement_coverage is not None
        else CoverageStatus.UNKNOWN
    )
    status = st.selectbox(
        "Are all dates in this period included?",
        tuple(CoverageStatus),
        index=tuple(CoverageStatus).index(default_status),
        format_func=lambda value: value.value.replace("_", " ").title(),
    )
    gaps = st.text_area(
        "Confirmed missing periods (one YYYY-MM-DD,YYYY-MM-DD range per line)",
        value=_default_gap_text(review.statement_coverage),
    )
    return True, start, end, status, gaps


def _pdf_mapping_choice(
    label: str,
    preview: DigitalPdfMappingPreview,
    role: DigitalPdfColumnRole,
    *,
    optional: bool,
    key: str,
) -> str | None:
    column_ids = tuple(column.column_id for column in preview.columns)
    options: tuple[str | None, ...] = (None, *column_ids) if optional else column_ids
    hinted = next(
        (column.column_id for column in preview.columns if column.role_hint is role),
        None,
    )
    index = options.index(hinted) if hinted in options else 0
    labels = {
        column.column_id: f"{column.header_text} [{column.column_id}]"
        for column in preview.columns
    }
    return st.selectbox(
        label,
        options,
        index=index,
        format_func=lambda value: "Not provided" if value is None else labels[value],
        key=key,
    )


def _render_pdf_mapping(
    preview: DigitalPdfMappingPreview,
) -> DigitalPdfColumnMapping | None:
    """Collect a file-bound mapping without retaining uploaded financial rows."""
    st.warning(
        "The PDF contains a table, but its columns cannot be identified safely. "
        "Match the columns below before reviewing any transactions."
    )
    st.dataframe(pdf_mapping_rows(preview), hide_index=True, use_container_width=True)
    if preview.truncated:
        st.caption(
            f"Showing {len(preview.sample_rows)} of {preview.total_rows} reconstructed "
            "rows for column mapping. All rows are checked after mapping."
        )
    key_prefix = f"pdf_mapping_{preview.structure_digest[:12]}"
    date_column = _pdf_mapping_choice(
        "Transaction date column",
        preview,
        DigitalPdfColumnRole.TRANSACTION_DATE,
        optional=False,
        key=f"{key_prefix}_date",
    )
    description_column = _pdf_mapping_choice(
        "Description column",
        preview,
        DigitalPdfColumnRole.DESCRIPTION,
        optional=False,
        key=f"{key_prefix}_description",
    )
    has_separate_hint = all(
        any(column.role_hint is role for column in preview.columns)
        for role in (
            DigitalPdfColumnRole.DEBIT_AMOUNT,
            DigitalPdfColumnRole.CREDIT_AMOUNT,
        )
    )
    amount_layout = st.radio(
        "PDF amount layout",
        ("Signed amount", "Separate debit and credit"),
        index=1 if has_separate_hint else 0,
        horizontal=True,
        key=f"{key_prefix}_amount_layout",
    )
    signed_amount: str | None = None
    debit_amount: str | None = None
    credit_amount: str | None = None
    if amount_layout == "Signed amount":
        signed_amount = _pdf_mapping_choice(
            "Signed amount column",
            preview,
            DigitalPdfColumnRole.SIGNED_AMOUNT,
            optional=False,
            key=f"{key_prefix}_signed_amount",
        )
    else:
        debit_amount = _pdf_mapping_choice(
            "Debit column",
            preview,
            DigitalPdfColumnRole.DEBIT_AMOUNT,
            optional=False,
            key=f"{key_prefix}_debit",
        )
        credit_amount = _pdf_mapping_choice(
            "Credit column",
            preview,
            DigitalPdfColumnRole.CREDIT_AMOUNT,
            optional=False,
            key=f"{key_prefix}_credit",
        )
    running_balance = _pdf_mapping_choice(
        "Running balance column (optional)",
        preview,
        DigitalPdfColumnRole.RUNNING_BALANCE,
        optional=True,
        key=f"{key_prefix}_balance",
    )
    apply_mapping = st.checkbox(
        "I checked these columns against the PDF and want to apply this mapping.",
        key=f"{key_prefix}_confirmed",
    )
    if not apply_mapping:
        return None
    try:
        return DigitalPdfColumnMapping(
            file_hash=preview.file_hash,
            structure_digest=preview.structure_digest,
            transaction_date=cast(str, date_column),
            description=cast(str, description_column),
            signed_amount=signed_amount,
            debit_amount=debit_amount,
            credit_amount=credit_amount,
            running_balance=running_balance,
        )
    except ValueError:
        st.error("Each PDF column must have one valid and unambiguous role.")
        return None


def _render_pdf_result(result: PdfImportSummary) -> None:
    st.success("PDF confirmation completed and every extracted row was accounted for.")
    columns = st.columns(4)
    columns[0].metric("New transactions", result.imported_transactions)
    columns[1].metric("Exact duplicates", result.exact_duplicates_skipped)
    columns[2].metric("Needs duplicate review", result.probable_duplicates)
    columns[3].metric("Rejected rows", result.rejected_rows)
    if result.repeated_file:
        st.info("This exact PDF was already imported; no second copy was created.")
    if result.coverage.new_missing_periods or result.coverage.disconnected_range:
        st.warning("The confirmed statement leaves a gap or disconnected date range.")
    if result.coverage.overlap_periods:
        st.info("Part of this statement covers dates you previously imported.")


def _render_pdf_workflow(
    client: ImportApi,
    account: AccountResponse,
    document: UploadedDocument,
) -> None:
    try:
        with loading_state("Extracting the digital PDF and preparing review…"):
            result = client.prepare_pdf_review(
                document,
                account_id=account.account_id,
                account_currency=account.currency,
            )
    except ApiClientError as error:
        _render_import_error(error)
        return

    mapping: DigitalPdfColumnMapping | None = None
    if result.state is DigitalPdfReviewState.UNSUPPORTED_LAYOUT:
        st.error(result.guidance)
        st.caption(f"Reason: {result.reason_code.replace('_', ' ')}.")
        return
    if result.state is DigitalPdfReviewState.MAPPING_REQUIRED:
        assert result.mapping_preview is not None
        mapping = _render_pdf_mapping(result.mapping_preview)
        if mapping is None:
            return
        try:
            with loading_state("Applying the mapping to the exact PDF…"):
                result = client.prepare_pdf_review(
                    document,
                    account_id=account.account_id,
                    account_currency=account.currency,
                    mapping=mapping,
                )
        except ApiClientError as error:
            _render_import_error(error)
            return
        if result.state is DigitalPdfReviewState.UNSUPPORTED_LAYOUT:
            st.error(result.guidance)
            return
        if result.state is DigitalPdfReviewState.MAPPING_REQUIRED:
            st.error(
                "This mapping still leaves incomplete transaction rows. Adjust the "
                "column choices or use the bank's CSV export."
            )
            return

    review = result.review
    if review is None:
        st.error("The API did not return a complete PDF review.")
        return

    st.success("Your statement is ready to check. Nothing has been saved yet.")
    _render_pdf_evidence(review)
    preview_name = PurePath(document.filename).stem or "statement"
    st.download_button(
        "Download unconfirmed extraction as CSV",
        data=pdf_review_csv_bytes(review),
        file_name=f"{preview_name}-unconfirmed.csv",
        mime="text/csv",
        help=(
            "This file is a convenience preview, not proof that extraction is "
            "correct. Confirm against the original PDF."
        ),
    )
    st.caption(
        "The downloadable CSV is unconfirmed. It is not imported or trusted until "
        "you complete every review step below."
    )

    with st.form("pdf_review"):
        st.subheader("Check the extracted statement")
        coverage_enabled, start, end, coverage_status, gaps = _pdf_coverage_fields(
            review
        )
        coverage_confirmed = st.checkbox(
            "I confirm the statement period and any missing dates/pages."
        )
        opening_default = (
            ""
            if review.balances is None or review.balances.opening_balance is None
            else str(review.balances.opening_balance)
        )
        closing_default = (
            ""
            if review.balances is None or review.balances.closing_balance is None
            else str(review.balances.closing_balance)
        )
        opening = st.text_input("Confirmed opening balance", value=opening_default)
        closing = st.text_input("Confirmed closing balance", value=closing_default)
        balances_confirmed = st.checkbox(
            "I confirm every opening/closing balance extracted from the PDF.",
            disabled=not review.balance_evidence,
        )
        selected_date_format: DateFormat | None = None
        date_format_confirmed = not review.requires_date_format_confirmation
        if review.requires_date_format_confirmation:
            selected_date_format = st.selectbox(
                "Source date interpretation",
                tuple(DateFormat),
                format_func=lambda value: value.value.replace("_", " ").title(),
            )
            date_format_confirmed = st.checkbox(
                "I confirm the selected source date interpretation."
            )
        sign_confirmed = not review.requires_debit_credit_sign_confirmation
        if review.requires_debit_credit_sign_confirmation:
            sign_confirmed = st.checkbox(
                "I confirm debits are negative and credits are positive."
            )
        pending = tuple(
            _row_decision_fields(row, index=index)
            for index, row in enumerate(review.uncertain_rows, start=1)
        )
        acknowledge_mismatch = st.checkbox(
            "I acknowledge the unexplained balance difference.",
            disabled=review.reconciliation.status is not ReconciliationStatus.MISMATCH,
        )
        statement_approved = st.checkbox(
            "I reviewed the extraction and explicitly approve these PDF decisions."
        )
        submitted = st.form_submit_button("Confirm and import PDF", type="primary")

    if not submitted:
        return
    if not statement_approved:
        st.error("Explicit statement approval is required.")
        return
    if review.requires_date_format_confirmation and not date_format_confirmed:
        st.error("Confirm the source date interpretation.")
        return
    if review.requires_debit_credit_sign_confirmation and not sign_confirmed:
        st.error("Confirm the debit and credit sign convention.")
        return
    if coverage_enabled and not coverage_confirmed:
        st.error("Confirm the statement period and missing-date information.")
        return
    if review.balance_evidence and not balances_confirmed:
        st.error("Confirm every extracted statement balance.")
        return
    if (
        review.reconciliation.status is ReconciliationStatus.MISMATCH
        and not acknowledge_mismatch
    ):
        st.error("Acknowledge the balance mismatch before approval.")
        return
    try:
        coverage = (
            build_statement_coverage(
                start_date=start,
                end_date=end,
                status=coverage_status,
                missing_periods_text=gaps,
            )
            if coverage_enabled
            else None
        )
        balances = balances_confirmed_from_review(
            review,
            opening_balance_text=opening,
            closing_balance_text=closing,
        )
        row_reviews: tuple[RowReview, ...] = tuple(
            corrected_row_review(
                item.row,
                decision=item.decision,
                transaction_date_text=item.transaction_date_text,
                posting_date_text=item.posting_date_text,
                description=item.description,
                amount_text=item.amount_text,
                balance_after_text=item.balance_after_text,
            )
            for item in pending
        )
        approval = StatementApproval(
            file_hash=review.file_hash,
            approved_at=datetime.now(UTC),
            statement_approved=True,
            date_format=(selected_date_format if date_format_confirmed else None),
            sign_convention=(
                AmountSignConvention.DEBIT_NEGATIVE_CREDIT_POSITIVE
                if review.requires_debit_credit_sign_confirmation and sign_confirmed
                else None
            ),
            confirmed_statement_coverage=coverage,
            confirmed_balances=balances,
            acknowledge_balance_mismatch=acknowledge_mismatch,
            row_reviews=row_reviews,
        )
        with loading_state("Re-extracting and importing the exact approved PDF…"):
            confirmation_result = client.confirm_pdf(
                document,
                account_id=account.account_id,
                account_currency=account.currency,
                approval=approval,
                mapping=mapping,
            )
    except ApiClientError as error:
        _render_import_error(error)
        return
    except ValueError:
        st.error("Check the corrected rows, coverage, balances, and decisions.")
        return
    _render_pdf_result(confirmation_result)


def render_import_page(
    client: ImportApi,
    session: FrontendSessionState,
) -> FrontendSessionState:
    """Render onboarding/import controls and return only safe session selections."""
    render_page_header(
        "Add data",
        "Import a statement",
        "Upload a bank export, check what was recognised, and decide what the app "
        "may use. Nothing is accepted without your confirmation.",
    )
    render_privacy_notice()
    profile = _load_or_create_profile(client)
    if profile is None:
        return session.model_copy(update={"user_profile_id": None, "account_id": None})
    account = _select_or_create_account(client, profile, session.account_id)
    if account is None:
        return session.model_copy(
            update={"user_profile_id": profile.profile_id, "account_id": None}
        )
    updated = session.model_copy(
        update={
            "user_profile_id": profile.profile_id,
            "account_id": account.account_id,
        }
    )

    st.subheader("Upload a statement")
    kind = st.radio(
        "Statement source",
        tuple(UploadKind),
        format_func=lambda value: value.value,
        horizontal=True,
    )
    uploaded = cast(
        UploadedFileLike | None,
        st.file_uploader(
            "Choose a statement",
            type=kind.extensions,
            accept_multiple_files=False,
            help="The file is processed only by CashFlow AI on this device.",
        ),
    )
    if uploaded is None:
        render_empty_state(
            "No statement selected",
            "Choose a bank CSV export or selectable-text digital PDF to begin.",
        )
        return updated
    document = UploadedDocument(
        filename=uploaded.name,
        content=uploaded.getvalue(),
        mime_type=kind.mime_type,
    )
    if kind is UploadKind.CSV:
        _render_csv_workflow(client, account, document)
    else:
        _render_pdf_workflow(client, account, document)
    return updated


__all__ = ["ImportApi", "UploadedFileLike", "render_import_page"]
