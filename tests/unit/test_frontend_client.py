"""Tests for the typed loopback-only Streamlit API client."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import httpx2
import pytest
from pydantic import BaseModel, ConfigDict

from cashflow_ai.config import Environment, LogFormat, Settings
from cashflow_ai.frontend.client import (
    ApiClient,
    ApiClientError,
    ApiClientErrorCode,
    UploadedDocument,
    api_base_url,
)
from cashflow_ai.schemas.accounts import AccountType
from cashflow_ai.schemas.analytics import AnalyticsScope, AnalyticsView
from cashflow_ai.schemas.api import (
    AccountCreate,
    HealthResponse,
    PdfSourceType,
    TransactionSearchRequest,
    UserProfileCreate,
)
from cashflow_ai.schemas.api_decisions import (
    FinancialDataFreshnessRequest,
    FinancialRoleSuggestionRequest,
    RoleDecisionRequest,
    TransactionRoleReviewRequest,
)
from cashflow_ai.schemas.csv_imports import (
    CsvColumnMapping,
    CsvImportConfirmation,
    CsvImportPlan,
)
from cashflow_ai.schemas.duplicates import (
    DuplicateReviewDecision,
    DuplicateReviewRequest,
)
from cashflow_ai.schemas.financial_roles import TransactionReviewAction
from cashflow_ai.schemas.freshness import FreshnessPolicy
from cashflow_ai.schemas.hybrid_categorisation import (
    CategoryFeedback,
    CategoryFeedbackAction,
)
from cashflow_ai.schemas.reconciliation import StatementApproval
from cashflow_ai.schemas.statements import (
    CoverageStatus,
    DateRange,
    ImportContext,
    StatementCoverage,
)


class ExampleRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str


class ExampleResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    result: str


def _settings(*, host: str = "127.0.0.1") -> Settings:
    return Settings(
        environment=Environment.TEST,
        debug=False,
        log_level="WARNING",
        log_format=LogFormat.CONSOLE,
        timezone="UTC",
        api_host=host,
        api_port=8765,
    )


def test_client_reads_typed_status_profile_and_json_post() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx2.Response(200, json={"status": "ok", "version": "0.1.0"})
        if request.url.path == "/ready":
            return httpx2.Response(
                503,
                json={
                    "status": "not_ready",
                    "database_connection": True,
                    "database_schema": False,
                },
            )
        if request.url.path == "/api/v1/profiles/current":
            return httpx2.Response(
                200,
                json={
                    "profile_id": "synthetic-profile",
                    "display_name": "Fictional User",
                    "base_currency": "GBP",
                    "timezone": "Europe/London",
                    "created_at": "2026-08-01T12:00:00Z",
                    "updated_at": "2026-08-01T12:00:00Z",
                },
            )
        assert request.method == "POST"
        return httpx2.Response(200, json={"result": "accepted"})

    client = ApiClient(
        "http://127.0.0.1:8765/",
        transport=httpx2.MockTransport(handler),
    )
    assert client.health() == HealthResponse(version="0.1.0")
    assert client.readiness().status == "not_ready"
    assert client.current_profile().profile_id == "synthetic-profile"
    response = client.post(
        "/example",
        ExampleRequest(value="fictional"),
        ExampleResponse,
    )
    queried = client.get("/health", HealthResponse, params={"limit": 1})
    client.close()

    assert response == ExampleResponse(result="accepted")
    assert queried.status == "ok"
    assert requests[-1].url.query == b"limit=1"
    assert b"fictional" in requests[-2].content


def test_client_supports_onboarding_and_review_gated_upload_contracts() -> None:
    requests: list[httpx2.Request] = []
    profile = {
        "profile_id": "synthetic-profile",
        "display_name": "Fictional User",
        "base_currency": "GBP",
        "timezone": "UTC",
        "created_at": "2026-08-01T12:00:00Z",
        "updated_at": "2026-08-01T12:00:00Z",
    }
    account = {
        "account_id": "synthetic-account",
        "user_profile_id": "synthetic-profile",
        "name": "Fictional Current",
        "account_type": "current",
        "currency": "GBP",
        "institution_label": "Example Bank",
        "is_active": True,
        "created_at": "2026-08-01T12:00:00Z",
    }
    review = {
        "file_hash": "a" * 64,
        "source_type": "digital_pdf",
        "statement_coverage": None,
        "balances": None,
        "balance_evidence": [],
        "document_issues": [],
        "rows": [
            {
                "source_identity": {
                    "source_type": "digital_pdf",
                    "source_document_hash": "a" * 64,
                    "source_row_number": None,
                    "page_number": 1,
                    "page_record_number": 1,
                },
                "source_fingerprint": "b" * 64,
                "original": {
                    "transaction_date_text": "2026-08-01",
                    "description_text": "SYNTHETIC SHOP",
                    "signed_amount_text": "-10.00",
                    "debit_amount_text": None,
                    "credit_amount_text": None,
                    "posting_date_text": None,
                    "running_balance_text": None,
                    "currency_text": None,
                    "external_id_text": None,
                    "transaction_type_text": None,
                    "raw_fields": [{"column": "Date", "value": "2026-08-01"}],
                },
                "extracted_draft": {
                    "transaction_date": "2026-08-01",
                    "description": "SYNTHETIC SHOP",
                    "amount": "-10.00",
                    "currency": "GBP",
                    "account_id": "synthetic-account",
                    "direction": "outflow",
                },
                "working_draft": {
                    "transaction_date": "2026-08-01",
                    "description": "SYNTHETIC SHOP",
                    "amount": "-10.00",
                    "currency": "GBP",
                    "account_id": "synthetic-account",
                    "direction": "outflow",
                },
                "provenance": {
                    "source_type": "digital_pdf",
                    "method": "pdf_text",
                    "page_number": 1,
                },
                "source_line_numbers": [],
                "field_confidences": [],
                "issues": [],
                "review_reasons": [],
            }
        ],
        "reconciliation": {
            "status": "unavailable",
            "opening_balance": None,
            "signed_transaction_total": "-10.00",
            "expected_closing_balance": None,
            "closing_balance": None,
            "unexplained_difference": None,
            "tolerance": "0.01",
            "unusable_transaction_count": 0,
        },
        "ocr_confidence_threshold": 0.85,
        "requires_date_format_confirmation": False,
        "requires_debit_credit_sign_confirmation": False,
        "requires_statement_approval": True,
    }
    approved = {
        "file_hash": "a" * 64,
        "source_type": "digital_pdf",
        "approved_at": "2026-08-02T12:00:00Z",
        "date_format": None,
        "sign_convention": None,
        "statement_coverage": None,
        "coverage_was_edited": False,
        "balances": None,
        "balance_evidence": [],
        "balance_was_edited": False,
        "document_issues": [],
        "rows": [],
        "rejected_rows": [],
        "rejected_source_fingerprints": [],
        "reconciliation": review["reconciliation"],
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v1/profiles":
            return httpx2.Response(201, json=profile)
        if path.endswith("/accounts") and request.method == "GET":
            return httpx2.Response(
                200,
                json={"items": [account], "limit": 100, "offset": 0, "total": 1},
            )
        if path.endswith("/accounts"):
            return httpx2.Response(201, json=account)
        if path == "/api/v1/ocr/status":
            return httpx2.Response(
                200,
                json={
                    "engine": "tesseract",
                    "execution": "local_only",
                    "available": True,
                    "message": "local Tesseract OCR is available",
                },
            )
        if path.endswith("/csv/preview"):
            return httpx2.Response(
                200,
                json={
                    "source_filename": "synthetic.csv",
                    "byte_size": 40,
                    "file_hash": "a" * 64,
                    "encoding": "utf-8",
                    "delimiter": ",",
                    "columns": ["Date", "Description", "Amount"],
                    "rows": [
                        {
                            "source_row_number": 2,
                            "values": ["2026-08-01", "SYNTHETIC SHOP", "-10.00"],
                        }
                    ],
                    "total_data_rows": 1,
                    "truncated": False,
                    "suggestions": {
                        "transaction_date": ["Date"],
                        "description": ["Description"],
                        "signed_amount": ["Amount"],
                    },
                },
            )
        if path.endswith("/csv/confirm"):
            return httpx2.Response(
                200,
                json={
                    "import_batch_id": "synthetic-batch",
                    "file_hash": "a" * 64,
                    "rows_read": 1,
                    "new_transactions": 1,
                    "exact_duplicates_skipped": 0,
                    "probable_duplicates": 0,
                    "rejected_rows": 0,
                    "coverage": {"previous_statement_count": 0},
                },
            )
        if path.endswith("/pdf/review"):
            return httpx2.Response(200, json=review)
        assert path.endswith("/pdf/confirm")
        return httpx2.Response(200, json=approved)

    document = UploadedDocument(
        filename="synthetic.csv",
        content=b"Date,Description,Amount\n2026-08-01,SYNTHETIC SHOP,-10.00\n",
        mime_type="text/csv",
    )
    client = ApiClient(
        "http://127.0.0.1:8765",
        transport=httpx2.MockTransport(handler),
    )
    created_profile = client.create_profile(UserProfileCreate(timezone="UTC"))
    accounts = client.list_accounts("synthetic-profile")
    created_account = client.create_account(
        "synthetic-profile",
        AccountCreate(name="Fictional Current", account_type=AccountType.CURRENT),
    )
    ocr = client.ocr_status()
    preview = client.preview_csv(document)
    plan = CsvImportPlan(
        account_id="synthetic-account",
        statement_context=ImportContext(
            account_id="synthetic-account",
            coverage=StatementCoverage(
                statement_start_date=date(2026, 8, 1),
                statement_end_date=date(2026, 8, 31),
                status=CoverageStatus.COMPLETE,
            ),
        ),
        mapping=CsvColumnMapping(
            transaction_date_column="Date",
            description_column="Description",
            signed_amount_column="Amount",
        ),
    )
    summary = client.confirm_csv(
        document,
        plan=plan,
        confirmation=CsvImportConfirmation(
            preview_file_hash="a" * 64,
            user_confirmed=True,
            confirmed_at=datetime(2026, 8, 2, tzinfo=UTC),
        ),
    )
    pdf_document = UploadedDocument(
        filename="synthetic.pdf",
        content=b"%PDF-synthetic",
        mime_type="application/pdf",
    )
    prepared = client.prepare_pdf_review(
        pdf_document,
        source_type=PdfSourceType.DIGITAL_PDF,
        account_id="synthetic-account",
        account_currency=created_account.currency,
        ocr_confidence_threshold=0.85,
    )
    confirmed = client.confirm_pdf(
        pdf_document,
        source_type=PdfSourceType.DIGITAL_PDF,
        account_id="synthetic-account",
        account_currency=created_account.currency,
        ocr_confidence_threshold=0.85,
        approval=StatementApproval(
            file_hash="a" * 64,
            approved_at=datetime(2026, 8, 2, tzinfo=UTC),
            statement_approved=True,
        ),
    )
    client.close()

    assert created_profile.profile_id == "synthetic-profile"
    assert accounts.items == (created_account,)
    assert ocr.available is True
    assert preview.rows[0].values[1] == "SYNTHETIC SHOP"
    assert summary.new_transactions == 1
    assert prepared.rows[0].working_draft.amount == Decimal("-10.00")
    assert confirmed.rows == ()
    assert all(
        "multipart/form-data" in request.headers["content-type"]
        for request in requests[4:]
    )
    assert "SYNTHETIC SHOP" not in repr(document)


def test_client_exposes_typed_transaction_review_and_dashboard_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ApiClient(
        "http://127.0.0.1:8765",
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(500, request=request)
        ),
    )
    request = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(client, "_request", request)
    observed = datetime(2026, 9, 1, tzinfo=UTC)
    search = TransactionSearchRequest(user_profile_id="synthetic-profile")
    category = CategoryFeedback(
        user_profile_id="synthetic-profile",
        transaction_id="synthetic-transaction",
        category_id="food",
        action=CategoryFeedbackAction.TRANSACTION_ONLY,
        corrected_at=observed,
    )
    role_suggestion = FinancialRoleSuggestionRequest(
        user_profile_id="synthetic-profile"
    )
    role_decision = RoleDecisionRequest(reviewed_at=observed)
    role_correction = TransactionRoleReviewRequest(
        action=TransactionReviewAction.EXPENSE,
        changed_at=observed,
    )
    duplicate = DuplicateReviewRequest(
        decision=DuplicateReviewDecision.REJECT,
        decided_at=observed,
    )
    scope = AnalyticsScope(
        user_profile_id="synthetic-profile",
        account_ids=("synthetic-account",),
        period=DateRange(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31)),
        view=AnalyticsView.ACCOUNT,
    )
    freshness = FinancialDataFreshnessRequest(
        account_id="synthetic-account",
        as_of_date=date(2026, 8, 31),
        policy=FreshnessPolicy(
            max_transaction_age_days=45,
            max_balance_age_days=45,
            max_coverage_age_days=45,
            minimum_contiguous_coverage_days=60,
        ),
    )

    client.search_transactions(search)
    client.list_categories()
    client.correct_category(category)
    client.generate_role_suggestions(role_suggestion)
    client.list_role_reviews("synthetic-profile")
    client.decide_role_suggestion("synthetic-suggestion", role_decision, confirm=True)
    client.decide_role_suggestion("synthetic-suggestion", role_decision, confirm=False)
    client.correct_financial_role("synthetic-transaction", role_correction)
    client.list_duplicate_reviews("synthetic-profile")
    client.decide_duplicate("synthetic-profile", "synthetic-raw", duplicate)
    client.cash_flow(scope)
    client.freshness(freshness)
    client.close()

    paths = [call.args[1] for call in request.call_args_list]
    assert paths == [
        "/api/v1/transactions/search",
        "/api/v1/categories",
        "/api/v1/categorisation/feedback",
        "/api/v1/financial-roles/suggestions",
        "/api/v1/profiles/synthetic-profile/financial-roles/reviews",
        "/api/v1/financial-role-suggestions/synthetic-suggestion/confirm",
        "/api/v1/financial-role-suggestions/synthetic-suggestion/reject",
        "/api/v1/transactions/synthetic-transaction/financial-role",
        "/api/v1/profiles/synthetic-profile/duplicates/reviews",
        "/api/v1/profiles/synthetic-profile/duplicates/synthetic-raw/review",
        "/api/v1/analytics/cash-flow",
        "/api/v1/coverage/freshness",
    ]


def test_client_rejects_empty_or_oversized_record_identifiers() -> None:
    client = ApiClient(
        "http://127.0.0.1:8000",
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={}, request=request)
        ),
    )
    for value in ("", "x" * 256):
        with pytest.raises(ApiClientError) as error:
            client.list_accounts(value)
        assert error.value.code is ApiClientErrorCode.INVALID_REQUEST_PATH
    client.close()


def test_client_context_manager_closes_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ApiClient(
        "http://localhost:8000",
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200,
                json={"status": "ok", "version": "0.1.0"},
                request=request,
            )
        ),
    )
    close = MagicMock()
    monkeypatch.setattr(client, "close", close)

    with client as entered:
        assert entered is client
        assert entered.health().status == "ok"

    close.assert_called_once_with()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:8000",
        "http://example.com:8000",
        "http://127.0.0.1",
        "http://user:password@127.0.0.1:8000",
        "http://127.0.0.1:8000/api",
        "http://127.0.0.1:8000?private=true",
        "http://127.0.0.1:8000#fragment",
        "http://127.0.0.1:0",
        "http://127.0.0.1:99999",
        "http://[::1",
    ],
)
def test_client_rejects_non_loopback_or_ambiguous_configuration(
    base_url: str,
) -> None:
    with pytest.raises(ApiClientError) as error:
        ApiClient(base_url)
    assert error.value.code is ApiClientErrorCode.INVALID_CONFIGURATION


def test_client_builds_ipv4_and_ipv6_urls_and_rejects_invalid_timeout() -> None:
    assert api_base_url(_settings()) == "http://127.0.0.1:8765"
    assert api_base_url(_settings(host="::1")) == "http://[::1]:8765"
    with pytest.raises(ApiClientError) as error:
        ApiClient("http://127.0.0.1:8000", timeout_seconds=0)
    assert error.value.code is ApiClientErrorCode.INVALID_CONFIGURATION


@pytest.mark.parametrize(
    "path",
    ["health", "//example.com/path", "https://example.com", "/health?raw=true", "/x#y"],
)
def test_client_rejects_non_relative_or_ambiguous_paths(path: str) -> None:
    client = ApiClient(
        "http://127.0.0.1:8000",
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={}, request=request)
        ),
    )
    with pytest.raises(ApiClientError) as error:
        client.get(path, ExampleResponse)
    assert error.value.code is ApiClientErrorCode.INVALID_REQUEST_PATH
    client.close()


@pytest.mark.parametrize(
    ("exception_type", "expected_code"),
    [
        (httpx2.ReadTimeout, ApiClientErrorCode.REQUEST_TIMED_OUT),
        (httpx2.ConnectError, ApiClientErrorCode.CONNECTION_FAILED),
    ],
)
def test_client_translates_network_failures_without_private_details(
    exception_type: type[httpx2.RequestError],
    expected_code: ApiClientErrorCode,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exception_type("private local path", request=request)

    client = ApiClient(
        "http://127.0.0.1:8000",
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(ApiClientError) as error:
        client.health()
    assert error.value.code is expected_code
    assert "private local path" not in str(error.value)
    client.close()


@pytest.mark.parametrize("valid_problem", [True, False])
def test_client_translates_api_errors_without_echoing_untrusted_bodies(
    valid_problem: bool,
) -> None:
    body: Any = (
        {
            "code": "profile_not_found",
            "message": "local user profile does not exist",
            "page_numbers": [],
            "validation_issues": [],
        }
        if valid_problem
        else {"private": "SYNTHETIC SECRET DESCRIPTION"}
    )
    client = ApiClient(
        "http://127.0.0.1:8000",
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(404, json=body, request=request)
        ),
    )

    with pytest.raises(ApiClientError) as error:
        client.current_profile()

    assert error.value.code is ApiClientErrorCode.API_REJECTED_REQUEST
    assert error.value.status_code == 404
    assert error.value.problem_code == ("profile_not_found" if valid_problem else None)
    assert "local user profile does not exist" not in str(error.value)
    assert "SYNTHETIC SECRET DESCRIPTION" not in str(error.value)
    client.close()


@pytest.mark.parametrize(
    "body",
    [
        {"status": "wrong", "version": "0.1.0"},
        b"not-json",
    ],
)
def test_client_rejects_invalid_success_responses(body: Any) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if isinstance(body, bytes):
            return httpx2.Response(200, content=body, request=request)
        return httpx2.Response(200, json=body, request=request)

    client = ApiClient(
        "http://127.0.0.1:8000",
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(ApiClientError) as error:
        client.health()
    assert error.value.code is ApiClientErrorCode.INVALID_RESPONSE
    assert error.value.status_code == 200
    client.close()
