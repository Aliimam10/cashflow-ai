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
    OcrStatusResponse,
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


def _prepare_pdf_test(
    monkeypatch: pytest.MonkeyPatch,
    review: MagicMock,
    *,
    checkboxes: list[bool],
    submitted: bool = True,
) -> tuple[MagicMock, MagicMock]:
    ui = _ui(monkeypatch)
    ui.slider.return_value = 0.85
    ui.checkbox.side_effect = checkboxes
    ui.form_submit_button.return_value = submitted
    ui.text_input.side_effect = ["100.00", "90.00"]
    ui.selectbox.return_value = DateFormat.DAY_FIRST
    client = MagicMock()
    client.prepare_pdf_review.return_value = review
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
        "This PDF contains image-only pages. Choose Scanned or camera PDF."
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
    dated = MagicMock()
    dated.working_draft.transaction_date = date(2026, 8, 7)
    review.rows = (dated,)
    ui.date_input.side_effect = [date(2026, 8, 7), date(2026, 8, 7)]
    page._pdf_coverage_fields(review)

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
    result.rows = (1, 2)
    result.rejected_rows = (3,)
    result.reconciliation.status = ReconciliationStatus.RECONCILED
    page._render_pdf_result(result)

    coverage_display.assert_called_once()
    assert ui.warning.call_count == 4
    ui.success.assert_called_once()


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


def test_ocr_status_failure_unavailable_and_pdf_prepare_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = UploadedDocument("synthetic.pdf", b"%PDF", "application/pdf")
    review = _review_mock()
    ui, client = _prepare_pdf_test(
        monkeypatch, review, checkboxes=[True, False, False, False], submitted=False
    )
    display = MagicMock()
    monkeypatch.setattr(page, "_render_import_error", display)
    client.ocr_status.side_effect = _api_error("ocr_engine_unavailable")
    page._render_pdf_workflow(client, _account(), document, UploadKind.OCR_PDF)
    display.assert_called_once()

    client.ocr_status.side_effect = None
    client.ocr_status.return_value = OcrStatusResponse(
        available=False, message="local Tesseract OCR is unavailable"
    )
    page._render_pdf_workflow(client, _account(), document, UploadKind.OCR_PDF)
    ui.error.assert_called_with(
        "Local Tesseract OCR is unavailable. Run `make check-ocr`."
    )

    client.ocr_status.return_value = OcrStatusResponse(
        available=True, message="local Tesseract OCR is available"
    )
    client.prepare_pdf_review.side_effect = _api_error("malformed_pdf")
    page._render_pdf_workflow(client, _account(), document, UploadKind.OCR_PDF)
    assert display.call_count == 2


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
        UploadKind.DIGITAL_PDF,
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
        UploadKind.DIGITAL_PDF,
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
        UploadKind.DIGITAL_PDF,
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
        UploadKind.DIGITAL_PDF,
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
        UploadKind.DIGITAL_PDF,
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
