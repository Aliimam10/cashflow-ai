"""Thin HTTP routes delegating to existing domain and application services."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Response, UploadFile, status

from cashflow_ai.api.dependencies import (
    EngineDependency,
    OcrEngineFactoryDependency,
    PaginationDependency,
    SessionFactoryDependency,
)
from cashflow_ai.api.services import (
    check_readiness,
    confirm_pdf_statement,
    create_account,
    create_profile,
    get_account,
    get_current_profile,
    get_health,
    get_import_context,
    get_ocr_status,
    get_profile,
    get_transaction,
    list_accounts,
    list_probable_duplicate_reviews,
    list_transactions,
    page_items,
    parse_csv_confirmation_form,
    parse_form_contract,
    prepare_pdf_statement_review,
    preview_ocr_statement,
    preview_text_statement,
    review_probable_duplicate,
    search_transactions,
)
from cashflow_ai.api.uploads import read_bounded_upload
from cashflow_ai.imports import (
    DEFAULT_MAX_CSV_BYTES,
    DEFAULT_MAX_PDF_BYTES,
    persist_confirmed_csv,
    preview_csv,
)
from cashflow_ai.schemas.api import (
    AccountCreate,
    AccountResponse,
    ApiProblem,
    HealthResponse,
    ImportContextResponse,
    OcrStatusResponse,
    Page,
    PdfSourceType,
    ReadinessResponse,
    TransactionResponse,
    TransactionSearchRequest,
    UserProfileCreate,
    UserProfileResponse,
)
from cashflow_ai.schemas.csv_imports import CsvImportSummary, CsvPreview
from cashflow_ai.schemas.duplicates import (
    DuplicateReviewRequest,
    DuplicateReviewResult,
    ProbableDuplicateReviewItem,
)
from cashflow_ai.schemas.ocr_imports import OcrPdfPreview
from cashflow_ai.schemas.pdf_imports import TextPdfPreview
from cashflow_ai.schemas.reconciliation import (
    ApprovedStatement,
    StatementApproval,
    StatementReview,
)
from cashflow_ai.schemas.transactions import Currency

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiProblem, "description": "The source data cannot be processed."},
    404: {"model": ApiProblem, "description": "The requested local record is absent."},
    409: {
        "model": ApiProblem,
        "description": "Explicit review or current state is required.",
    },
    413: {"model": ApiProblem, "description": "The uploaded file exceeds its limit."},
    415: {"model": ApiProblem, "description": "The uploaded file type is unsupported."},
    422: {"model": ApiProblem, "description": "The request contract is invalid."},
    500: {"model": ApiProblem, "description": "A private internal failure occurred."},
    503: {"model": ApiProblem, "description": "A local dependency is unavailable."},
}

router = APIRouter(responses=ERROR_RESPONSES)


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["operations"],
    summary="Check process liveness",
)
def health() -> HealthResponse:
    """Return liveness without checking the database or optional OCR engine."""
    return get_health()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    tags=["operations"],
    summary="Check local database readiness",
)
def readiness(response: Response, engine: EngineDependency) -> ReadinessResponse:
    """Return 503 until the database connection and required schema are ready."""
    result = check_readiness(engine)
    if result.status == "not_ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result


@router.post(
    "/api/v1/profiles",
    response_model=UserProfileResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["profiles"],
    summary="Create the local profile",
)
def create_profile_route(
    request: UserProfileCreate, factory: SessionFactoryDependency
) -> UserProfileResponse:
    """Create the one local profile through the application service."""
    return create_profile(factory, request)


@router.get(
    "/api/v1/profiles/current",
    response_model=UserProfileResponse,
    tags=["profiles"],
    summary="Get the current local profile",
)
def get_current_profile_route(
    factory: SessionFactoryDependency,
) -> UserProfileResponse:
    """Return the configured single-user profile."""
    return get_current_profile(factory)


@router.get(
    "/api/v1/profiles/{profile_id}",
    response_model=UserProfileResponse,
    tags=["profiles"],
    summary="Get a profile by ID",
)
def get_profile_route(
    profile_id: str, factory: SessionFactoryDependency
) -> UserProfileResponse:
    """Return one profile through the application service."""
    return get_profile(factory, profile_id=profile_id)


@router.post(
    "/api/v1/profiles/{profile_id}/accounts",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["accounts"],
    summary="Create an account",
)
def create_account_route(
    profile_id: str,
    request: AccountCreate,
    factory: SessionFactoryDependency,
) -> AccountResponse:
    """Create supported account metadata through the application service."""
    return create_account(factory, profile_id=profile_id, request=request)


@router.get(
    "/api/v1/profiles/{profile_id}/accounts",
    response_model=Page[AccountResponse],
    tags=["accounts"],
    summary="List profile accounts",
)
def list_accounts_route(
    profile_id: str,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[AccountResponse]:
    """Return accounts in a deterministic order."""
    return page_items(list_accounts(factory, profile_id=profile_id), pagination)


@router.get(
    "/api/v1/accounts/{account_id}",
    response_model=AccountResponse,
    tags=["accounts"],
    summary="Get an account",
)
def get_account_route(
    account_id: str, factory: SessionFactoryDependency
) -> AccountResponse:
    """Return account metadata through the application service."""
    return get_account(factory, account_id=account_id)


@router.post(
    "/api/v1/imports/csv/preview",
    response_model=CsvPreview,
    tags=["ingestion"],
    summary="Preview a CSV statement",
)
async def preview_csv_route(
    file: Annotated[UploadFile, File(description="CSV statement to validate")],
) -> CsvPreview:
    """Validate a bounded CSV upload without writing it to disk or the database."""
    filename = file.filename or ""
    content = await read_bounded_upload(file, max_bytes=DEFAULT_MAX_CSV_BYTES)
    return preview_csv(content, filename)


@router.post(
    "/api/v1/imports/csv/confirm",
    response_model=CsvImportSummary,
    tags=["ingestion"],
    summary="Confirm and persist an exact CSV preview",
)
async def confirm_csv_route(
    file: Annotated[
        UploadFile,
        File(description="The exact CSV bytes previously previewed"),
    ],
    plan_json: Annotated[
        str,
        Form(description="JSON-encoded CsvImportPlan contract"),
    ],
    confirmation_json: Annotated[
        str,
        Form(description="JSON-encoded CsvImportConfirmation contract"),
    ],
    factory: SessionFactoryDependency,
) -> CsvImportSummary:
    """Revalidate and atomically persist a user-confirmed CSV statement."""
    plan, confirmation = parse_csv_confirmation_form(plan_json, confirmation_json)
    filename = file.filename or ""
    mime_type = file.content_type or ""
    content = await read_bounded_upload(file, max_bytes=DEFAULT_MAX_CSV_BYTES)
    return persist_confirmed_csv(
        factory,
        content,
        filename,
        mime_type=mime_type,
        plan=plan,
        confirmation=confirmation,
    )


@router.post(
    "/api/v1/imports/pdf/text/preview",
    response_model=TextPdfPreview,
    tags=["ingestion"],
    summary="Preview an embedded-text PDF statement",
)
async def preview_text_pdf_route(
    file: Annotated[UploadFile, File(description="Digital PDF bank statement")],
    account_id: Annotated[str, Form(min_length=1, max_length=255)],
    factory: SessionFactoryDependency,
    account_currency: Annotated[Currency, Form()] = Currency.GBP,
) -> TextPdfPreview:
    """Extract a bounded digital PDF into non-persistent review candidates."""
    filename = file.filename or ""
    mime_type = file.content_type or ""
    content = await read_bounded_upload(file, max_bytes=DEFAULT_MAX_PDF_BYTES)
    return preview_text_statement(
        factory,
        content,
        filename,
        mime_type=mime_type,
        account_id=account_id,
        account_currency=account_currency,
    )


@router.post(
    "/api/v1/imports/pdf/ocr/preview",
    response_model=OcrPdfPreview,
    tags=["ingestion"],
    summary="Preview a scanned PDF with local OCR",
)
async def preview_ocr_pdf_route(
    file: Annotated[UploadFile, File(description="Scanned PDF bank statement")],
    account_id: Annotated[str, Form(min_length=1, max_length=255)],
    ocr_engine_factory: OcrEngineFactoryDependency,
    factory: SessionFactoryDependency,
    account_currency: Annotated[Currency, Form()] = Currency.GBP,
) -> OcrPdfPreview:
    """Run bounded local OCR and return review-only candidates and confidence."""
    filename = file.filename or ""
    mime_type = file.content_type or ""
    content = await read_bounded_upload(file, max_bytes=DEFAULT_MAX_PDF_BYTES)
    return preview_ocr_statement(
        factory,
        content,
        filename,
        mime_type=mime_type,
        account_id=account_id,
        account_currency=account_currency,
        engine_factory=ocr_engine_factory,
    )


@router.get(
    "/api/v1/ocr/status",
    response_model=OcrStatusResponse,
    tags=["ingestion"],
    summary="Check optional local OCR availability",
)
def ocr_status_route(
    ocr_engine_factory: OcrEngineFactoryDependency,
) -> OcrStatusResponse:
    """Report Tesseract availability without exposing subprocess details."""
    return get_ocr_status(ocr_engine_factory)


@router.post(
    "/api/v1/imports/pdf/review",
    response_model=StatementReview,
    tags=["ingestion"],
    summary="Prepare a targeted PDF review",
)
async def prepare_pdf_review_route(
    file: Annotated[
        UploadFile,
        File(description="The exact PDF bytes to prepare for review"),
    ],
    source_type: Annotated[PdfSourceType, Form()],
    account_id: Annotated[str, Form(min_length=1, max_length=255)],
    ocr_engine_factory: OcrEngineFactoryDependency,
    factory: SessionFactoryDependency,
    account_currency: Annotated[Currency, Form()] = Currency.GBP,
    ocr_confidence_threshold: Annotated[float, Form(gt=0, le=1)] = 0.85,
) -> StatementReview:
    """Re-extract exact bytes into explicit row, date, sign, and balance decisions."""
    filename = file.filename or ""
    mime_type = file.content_type or ""
    content = await read_bounded_upload(file, max_bytes=DEFAULT_MAX_PDF_BYTES)
    return prepare_pdf_statement_review(
        factory,
        content,
        filename,
        mime_type=mime_type,
        source_type=source_type,
        account_id=account_id,
        account_currency=account_currency,
        ocr_confidence_threshold=ocr_confidence_threshold,
        engine_factory=ocr_engine_factory,
    )


@router.post(
    "/api/v1/imports/pdf/confirm",
    response_model=ApprovedStatement,
    tags=["ingestion"],
    summary="Confirm a reviewed PDF statement in memory",
)
async def confirm_pdf_review_route(
    file: Annotated[
        UploadFile,
        File(description="The exact PDF bytes previously reviewed"),
    ],
    source_type: Annotated[PdfSourceType, Form()],
    account_id: Annotated[str, Form(min_length=1, max_length=255)],
    approval_json: Annotated[
        str,
        Form(description="JSON-encoded StatementApproval contract"),
    ],
    ocr_engine_factory: OcrEngineFactoryDependency,
    factory: SessionFactoryDependency,
    account_currency: Annotated[Currency, Form()] = Currency.GBP,
    ocr_confidence_threshold: Annotated[float, Form(gt=0, le=1)] = 0.85,
) -> ApprovedStatement:
    """Re-extract exact bytes and apply approval without PDF persistence."""
    approval = parse_form_contract(approval_json, StatementApproval)
    filename = file.filename or ""
    mime_type = file.content_type or ""
    content = await read_bounded_upload(file, max_bytes=DEFAULT_MAX_PDF_BYTES)
    return confirm_pdf_statement(
        factory,
        content,
        filename,
        mime_type=mime_type,
        source_type=source_type,
        account_id=account_id,
        account_currency=account_currency,
        ocr_confidence_threshold=ocr_confidence_threshold,
        engine_factory=ocr_engine_factory,
        approval=approval,
    )


@router.get(
    "/api/v1/imports/{import_batch_id}/context",
    response_model=ImportContextResponse,
    tags=["ingestion"],
    summary="Get stored import context",
)
def get_import_context_route(
    import_batch_id: str, factory: SessionFactoryDependency
) -> ImportContextResponse:
    """Return inert notes, flags, coverage, and reported balances for an import."""
    return get_import_context(factory, import_batch_id=import_batch_id)


@router.get(
    "/api/v1/accounts/{account_id}/transactions",
    response_model=Page[TransactionResponse],
    tags=["transactions"],
    summary="List verified account transactions",
)
def list_transactions_route(
    account_id: str,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[TransactionResponse]:
    """Return verified transactions while excluding raw source payloads."""
    return page_items(list_transactions(factory, account_id=account_id), pagination)


@router.post(
    "/api/v1/transactions/search",
    response_model=Page[TransactionResponse],
    tags=["transactions"],
    summary="Search verified profile transactions",
)
def search_transactions_route(
    request: TransactionSearchRequest,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[TransactionResponse]:
    """Apply profile-owned filters before returning one bounded result page."""
    return page_items(search_transactions(factory, request), pagination)


@router.get(
    "/api/v1/profiles/{profile_id}/duplicates/reviews",
    response_model=Page[ProbableDuplicateReviewItem],
    tags=["duplicates"],
    summary="List probable duplicate reviews",
)
def probable_duplicate_reviews_route(
    profile_id: str,
    factory: SessionFactoryDependency,
    pagination: PaginationDependency,
) -> Page[ProbableDuplicateReviewItem]:
    """Return unresolved probable rows without their complete raw payloads."""
    return page_items(
        list_probable_duplicate_reviews(factory, user_profile_id=profile_id),
        pagination,
    )


@router.post(
    "/api/v1/profiles/{profile_id}/duplicates/{raw_transaction_id}/review",
    response_model=DuplicateReviewResult,
    tags=["duplicates"],
    summary="Resolve a probable duplicate",
)
def probable_duplicate_review_route(
    profile_id: str,
    raw_transaction_id: str,
    request: DuplicateReviewRequest,
    factory: SessionFactoryDependency,
) -> DuplicateReviewResult:
    """Keep or reject one raw candidate through the atomic review service."""
    return review_probable_duplicate(
        factory,
        user_profile_id=profile_id,
        raw_transaction_id=raw_transaction_id,
        request=request,
    )


@router.get(
    "/api/v1/transactions/{transaction_id}",
    response_model=TransactionResponse,
    tags=["transactions"],
    summary="Get a verified transaction",
)
def get_transaction_route(
    transaction_id: str, factory: SessionFactoryDependency
) -> TransactionResponse:
    """Return one verified transaction without its raw import record."""
    return get_transaction(factory, transaction_id=transaction_id)


__all__ = ["ERROR_RESPONSES", "router"]
