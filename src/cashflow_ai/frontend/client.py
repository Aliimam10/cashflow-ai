"""Typed, privacy-safe client for the local CashFlow AI HTTP API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from urllib.parse import quote, urlsplit

import httpx2
from pydantic import BaseModel

from cashflow_ai.config import Settings
from cashflow_ai.schemas.analytics import AnalyticsScope, CashFlowAnalytics
from cashflow_ai.schemas.api import (
    AccountCreate,
    AccountResponse,
    ApiProblem,
    HealthResponse,
    OcrStatusResponse,
    Page,
    PdfSourceType,
    ReadinessResponse,
    TransactionResponse,
    TransactionSearchRequest,
    UserProfileCreate,
    UserProfileResponse,
)
from cashflow_ai.schemas.api_decisions import (
    BalanceForecastRequest,
    FinancialDataFreshnessRequest,
    FinancialRoleSuggestionRequest,
    ForecastEvaluationRequest,
    RecurrenceDetectionRequest,
    RoleDecisionRequest,
    TransactionRoleReviewRequest,
)
from cashflow_ai.schemas.categories import CategorySummary
from cashflow_ai.schemas.csv_imports import (
    CsvImportConfirmation,
    CsvImportPlan,
    CsvImportSummary,
    CsvPreview,
)
from cashflow_ai.schemas.duplicates import (
    DuplicateReviewRequest,
    DuplicateReviewResult,
    ProbableDuplicateReviewItem,
)
from cashflow_ai.schemas.financial_roles import (
    FinancialRoleSuggestion,
    RoleDecisionResult,
    RoleReviewItem,
)
from cashflow_ai.schemas.forecast_models import ForecastTrainingResult
from cashflow_ai.schemas.forecast_paths import BalanceForecastPath
from cashflow_ai.schemas.freshness import FinancialDataFreshness
from cashflow_ai.schemas.hybrid_categorisation import (
    CategoryFeedback,
    CategoryFeedbackResult,
)
from cashflow_ai.schemas.reconciliation import (
    ApprovedStatement,
    StatementApproval,
    StatementReview,
)
from cashflow_ai.schemas.recurrence import (
    RecurrenceReview,
    RecurrenceReviewResult,
    RecurringPaymentCandidate,
)
from cashflow_ai.schemas.transactions import Currency


class ApiClientErrorCode(StrEnum):
    """Stable frontend failures that never contain response bodies or URLs."""

    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_REQUEST_PATH = "invalid_request_path"
    CONNECTION_FAILED = "connection_failed"
    REQUEST_TIMED_OUT = "request_timed_out"
    API_REJECTED_REQUEST = "api_rejected_request"
    INVALID_RESPONSE = "invalid_response"


class ApiClientError(RuntimeError):
    """Safe failure suitable for direct display by common UI components."""

    def __init__(
        self,
        code: ApiClientErrorCode,
        message: str,
        *,
        status_code: int | None = None,
        problem_code: str | None = None,
    ) -> None:
        """Retain only controlled display metadata."""
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.problem_code = problem_code


@dataclass(frozen=True, slots=True, repr=False)
class UploadedDocument:
    """Ephemeral upload bytes that must never be placed in session state or logs."""

    filename: str
    content: bytes
    mime_type: str


def api_base_url(settings: Settings) -> str:
    """Build an HTTP loopback URL from validated application settings."""
    host = f"[{settings.api_host}]" if ":" in settings.api_host else settings.api_host
    return f"http://{host}:{settings.api_port}"


def _normalise_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ApiClientError(
            ApiClientErrorCode.INVALID_CONFIGURATION,
            "the local API address is invalid",
        ) from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or port < 1
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ApiClientError(
            ApiClientErrorCode.INVALID_CONFIGURATION,
            "the frontend can connect only to an explicit local HTTP API port",
        )
    return value.rstrip("/")


def _validate_path(path: str) -> None:
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ApiClientError(
            ApiClientErrorCode.INVALID_REQUEST_PATH,
            "the API request path must be local and relative",
        )


def _path_segment(value: str) -> str:
    if not value or len(value) > 255:
        raise ApiClientError(
            ApiClientErrorCode.INVALID_REQUEST_PATH,
            "the API record identifier is invalid",
        )
    return quote(value, safe="")


class ApiClient:
    """Synchronous typed client for the loopback-only Version 1 API."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        """Create one reusable client without environment proxy inheritance."""
        if timeout_seconds <= 0:
            raise ApiClientError(
                ApiClientErrorCode.INVALID_CONFIGURATION,
                "the local API timeout must be greater than zero",
            )
        self._client = httpx2.Client(
            base_url=_normalise_base_url(base_url),
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
        )

    def __enter__(self) -> ApiClient:
        """Return this client for a bounded connection lifecycle."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close connections when leaving a bounded client lifecycle."""
        del exception_type, exception, traceback
        self.close()

    def close(self) -> None:
        """Release pooled local connections."""
        self._client.close()

    def _request[ResponseT: BaseModel](
        self,
        method: str,
        path: str,
        response_model: type[ResponseT],
        *,
        params: Mapping[str, str | int] | None = None,
        body: BaseModel | None = None,
        form: Mapping[str, str] | None = None,
        document: UploadedDocument | None = None,
        request_timeout_seconds: float | None = None,
        accepted_statuses: frozenset[int] = frozenset(),
    ) -> ResponseT:
        _validate_path(path)
        files = (
            None
            if document is None
            else {
                "file": (
                    document.filename,
                    document.content,
                    document.mime_type,
                )
            }
        )
        try:
            response = self._client.request(
                method,
                path,
                params=params,
                json=None if body is None else body.model_dump(mode="json"),
                data=form,
                files=files,
                timeout=(
                    httpx2.USE_CLIENT_DEFAULT
                    if request_timeout_seconds is None
                    else request_timeout_seconds
                ),
            )
        except httpx2.TimeoutException as error:
            raise ApiClientError(
                ApiClientErrorCode.REQUEST_TIMED_OUT,
                "the local API did not respond in time",
            ) from error
        except httpx2.RequestError as error:
            raise ApiClientError(
                ApiClientErrorCode.CONNECTION_FAILED,
                "the local API is unavailable; start it and try again",
            ) from error

        if response.is_error and response.status_code not in accepted_statuses:
            try:
                problem = ApiProblem.model_validate(response.json())
            except (TypeError, ValueError):
                problem = None
            raise ApiClientError(
                ApiClientErrorCode.API_REJECTED_REQUEST,
                "the local API rejected the request",
                status_code=response.status_code,
                problem_code=None if problem is None else problem.code,
            )
        try:
            return response_model.model_validate(response.json())
        except (TypeError, ValueError) as error:
            raise ApiClientError(
                ApiClientErrorCode.INVALID_RESPONSE,
                "the local API returned an unexpected response",
                status_code=response.status_code,
            ) from error

    def get[ResponseT: BaseModel](
        self,
        path: str,
        response_model: type[ResponseT],
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> ResponseT:
        """Perform one typed local GET request."""
        return self._request("GET", path, response_model, params=params)

    def post[ResponseT: BaseModel, RequestT: BaseModel](
        self,
        path: str,
        request: RequestT,
        response_model: type[ResponseT],
    ) -> ResponseT:
        """Perform one typed JSON POST request."""
        return self._request("POST", path, response_model, body=request)

    def health(self) -> HealthResponse:
        """Read process liveness."""
        return self.get("/health", HealthResponse)

    def readiness(self) -> ReadinessResponse:
        """Read local database readiness."""
        return self._request(
            "GET",
            "/ready",
            ReadinessResponse,
            accepted_statuses=frozenset({503}),
        )

    def current_profile(self) -> UserProfileResponse:
        """Read the current local profile without exposing raw financial data."""
        return self.get("/api/v1/profiles/current", UserProfileResponse)

    def create_profile(self, request: UserProfileCreate) -> UserProfileResponse:
        """Create the single local profile."""
        return self.post("/api/v1/profiles", request, UserProfileResponse)

    def list_accounts(self, profile_id: str) -> Page[AccountResponse]:
        """List local accounts owned by one profile."""
        return self.get(
            f"/api/v1/profiles/{_path_segment(profile_id)}/accounts",
            Page[AccountResponse],
            params={"limit": 100, "offset": 0},
        )

    def create_account(
        self,
        profile_id: str,
        request: AccountCreate,
    ) -> AccountResponse:
        """Create current/checking or savings account metadata."""
        return self.post(
            f"/api/v1/profiles/{_path_segment(profile_id)}/accounts",
            request,
            AccountResponse,
        )

    def ocr_status(self) -> OcrStatusResponse:
        """Report whether local Tesseract OCR is available."""
        return self.get("/api/v1/ocr/status", OcrStatusResponse)

    def preview_csv(self, document: UploadedDocument) -> CsvPreview:
        """Return a bounded structural preview without persistence."""
        return self._request(
            "POST",
            "/api/v1/imports/csv/preview",
            CsvPreview,
            document=document,
            request_timeout_seconds=30.0,
        )

    def confirm_csv(
        self,
        document: UploadedDocument,
        *,
        plan: CsvImportPlan,
        confirmation: CsvImportConfirmation,
    ) -> CsvImportSummary:
        """Re-send exact bytes with explicit mapping and confirmation."""
        return self._request(
            "POST",
            "/api/v1/imports/csv/confirm",
            CsvImportSummary,
            form={
                "plan_json": plan.model_dump_json(),
                "confirmation_json": confirmation.model_dump_json(),
            },
            document=document,
            request_timeout_seconds=30.0,
        )

    def prepare_pdf_review(
        self,
        document: UploadedDocument,
        *,
        source_type: PdfSourceType,
        account_id: str,
        account_currency: Currency,
        ocr_confidence_threshold: float,
    ) -> StatementReview:
        """Re-extract one PDF into a targeted, non-persistent review."""
        return self._request(
            "POST",
            "/api/v1/imports/pdf/review",
            StatementReview,
            form={
                "source_type": source_type.value,
                "account_id": account_id,
                "account_currency": account_currency.value,
                "ocr_confidence_threshold": str(ocr_confidence_threshold),
            },
            document=document,
            request_timeout_seconds=120.0,
        )

    def confirm_pdf(
        self,
        document: UploadedDocument,
        *,
        source_type: PdfSourceType,
        account_id: str,
        account_currency: Currency,
        ocr_confidence_threshold: float,
        approval: StatementApproval,
    ) -> ApprovedStatement:
        """Apply explicit approval after server-side re-extraction."""
        return self._request(
            "POST",
            "/api/v1/imports/pdf/confirm",
            ApprovedStatement,
            form={
                "source_type": source_type.value,
                "account_id": account_id,
                "account_currency": account_currency.value,
                "ocr_confidence_threshold": str(ocr_confidence_threshold),
                "approval_json": approval.model_dump_json(),
            },
            document=document,
            request_timeout_seconds=120.0,
        )

    def search_transactions(
        self,
        request: TransactionSearchRequest,
    ) -> Page[TransactionResponse]:
        """Search verified rows owned by the selected local profile."""
        return self._request(
            "POST",
            "/api/v1/transactions/search",
            Page[TransactionResponse],
            params={"limit": 100, "offset": 0},
            body=request,
        )

    def list_categories(self) -> Page[CategorySummary]:
        """List the local versioned category taxonomy."""
        return self.get(
            "/api/v1/categories",
            Page[CategorySummary],
            params={"limit": 100, "offset": 0},
        )

    def correct_category(self, request: CategoryFeedback) -> CategoryFeedbackResult:
        """Record one explicit transaction-only category correction."""
        return self.post(
            "/api/v1/categorisation/feedback",
            request,
            CategoryFeedbackResult,
        )

    def generate_role_suggestions(
        self,
        request: FinancialRoleSuggestionRequest,
    ) -> Page[FinancialRoleSuggestion]:
        """Generate advisory transfer, refund, and reimbursement suggestions."""
        return self._request(
            "POST",
            "/api/v1/financial-roles/suggestions",
            Page[FinancialRoleSuggestion],
            params={"limit": 100, "offset": 0},
            body=request,
        )

    def list_role_reviews(self, profile_id: str) -> Page[RoleReviewItem]:
        """List pending financial-role suggestions with review context."""
        return self.get(
            f"/api/v1/profiles/{_path_segment(profile_id)}/financial-roles/reviews",
            Page[RoleReviewItem],
            params={"limit": 100, "offset": 0},
        )

    def decide_role_suggestion(
        self,
        suggestion_id: str,
        request: RoleDecisionRequest,
        *,
        confirm: bool,
    ) -> RoleDecisionResult:
        """Confirm or reject one advisory financial-role suggestion."""
        action = "confirm" if confirm else "reject"
        return self.post(
            f"/api/v1/financial-role-suggestions/"
            f"{_path_segment(suggestion_id)}/{action}",
            request,
            RoleDecisionResult,
        )

    def correct_financial_role(
        self,
        transaction_id: str,
        request: TransactionRoleReviewRequest,
    ) -> RoleDecisionResult:
        """Apply one explicit financial-role correction."""
        return self.post(
            f"/api/v1/transactions/{_path_segment(transaction_id)}/financial-role",
            request,
            RoleDecisionResult,
        )

    def list_duplicate_reviews(
        self,
        profile_id: str,
    ) -> Page[ProbableDuplicateReviewItem]:
        """List unresolved probable duplicates without complete raw payloads."""
        return self.get(
            f"/api/v1/profiles/{_path_segment(profile_id)}/duplicates/reviews",
            Page[ProbableDuplicateReviewItem],
            params={"limit": 100, "offset": 0},
        )

    def decide_duplicate(
        self,
        profile_id: str,
        raw_transaction_id: str,
        request: DuplicateReviewRequest,
    ) -> DuplicateReviewResult:
        """Keep or reject one probable raw transaction candidate."""
        return self.post(
            f"/api/v1/profiles/{_path_segment(profile_id)}/duplicates/"
            f"{_path_segment(raw_transaction_id)}/review",
            request,
            DuplicateReviewResult,
        )

    def cash_flow(self, request: AnalyticsScope) -> CashFlowAnalytics:
        """Calculate coverage-aware analytics for an explicit account period."""
        return self.post("/api/v1/analytics/cash-flow", request, CashFlowAnalytics)

    def freshness(
        self,
        request: FinancialDataFreshnessRequest,
    ) -> FinancialDataFreshness:
        """Assess account evidence freshness using caller-visible limits."""
        return self.post(
            "/api/v1/coverage/freshness",
            request,
            FinancialDataFreshness,
        )

    def detect_recurring(
        self,
        request: RecurrenceDetectionRequest,
    ) -> Page[RecurringPaymentCandidate]:
        """Refresh point-in-time recurring candidates for explicit user review."""
        return self._request(
            "POST",
            "/api/v1/recurring/detect",
            Page[RecurringPaymentCandidate],
            params={"limit": 100, "offset": 0},
            body=request,
        )

    def list_recurring(self, profile_id: str) -> Page[RecurringPaymentCandidate]:
        """List persisted recurring review state without rerunning detection."""
        return self.get(
            f"/api/v1/profiles/{_path_segment(profile_id)}/recurring",
            Page[RecurringPaymentCandidate],
            params={"limit": 100, "offset": 0},
        )

    def review_recurring(
        self,
        request: RecurrenceReview,
    ) -> RecurrenceReviewResult:
        """Confirm or cancel one detected recurring candidate."""
        return self.post(
            "/api/v1/recurring/reviews",
            request,
            RecurrenceReviewResult,
        )

    def balance_forecast(self, request: BalanceForecastRequest) -> BalanceForecastPath:
        """Calculate one uncertainty-aware future balance path locally."""
        return self.post(
            "/api/v1/forecasts/balance",
            request,
            BalanceForecastPath,
        )

    def evaluate_forecast(
        self, request: ForecastEvaluationRequest
    ) -> ForecastTrainingResult:
        """Return chronological candidate-versus-baseline evaluation evidence."""
        return self.post(
            "/api/v1/forecasts/evaluate",
            request,
            ForecastTrainingResult,
        )


__all__ = [
    "ApiClient",
    "ApiClientError",
    "ApiClientErrorCode",
    "UploadedDocument",
    "api_base_url",
]
