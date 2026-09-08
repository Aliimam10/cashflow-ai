"""Tests for the thin Streamlit onboarding and import page."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import MagicMock

import pytest

import cashflow_ai.frontend.import_page as page
from cashflow_ai.frontend.client import (
    ApiClientError,
    ApiClientErrorCode,
    UploadedDocument,
)
from cashflow_ai.frontend.import_workflow import UploadKind
from cashflow_ai.frontend.session import FrontendSessionState
from cashflow_ai.schemas.accounts import AccountType
from cashflow_ai.schemas.api import (
    AccountResponse,
    Page,
    UserProfileResponse,
)
from cashflow_ai.schemas.csv_imports import (
    CsvColumnSuggestions,
    CsvCoverageAnalysis,
    CsvEncoding,
    CsvImportSummary,
    CsvPreview,
    CsvPreviewRow,
)
from cashflow_ai.schemas.imports import (
    FieldConfidence,
    ImportIssue,
    IssueSeverity,
    TransactionField,
)
from cashflow_ai.schemas.pdf_api import (
    DigitalPdfColumn,
    DigitalPdfColumnMapping,
    DigitalPdfColumnRole,
    DigitalPdfMappingPreview,
    DigitalPdfMappingRow,
    DigitalPdfReviewState,
)
from cashflow_ai.schemas.reconciliation import (
    DateFormat,
    ReconciliationStatus,
    RowDecision,
    RowReview,
)
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    DateRange,
    StatementBalances,
    StatementCoverage,
)
from cashflow_ai.schemas.transactions import Currency, Direction, TransactionDraft

HASH_A = "a" * 64


def _profile() -> UserProfileResponse:
    return UserProfileResponse(
        profile_id="synthetic-profile",
        display_name="Fictional User",
        base_currency=Currency.GBP,
        timezone="UTC",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _account() -> AccountResponse:
    return AccountResponse(
        account_id="synthetic-account",
        user_profile_id="synthetic-profile",
        name="Fictional Current",
        account_type=AccountType.CURRENT,
        currency=Currency.GBP,
        institution_label="Example Bank",
        is_active=True,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _preview(*, separate_amounts: bool = False) -> CsvPreview:
    columns = (
        ("Date", "Description", "Debit", "Credit", "Balance")
        if separate_amounts
        else ("Date", "Description", "Amount", "Balance")
    )
    values = (
        ("2026-08-01", "SYNTHETIC SHOP", "10.00", "", "90.00")
        if separate_amounts
        else ("2026-08-01", "SYNTHETIC SHOP", "-10.00", "90.00")
    )
    return CsvPreview(
        source_filename="synthetic.csv",
        byte_size=100,
        file_hash=HASH_A,
        encoding=CsvEncoding.UTF_8,
        delimiter=",",
        columns=columns,
        rows=(CsvPreviewRow(source_row_number=2, values=values),),
        total_data_rows=1,
        truncated=False,
        suggestions=CsvColumnSuggestions(
            transaction_date=("Date",),
            description=("Description",),
            signed_amount=() if separate_amounts else ("Amount",),
            debit_amount=("Debit",) if separate_amounts else (),
            credit_amount=("Credit",) if separate_amounts else (),
            running_balance=("Balance",),
        ),
        suggested_date_column="Date",
        suggested_statement_period=DateRange(
            start_date=date(2025, 9, 1),
            end_date=date(2026, 8, 31),
        ),
    )


def _summary(*, warnings: bool = False) -> CsvImportSummary:
    return CsvImportSummary(
        import_batch_id="synthetic-batch",
        file_hash=HASH_A,
        rows_read=4 if warnings else 1,
        new_transactions=1,
        exact_duplicates_skipped=1 if warnings else 0,
        probable_duplicates=1 if warnings else 0,
        rejected_rows=1 if warnings else 0,
        repeated_file=warnings,
        exact_duplicate_rows=(2,) if warnings else (),
        probable_duplicate_rows=(3,) if warnings else (),
        rejected_row_numbers=(4,) if warnings else (),
        coverage=CsvCoverageAnalysis(
            previous_statement_count=1 if warnings else 0,
            new_missing_periods=(
                (DateRange(start_date=date(2026, 8, 2), end_date=date(2026, 8, 3)),)
                if warnings
                else ()
            ),
            overlap_periods=(
                (DateRange(start_date=date(2026, 8, 1), end_date=date(2026, 8, 1)),)
                if warnings
                else ()
            ),
            disconnected_range=warnings,
        ),
    )


def _ui(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    ui = MagicMock()
    ui.form.return_value = nullcontext()
    ui.expander.return_value = nullcontext()
    monkeypatch.setattr(page, "st", ui)
    monkeypatch.setattr(page, "loading_state", lambda message: nullcontext())
    monkeypatch.setattr(page, "render_page_header", MagicMock())
    return ui


def _api_error(problem_code: str | None = None) -> ApiClientError:
    return ApiClientError(
        ApiClientErrorCode.API_REJECTED_REQUEST,
        "the local API rejected the request",
        problem_code=problem_code,
    )


def _review_mock(
    *,
    status: ReconciliationStatus = ReconciliationStatus.RECONCILED,
    date_confirmation: bool = False,
    sign_confirmation: bool = False,
    balances: bool = False,
    uncertain: bool = False,
) -> MagicMock:
    review = MagicMock()
    review.file_hash = HASH_A
    review.rows = (MagicMock(),)
    review.uncertain_rows = (MagicMock(),) if uncertain else ()
    review.statement_coverage = None
    review.balances = None
    review.balance_evidence = (MagicMock(),) if balances else ()
    review.document_issues = ()
    review.requires_date_format_confirmation = date_confirmation
    review.requires_debit_credit_sign_confirmation = sign_confirmation
    review.reconciliation.status = status
    review.reconciliation.unexplained_difference = Decimal("1.00")
    return review


def _mapping_preview() -> DigitalPdfMappingPreview:
    return DigitalPdfMappingPreview(
        file_hash=HASH_A,
        structure_digest="b" * 64,
        page_count=1,
        columns=(
            DigitalPdfColumn(
                column_id="column_1",
                header_text="Column 1",
                role_hint=DigitalPdfColumnRole.TRANSACTION_DATE,
            ),
            DigitalPdfColumn(
                column_id="column_2",
                header_text="Column 2",
                role_hint=DigitalPdfColumnRole.DESCRIPTION,
            ),
            DigitalPdfColumn(
                column_id="column_3",
                header_text="Column 3",
                role_hint=DigitalPdfColumnRole.SIGNED_AMOUNT,
            ),
        ),
        sample_rows=(
            DigitalPdfMappingRow(
                page_number=1,
                page_record_number=1,
                values=("01/08/2026", "SYNTHETIC SHOP", "-10.00"),
            ),
        ),
        total_rows=2,
        truncated=True,
    )


def _prepare_pdf_test(
    monkeypatch: pytest.MonkeyPatch,
    review: MagicMock,
    *,
    checkboxes: list[bool],
    submitted: bool = True,
) -> tuple[MagicMock, MagicMock]:
    ui = _ui(monkeypatch)
    ui.checkbox.side_effect = checkboxes
    ui.form_submit_button.return_value = submitted
    ui.text_input.side_effect = ["100.00", "90.00"]
    ui.selectbox.return_value = DateFormat.DAY_FIRST
    client = MagicMock()
    result = MagicMock()
    result.state = DigitalPdfReviewState.READY
    result.review = review
    result.mapping_preview = None
    client.prepare_pdf_review.return_value = result
    monkeypatch.setattr(page, "_render_pdf_evidence", MagicMock())
    monkeypatch.setattr(page, "_render_pdf_result", MagicMock())
    monkeypatch.setattr(
        page,
        "_pdf_coverage_fields",
        MagicMock(
            return_value=(
                False,
                date(2026, 8, 1),
                date(2026, 8, 31),
                CoverageStatus.UNKNOWN,
                "",
            )
        ),
    )
    return ui, client


def test_import_error_adds_known_guidance_only(monkeypatch: pytest.MonkeyPatch) -> None:
    ui = _ui(monkeypatch)
    display = MagicMock()
    monkeypatch.setattr(page, "render_error", display)
    routed = _api_error("ocr_required")

    page._render_import_error(routed)
    page._render_import_error(_api_error("unknown_problem"))

    assert display.call_count == 2
    ui.caption.assert_called_once_with(
        "Scanned and image-only PDFs are outside Version 1. Download a CSV export "
        "from the bank instead."
    )


def test_existing_profile_is_displayed(monkeypatch: pytest.MonkeyPatch) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    client.current_profile.return_value = _profile()

    assert page._load_or_create_profile(client) == _profile()
    ui.caption.assert_called_once_with("Local profile: Fictional User · GBP · UTC")


def test_profile_lookup_failure_stops_on_non_missing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ui(monkeypatch)
    client = MagicMock()
    client.current_profile.side_effect = _api_error("database_unavailable")
    display = MagicMock()
    monkeypatch.setattr(page, "_render_import_error", display)

    assert page._load_or_create_profile(client) is None
    display.assert_called_once()


def test_missing_profile_waits_for_explicit_form_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    ui.text_input.side_effect = ["", "UTC"]
    ui.selectbox.return_value = Currency.GBP
    ui.form_submit_button.return_value = False
    client = MagicMock()
    client.current_profile.side_effect = _api_error("profile_not_found")

    assert page._load_or_create_profile(client) is None
    client.create_profile.assert_not_called()


def test_profile_form_handles_validation_api_failure_and_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    client.current_profile.side_effect = _api_error("profile_not_found")
    ui.selectbox.return_value = Currency.GBP
    ui.form_submit_button.return_value = True

    ui.text_input.side_effect = ["Fictional", "Mars/Olympus"]
    assert page._load_or_create_profile(client) is None
    ui.error.assert_called_with("Check the profile name, currency, and IANA timezone.")

    ui.text_input.side_effect = ["Fictional", "UTC"]
    client.create_profile.side_effect = _api_error("profile_already_exists")
    assert page._load_or_create_profile(client) is None

    ui.text_input.side_effect = ["Fictional", "UTC"]
    client.create_profile.side_effect = None
    client.create_profile.return_value = _profile()
    assert page._load_or_create_profile(client) == _profile()
    ui.success.assert_called_with("Local profile created.")


def test_account_list_failure_and_empty_form_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    display = MagicMock()
    empty = MagicMock()
    monkeypatch.setattr(page, "_render_import_error", display)
    monkeypatch.setattr(page, "render_empty_state", empty)
    client = MagicMock()
    client.list_accounts.side_effect = _api_error("database_unavailable")
    assert page._select_or_create_account(client, _profile(), None) is None
    display.assert_called_once()

    client.list_accounts.side_effect = None
    client.list_accounts.return_value = Page[AccountResponse](
        items=(), limit=100, offset=0, total=0
    )
    ui.text_input.side_effect = ["", ""]
    ui.selectbox.side_effect = [AccountType.CURRENT, Currency.GBP]
    ui.form_submit_button.return_value = False
    assert page._select_or_create_account(client, _profile(), None) is None
    empty.assert_called_once()


def test_existing_account_selection_prefers_saved_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    account = _account()
    client = MagicMock()
    client.list_accounts.return_value = Page[AccountResponse](
        items=(account,), limit=100, offset=0, total=1
    )
    ui.text_input.side_effect = ["", ""]
    ui.selectbox.side_effect = [AccountType.CURRENT, Currency.GBP, account.account_id]
    ui.form_submit_button.return_value = False

    selected = page._select_or_create_account(client, _profile(), account.account_id)

    assert selected == account
    assert page._account_label(account) == "Fictional Current · current · GBP"
    assert ui.selectbox.call_args_list[-1].kwargs["index"] == 0


def test_account_form_handles_validation_api_failure_and_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    client.list_accounts.return_value = Page[AccountResponse](
        items=(), limit=100, offset=0, total=0
    )
    ui.form_submit_button.return_value = True

    ui.text_input.side_effect = ["", ""]
    ui.selectbox.side_effect = [AccountType.CURRENT, Currency.GBP]
    assert page._select_or_create_account(client, _profile(), None) is None
    ui.error.assert_called_with("Enter a name and supported account details.")

    ui.text_input.side_effect = ["Fictional Current", "Example Bank"]
    ui.selectbox.side_effect = [AccountType.CURRENT, Currency.GBP]
    client.create_account.side_effect = _api_error("account_name_exists")
    assert page._select_or_create_account(client, _profile(), None) is None

    ui.text_input.side_effect = ["Fictional Current", "Example Bank"]
    ui.selectbox.side_effect = [
        AccountType.CURRENT,
        Currency.GBP,
        "synthetic-account",
    ]
    client.create_account.side_effect = None
    client.create_account.return_value = _account()
    assert page._select_or_create_account(client, _profile(), None) == _account()
    ui.success.assert_called_with(
        "Account added without storing bank credentials or numbers."
    )


def test_column_choice_delegates_suggestion_and_optional_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    ui.selectbox.return_value = "Amount"
    preview = _preview()

    assert (
        page._column_choice(
            "Amount",
            preview,
            ("Amount",),
            optional=True,
            key="amount",
        )
        == "Amount"
    )
    assert ui.selectbox.call_args.kwargs["index"] == 3
    assert ui.selectbox.call_args.kwargs["format_func"](None) == "Not provided"
    assert ui.selectbox.call_args.kwargs["format_func"]("Amount") == "Amount"


def test_coverage_and_csv_result_warnings_are_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    coverage = StatementCoverage(
        statement_start_date=date(2026, 8, 1),
        statement_end_date=date(2026, 8, 31),
        status=CoverageStatus.GAPPED,
        missing_periods=(
            DateRange(start_date=date(2026, 8, 10), end_date=date(2026, 8, 11)),
        ),
    )
    page._render_coverage(coverage)
    page._render_csv_result(_summary(warnings=True))
    page._render_csv_result(_summary())

    assert ui.warning.call_count == 2
    assert ui.info.call_count == 2
    assert ui.success.call_count == 2


def _configure_csv_form(
    ui: MagicMock,
    *,
    layout: str = "Signed amount",
    submitted: bool = False,
    include_balances: bool = False,
    confirmed: bool = False,
) -> None:
    ui.radio.return_value = layout
    ui.date_input.side_effect = [date(2026, 8, 1), date(2026, 8, 31)]
    ui.selectbox.return_value = CoverageStatus.COMPLETE
    ui.text_area.side_effect = ["", "Synthetic note"]
    ui.text_input.side_effect = ["100.00", "90.00"]
    ui.checkbox.side_effect = [include_balances, confirmed]
    ui.multiselect.return_value = []
    ui.form_submit_button.return_value = submitted


def test_csv_preview_failure_and_both_mapping_layouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    client.preview_csv.side_effect = _api_error("malformed_csv")
    display = MagicMock()
    monkeypatch.setattr(page, "_render_import_error", display)
    page._render_csv_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.csv", b"synthetic", "text/csv"),
    )
    display.assert_called_once()

    client.preview_csv.side_effect = None
    client.preview_csv.return_value = _preview()
    choices = MagicMock(
        side_effect=["Date", "Description", "Amount", None, "Balance", None, None, None]
    )
    monkeypatch.setattr(page, "_column_choice", choices)
    _configure_csv_form(ui)
    page._render_csv_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.csv", b"synthetic", "text/csv"),
    )
    assert choices.call_count == 8
    assert ui.date_input.call_args_list[0].kwargs["value"] == date(2025, 9, 1)
    assert ui.date_input.call_args_list[1].kwargs["value"] == date(2026, 8, 31)
    assert "aaaaaaaaaaaa_Date" in ui.date_input.call_args_list[0].kwargs["key"]

    choices.reset_mock(side_effect=True)
    choices.side_effect = [
        "Date",
        "Description",
        "Debit",
        "Credit",
        None,
        "Balance",
        None,
        None,
        None,
    ]
    client.preview_csv.return_value = _preview(separate_amounts=True)
    _configure_csv_form(ui, layout="Separate debit and credit")
    page._render_csv_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.csv", b"synthetic", "text/csv"),
    )
    assert choices.call_count == 9


def test_csv_form_requires_manual_dates_when_preview_cannot_infer_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    client.preview_csv.return_value = _preview().model_copy(
        update={
            "suggested_date_column": None,
            "suggested_statement_period": None,
        }
    )
    monkeypatch.setattr(
        page,
        "_column_choice",
        MagicMock(
            side_effect=[
                "Date",
                "Description",
                "Amount",
                None,
                "Balance",
                None,
                None,
                None,
            ]
        ),
    )
    _configure_csv_form(ui)

    page._render_csv_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.csv", b"synthetic", "text/csv"),
    )

    assert any(
        "could not be detected" in call.args[0] for call in ui.warning.call_args_list
    )


def test_csv_submission_requires_confirmation_and_valid_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    client.preview_csv.return_value = _preview()
    monkeypatch.setattr(
        page,
        "_column_choice",
        MagicMock(
            side_effect=[
                "Date",
                "Description",
                "Amount",
                None,
                "Balance",
                None,
                None,
                None,
            ]
        ),
    )
    _configure_csv_form(ui, submitted=True, confirmed=False)
    page._render_csv_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.csv", b"synthetic", "text/csv"),
    )
    ui.error.assert_called_with(
        "Explicit confirmation is required before importing a CSV."
    )

    monkeypatch.setattr(
        page,
        "_column_choice",
        MagicMock(
            side_effect=[
                "Date",
                "Description",
                "Amount",
                None,
                "Balance",
                None,
                None,
                None,
            ]
        ),
    )
    _configure_csv_form(ui, submitted=True, confirmed=True)
    ui.date_input.side_effect = [date(2026, 8, 31), date(2026, 8, 1)]
    page._render_csv_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.csv", b"synthetic", "text/csv"),
    )
    assert client.confirm_csv.call_count == 0


def test_csv_submission_handles_contract_api_error_and_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    client.preview_csv.return_value = _preview()
    result_display = MagicMock()
    error_display = MagicMock()
    monkeypatch.setattr(page, "_render_csv_result", result_display)
    monkeypatch.setattr(page, "_render_import_error", error_display)

    def choices() -> MagicMock:
        return MagicMock(
            side_effect=[
                "Date",
                "Description",
                "Amount",
                None,
                "Balance",
                None,
                None,
                None,
            ]
        )

    monkeypatch.setattr(page, "_column_choice", choices())
    _configure_csv_form(ui, submitted=True, include_balances=True, confirmed=True)
    client.confirm_csv.side_effect = _api_error("preview_changed")
    page._render_csv_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.csv", b"synthetic", "text/csv"),
    )
    error_display.assert_called_once()

    monkeypatch.setattr(page, "_column_choice", choices())
    _configure_csv_form(ui, submitted=True, include_balances=True, confirmed=True)
    client.confirm_csv.side_effect = None
    client.confirm_csv.return_value = _summary()
    page._render_csv_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.csv", b"synthetic", "text/csv"),
    )
    result_display.assert_called_once_with(_summary())
    sent_plan = client.confirm_csv.call_args.kwargs["plan"]
    assert sent_plan.statement_context.note == "Synthetic note"
    assert sent_plan.statement_context.balances.opening_balance == Decimal("100.00")

    monkeypatch.setattr(page, "_column_choice", choices())
    _configure_csv_form(ui, submitted=True, include_balances=True, confirmed=True)
    ui.text_input.side_effect = ["not-money", "90.00"]
    page._render_csv_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.csv", b"synthetic", "text/csv"),
    )
    ui.error.assert_called_with(
        "Check the mapping, balances, flags, and optional note."
    )


def test_default_gap_text_and_pdf_coverage_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    coverage = StatementCoverage(
        statement_start_date=date(2026, 8, 1),
        statement_end_date=date(2026, 8, 31),
        status=CoverageStatus.GAPPED,
        missing_periods=(
            DateRange(start_date=date(2026, 8, 4), end_date=date(2026, 8, 5)),
        ),
    )
    assert page._default_gap_text(None) == ""
    assert page._default_gap_text(coverage) == "2026-08-04,2026-08-05"

    review = _review_mock(balances=True)
    review.statement_coverage = coverage
    ui.checkbox.return_value = True
    ui.date_input.side_effect = [date(2026, 8, 1), date(2026, 8, 31)]
    ui.selectbox.return_value = CoverageStatus.GAPPED
    ui.text_area.return_value = "2026-08-04,2026-08-05"
    result = page._pdf_coverage_fields(review)
    assert result[0] is True
    assert result[-1] == "2026-08-04,2026-08-05"

    review.statement_coverage = None
    review.balance_evidence = ()
    ui.checkbox.return_value = False
    dated = MagicMock()
    dated.working_draft.transaction_date = date(2026, 8, 7)
    review.rows = (dated,)
    ui.date_input.side_effect = [date(2026, 8, 7), date(2026, 8, 7)]
    fallback_result = page._pdf_coverage_fields(review)
    assert fallback_result[0] is True

    undated = MagicMock()
    undated.working_draft.transaction_date = None
    review.rows = (undated,)
    ui.date_input.side_effect = [date.today(), date.today()]
    page._pdf_coverage_fields(review)


def test_pdf_evidence_and_result_show_reconciliation_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    review = _review_mock(status=ReconciliationStatus.MISMATCH)
    review.rows = (MagicMock(),)
    review.uncertain_rows = ()
    review.statement_coverage = StatementCoverage(
        statement_start_date=date(2026, 8, 1),
        statement_end_date=date(2026, 8, 31),
        status=CoverageStatus.COMPLETE,
    )
    review.balances = StatementBalances(
        opening_balance=Decimal("100.00"), closing_balance=Decimal("89.00")
    )
    review.document_issues = (
        ImportIssue(
            code="synthetic_warning",
            message="Synthetic document warning",
            severity=IssueSeverity.WARNING,
        ),
    )
    monkeypatch.setattr(page, "pdf_review_rows", MagicMock(return_value=({},)))
    coverage_display = MagicMock()
    monkeypatch.setattr(page, "_render_coverage", coverage_display)

    page._render_pdf_evidence(review)
    unavailable = _review_mock(status=ReconciliationStatus.UNAVAILABLE)
    page._render_pdf_evidence(unavailable)
    reconciled = _review_mock(status=ReconciliationStatus.RECONCILED)
    page._render_pdf_evidence(reconciled)
    result = MagicMock()
    result.imported_transactions = 2
    result.exact_duplicates_skipped = 1
    result.probable_duplicates = 1
    result.rejected_rows = 1
    result.repeated_file = True
    result.coverage.new_missing_periods = (1,)
    result.coverage.disconnected_range = True
    result.coverage.overlap_periods = (1,)
    page._render_pdf_result(result)

    coverage_display.assert_called_once()
    assert ui.warning.call_count == 4
    ui.success.assert_called_once()
    assert ui.info.call_count == 2


def test_pdf_result_without_coverage_warnings_reports_only_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    result = MagicMock()
    result.imported_transactions = 1
    result.exact_duplicates_skipped = 0
    result.probable_duplicates = 0
    result.rejected_rows = 0
    result.repeated_file = False
    result.coverage.new_missing_periods = ()
    result.coverage.disconnected_range = False
    result.coverage.overlap_periods = ()

    page._render_pdf_result(result)

    ui.success.assert_called_once()
    ui.warning.assert_not_called()
    ui.info.assert_not_called()


def test_targeted_row_fields_show_confidence_and_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    ui.radio.return_value = RowDecision.CONFIRM
    ui.text_input.side_effect = [
        "2026-08-02",
        "",
        "SYNTHETIC CORRECTED",
        "-9.00",
        "91.00",
    ]
    row = MagicMock()
    row.source_identity.page_number = 1
    row.original.transaction_date_text = "01/08/2026"
    row.original.description_text = "SYNTHETIC SHOP"
    row.original.signed_amount_text = None
    row.original.debit_amount_text = "10.00"
    row.original.credit_amount_text = None
    row.field_confidences = (
        FieldConfidence(
            field=TransactionField.AMOUNT,
            confidence=0.70,
            raw_value="10.00",
        ),
    )
    row.issues = (
        ImportIssue(
            code="synthetic_issue",
            message="Synthetic row issue",
            severity=IssueSeverity.WARNING,
        ),
    )
    row.working_draft = TransactionDraft(
        transaction_date=date(2026, 8, 1),
        description="SYNTHETIC SHOP",
        amount=Decimal("-10.00"),
        balance_after=Decimal("90.00"),
        currency=Currency.GBP,
        account_id="synthetic-account",
        direction=Direction.OUTFLOW,
    )

    pending = page._row_decision_fields(row, index=1)

    assert pending.decision is RowDecision.CONFIRM
    assert pending.amount_text == "-9.00"
    assert ui.caption.call_count == 2
    ui.warning.assert_called_once()


def test_pdf_prepare_failure_and_unsupported_layout_recommend_csv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf")
    review = _review_mock()
    ui, client = _prepare_pdf_test(
        monkeypatch, review, checkboxes=[True, False, False, False], submitted=False
    )
    display = MagicMock()
    monkeypatch.setattr(page, "_render_import_error", display)
    client.prepare_pdf_review.side_effect = _api_error("malformed_pdf")
    page._render_pdf_workflow(client, _account(), document)
    display.assert_called_once()

    unsupported = MagicMock()
    unsupported.state = DigitalPdfReviewState.UNSUPPORTED_LAYOUT
    unsupported.guidance = "Use a CSV export."
    unsupported.reason_code = "image_only_or_scanned_pdf"
    client.prepare_pdf_review.side_effect = None
    client.prepare_pdf_review.return_value = unsupported
    page._render_pdf_workflow(client, _account(), document)
    ui.error.assert_called_with("Use a CSV export.")
    ui.caption.assert_any_call("Reason: image only or scanned pdf.")


def test_pdf_mapping_requires_confirmation_and_returns_file_bound_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    preview = _mapping_preview()
    ui.selectbox.side_effect = ["column_1", "column_2", "column_3", None]
    ui.radio.return_value = "Signed amount"
    ui.checkbox.return_value = False
    assert page._render_pdf_mapping(preview) is None

    ui.selectbox.side_effect = ["column_1", "column_2", "column_3", None]
    ui.checkbox.return_value = True
    mapping = page._render_pdf_mapping(preview)
    assert mapping is not None
    assert mapping.file_hash == HASH_A
    assert mapping.structure_digest == "b" * 64
    assert mapping.signed_amount == "column_3"
    ui.caption.assert_any_call(
        "Showing 1 of 2 reconstructed rows for column mapping. All rows are checked "
        "after mapping."
    )


def test_pdf_mapping_rejects_duplicate_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    ui.selectbox.side_effect = ["column_1", "column_1", "column_3", None]
    ui.radio.return_value = "Signed amount"
    ui.checkbox.return_value = True

    assert page._render_pdf_mapping(_mapping_preview()) is None
    ui.error.assert_called_with(
        "Each PDF column must have one valid and unambiguous role."
    )


def test_pdf_mapping_supports_separate_debit_credit_without_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    preview = DigitalPdfMappingPreview(
        file_hash=HASH_A,
        structure_digest="c" * 64,
        page_count=1,
        columns=tuple(
            DigitalPdfColumn(
                column_id=f"column_{index}",
                header_text=label,
                role_hint=role,
            )
            for index, (label, role) in enumerate(
                (
                    ("Date", DigitalPdfColumnRole.TRANSACTION_DATE),
                    ("Description", DigitalPdfColumnRole.DESCRIPTION),
                    ("Debit", DigitalPdfColumnRole.DEBIT_AMOUNT),
                    ("Credit", DigitalPdfColumnRole.CREDIT_AMOUNT),
                    ("Balance", DigitalPdfColumnRole.RUNNING_BALANCE),
                ),
                start=1,
            )
        ),
        sample_rows=(
            DigitalPdfMappingRow(
                page_number=1,
                page_record_number=1,
                values=("01/08/2026", "SYNTHETIC SHOP", "10.00", "", "90.00"),
            ),
        ),
        total_rows=1,
        truncated=False,
    )
    ui.selectbox.side_effect = [
        "column_1",
        "column_2",
        "column_3",
        "column_4",
        "column_5",
    ]
    ui.radio.return_value = "Separate debit and credit"
    ui.checkbox.return_value = True

    mapping = page._render_pdf_mapping(preview)

    assert mapping is not None
    assert mapping.signed_amount is None
    assert mapping.debit_amount == "column_3"
    assert mapping.credit_amount == "column_4"
    assert mapping.running_balance == "column_5"
    ui.caption.assert_not_called()


def test_pdf_workflow_applies_mapping_then_renders_ready_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    ui.form_submit_button.return_value = False
    document = UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf")
    mapping_preview = _mapping_preview()
    mapping = DigitalPdfColumnMapping(
        file_hash=HASH_A,
        structure_digest="b" * 64,
        transaction_date="column_1",
        description="column_2",
        signed_amount="column_3",
    )
    first = MagicMock()
    first.state = DigitalPdfReviewState.MAPPING_REQUIRED
    first.mapping_preview = mapping_preview
    ready = MagicMock()
    ready.state = DigitalPdfReviewState.READY
    ready.review = _review_mock()
    client = MagicMock()
    client.prepare_pdf_review.side_effect = [first, ready]
    mapping_form = MagicMock(return_value=mapping)
    monkeypatch.setattr(page, "_render_pdf_mapping", mapping_form)
    monkeypatch.setattr(page, "_render_pdf_evidence", MagicMock())
    monkeypatch.setattr(page, "pdf_review_csv_bytes", MagicMock(return_value=b"csv"))

    page._render_pdf_workflow(client, _account(), document)

    assert client.prepare_pdf_review.call_count == 2
    assert client.prepare_pdf_review.call_args_list[1].kwargs["mapping"] == mapping
    ui.download_button.assert_called_once()
    client.confirm_pdf.assert_not_called()


def test_pdf_workflow_waits_for_mapping_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ui(monkeypatch)
    result = MagicMock()
    result.state = DigitalPdfReviewState.MAPPING_REQUIRED
    result.mapping_preview = _mapping_preview()
    client = MagicMock()
    client.prepare_pdf_review.return_value = result
    monkeypatch.setattr(page, "_render_pdf_mapping", MagicMock(return_value=None))

    page._render_pdf_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf"),
    )

    client.prepare_pdf_review.assert_called_once()
    client.confirm_pdf.assert_not_called()


def test_pdf_workflow_handles_mapping_retry_failure_and_incomplete_ready_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    first = MagicMock()
    first.state = DigitalPdfReviewState.MAPPING_REQUIRED
    first.mapping_preview = _mapping_preview()
    client = MagicMock()
    client.prepare_pdf_review.side_effect = [first, _api_error("invalid_pdf_mapping")]
    monkeypatch.setattr(
        page,
        "_render_pdf_mapping",
        MagicMock(
            return_value=DigitalPdfColumnMapping(
                file_hash=HASH_A,
                structure_digest="b" * 64,
                transaction_date="column_1",
                description="column_2",
                signed_amount="column_3",
            )
        ),
    )
    display = MagicMock()
    monkeypatch.setattr(page, "_render_import_error", display)

    page._render_pdf_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf"),
    )
    display.assert_called_once()

    incomplete = MagicMock()
    incomplete.state = DigitalPdfReviewState.READY
    incomplete.review = None
    client.prepare_pdf_review.side_effect = None
    client.prepare_pdf_review.return_value = incomplete
    page._render_pdf_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf"),
    )
    ui.error.assert_called_with("The API did not return a complete PDF review.")


@pytest.mark.parametrize(
    ("second_state", "message"),
    [
        (DigitalPdfReviewState.UNSUPPORTED_LAYOUT, "Use CSV."),
        (
            DigitalPdfReviewState.MAPPING_REQUIRED,
            "This mapping still leaves incomplete transaction rows. Adjust the "
            "column choices or use the bank's CSV export.",
        ),
    ],
)
def test_pdf_workflow_stops_when_applied_mapping_is_not_ready(
    second_state: DigitalPdfReviewState,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    first = MagicMock()
    first.state = DigitalPdfReviewState.MAPPING_REQUIRED
    first.mapping_preview = _mapping_preview()
    second = MagicMock()
    second.state = second_state
    second.guidance = "Use CSV."
    client = MagicMock()
    client.prepare_pdf_review.side_effect = [first, second]
    monkeypatch.setattr(
        page,
        "_render_pdf_mapping",
        MagicMock(
            return_value=DigitalPdfColumnMapping(
                file_hash=HASH_A,
                structure_digest="b" * 64,
                transaction_date="column_1",
                description="column_2",
                signed_amount="column_3",
            )
        ),
    )

    page._render_pdf_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf"),
    )

    ui.error.assert_called_with(message)
    client.confirm_pdf.assert_not_called()


@pytest.mark.parametrize(
    ("review", "checkboxes", "coverage_enabled", "message"),
    [
        (
            _review_mock(),
            [True, False, False, False],
            False,
            "Explicit statement approval is required.",
        ),
        (
            _review_mock(date_confirmation=True),
            [True, False, False, False, True],
            False,
            "Confirm the source date interpretation.",
        ),
        (
            _review_mock(sign_confirmation=True),
            [True, False, False, False, True],
            False,
            "Confirm the debit and credit sign convention.",
        ),
        (
            _review_mock(),
            [False, False, False, True],
            True,
            "Confirm the statement period and missing-date information.",
        ),
        (
            _review_mock(balances=True),
            [True, False, False, True],
            False,
            "Confirm every extracted statement balance.",
        ),
        (
            _review_mock(status=ReconciliationStatus.MISMATCH),
            [True, False, False, True],
            False,
            "Acknowledge the balance mismatch before approval.",
        ),
    ],
)
def test_pdf_approval_requires_every_explicit_gate(
    review: MagicMock,
    checkboxes: list[bool],
    coverage_enabled: bool,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui, client = _prepare_pdf_test(monkeypatch, review, checkboxes=checkboxes)
    if coverage_enabled:
        cast(MagicMock, page._pdf_coverage_fields).return_value = (
            True,
            date(2026, 8, 1),
            date(2026, 8, 31),
            CoverageStatus.COMPLETE,
            "",
        )
    page._render_pdf_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf"),
    )
    ui.error.assert_called_with(message)
    client.confirm_pdf.assert_not_called()


def test_pdf_approval_handles_contract_error_api_error_and_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = _review_mock(
        status=ReconciliationStatus.MISMATCH,
        date_confirmation=True,
        sign_confirmation=True,
        balances=True,
        uncertain=True,
    )
    ui, client = _prepare_pdf_test(
        monkeypatch,
        review,
        checkboxes=[True, True, True, True, True, True],
    )
    cast(MagicMock, page._pdf_coverage_fields).return_value = (
        True,
        date(2026, 8, 1),
        date(2026, 8, 31),
        CoverageStatus.COMPLETE,
        "",
    )
    pending = page._PendingRowDecision(
        row=review.uncertain_rows[0],
        decision=RowDecision.REJECT,
        transaction_date_text="",
        posting_date_text="",
        description="",
        amount_text="",
        balance_after_text="",
    )
    monkeypatch.setattr(page, "_row_decision_fields", MagicMock(return_value=pending))
    monkeypatch.setattr(
        page,
        "corrected_row_review",
        MagicMock(
            return_value=RowReview(
                source_fingerprint=HASH_A,
                decision=RowDecision.REJECT,
            )
        ),
    )
    monkeypatch.setattr(
        page,
        "balances_confirmed_from_review",
        MagicMock(return_value=StatementBalances(opening_balance=Decimal("100.00"))),
    )
    client.confirm_pdf.side_effect = _api_error("file_changed")
    error_display = MagicMock()
    monkeypatch.setattr(page, "_render_import_error", error_display)
    page._render_pdf_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf"),
    )
    error_display.assert_called_once()

    ui.checkbox.side_effect = [True, True, True, True, True, True]
    ui.text_input.side_effect = ["100.00", "90.00"]
    client.confirm_pdf.side_effect = None
    client.confirm_pdf.return_value = MagicMock()
    page._render_pdf_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf"),
    )
    cast(MagicMock, page._render_pdf_result).assert_called_once()
    approval = client.confirm_pdf.call_args.kwargs["approval"]
    assert approval.date_format is DateFormat.DAY_FIRST
    assert approval.sign_convention.value == "debit_negative_credit_positive"

    ui.checkbox.side_effect = [True, True, True, True, True, True]
    ui.text_input.side_effect = ["100.00", "90.00"]
    monkeypatch.setattr(
        page,
        "build_statement_coverage",
        MagicMock(side_effect=ValueError("private")),
    )
    page._render_pdf_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf"),
    )
    ui.error.assert_called_with(
        "Check the corrected rows, coverage, balances, and decisions."
    )


def test_pdf_workflow_waits_for_form_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = _review_mock()
    _, client = _prepare_pdf_test(
        monkeypatch, review, checkboxes=[True, False, False, True], submitted=False
    )
    page._render_pdf_workflow(
        client,
        _account(),
        UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf"),
    )
    client.confirm_pdf.assert_not_called()


class _Upload:
    name = "synthetic.csv"

    def getvalue(self) -> bytes:
        return b"synthetic"


def test_import_page_updates_only_safe_state_and_dispatches_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = _ui(monkeypatch)
    client = MagicMock()
    profile = _profile()
    account = _account()
    monkeypatch.setattr(page, "render_privacy_notice", MagicMock())
    profile_loader = MagicMock(return_value=None)
    account_loader = MagicMock(return_value=None)
    monkeypatch.setattr(page, "_load_or_create_profile", profile_loader)
    monkeypatch.setattr(page, "_select_or_create_account", account_loader)
    session = FrontendSessionState(account_id="old-account")

    cleared = page.render_import_page(client, session)
    assert cleared.user_profile_id is None
    assert cleared.account_id is None

    profile_loader.return_value = profile
    no_account = page.render_import_page(client, session)
    assert no_account.user_profile_id == profile.profile_id
    assert no_account.account_id is None

    account_loader.return_value = account
    ui.radio.return_value = UploadKind.CSV
    ui.file_uploader.return_value = None
    empty = MagicMock()
    monkeypatch.setattr(page, "render_empty_state", empty)
    selected = page.render_import_page(client, session)
    assert selected.account_id == account.account_id
    empty.assert_called_once()

    csv_workflow = MagicMock()
    pdf_workflow = MagicMock()
    monkeypatch.setattr(page, "_render_csv_workflow", csv_workflow)
    monkeypatch.setattr(page, "_render_pdf_workflow", pdf_workflow)
    ui.file_uploader.return_value = _Upload()
    page.render_import_page(client, session)
    csv_workflow.assert_called_once()

    ui.radio.return_value = UploadKind.DIGITAL_PDF
    page.render_import_page(client, session)
    pdf_workflow.assert_called_once()
    sent_document = pdf_workflow.call_args.args[2]
    assert sent_document.content == b"synthetic"
    assert sent_document.mime_type == "application/pdf"
