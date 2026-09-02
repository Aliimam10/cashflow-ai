"""Privacy-safe exception translation for the local HTTP API."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from cashflow_ai.api.services import ApiServiceError, ApiServiceErrorCode
from cashflow_ai.imports import (
    CsvImportError,
    CsvImportErrorCode,
    PdfImportError,
    PdfImportErrorCode,
    StatementReviewError,
    StatementReviewErrorCode,
)
from cashflow_ai.schemas.api import ApiProblem, ApiValidationIssue


def _response(status_code: int, problem: ApiProblem) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json"),
    )


def _api_service_status(code: ApiServiceErrorCode) -> int:
    if code in {
        ApiServiceErrorCode.PROFILE_NOT_FOUND,
        ApiServiceErrorCode.ACCOUNT_NOT_FOUND,
        ApiServiceErrorCode.TRANSACTION_NOT_FOUND,
        ApiServiceErrorCode.IMPORT_NOT_FOUND,
    }:
        return 404
    if code is ApiServiceErrorCode.INVALID_FORM_JSON:
        return 422
    return 409


def _csv_status(code: CsvImportErrorCode) -> int:
    if code is CsvImportErrorCode.FILE_TOO_LARGE:
        return 413
    if code in {
        CsvImportErrorCode.UNSUPPORTED_FILE_TYPE,
        CsvImportErrorCode.UNSUPPORTED_MIME_TYPE,
    }:
        return 415
    if code in {
        CsvImportErrorCode.CONFIRMATION_REQUIRED,
        CsvImportErrorCode.PREVIEW_CHANGED,
    }:
        return 409
    return 400


def _pdf_status(code: PdfImportErrorCode) -> int:
    if code is PdfImportErrorCode.FILE_TOO_LARGE:
        return 413
    if code in {
        PdfImportErrorCode.UNSUPPORTED_FILE_TYPE,
        PdfImportErrorCode.UNSUPPORTED_MIME_TYPE,
    }:
        return 415
    if code in {
        PdfImportErrorCode.OCR_REQUIRED,
        PdfImportErrorCode.OCR_ENGINE_UNAVAILABLE,
    }:
        return 409
    return 400


def _review_status(code: StatementReviewErrorCode) -> int:
    del code
    return 409


def register_exception_handlers(app: FastAPI) -> None:
    """Register stable handlers that never echo request bodies or tracebacks."""

    @app.exception_handler(ApiServiceError)
    async def api_service_error(
        request: Request, error: ApiServiceError
    ) -> JSONResponse:
        del request
        return _response(
            _api_service_status(error.code),
            ApiProblem(code=error.code.value, message=str(error)),
        )

    @app.exception_handler(CsvImportError)
    async def csv_import_error(request: Request, error: CsvImportError) -> JSONResponse:
        del request
        return _response(
            _csv_status(error.code),
            ApiProblem(code=error.code.value, message=str(error)),
        )

    @app.exception_handler(PdfImportError)
    async def pdf_import_error(request: Request, error: PdfImportError) -> JSONResponse:
        del request
        return _response(
            _pdf_status(error.code),
            ApiProblem(
                code=error.code.value,
                message=str(error),
                page_numbers=error.page_numbers,
            ),
        )

    @app.exception_handler(StatementReviewError)
    async def statement_review_error(
        request: Request, error: StatementReviewError
    ) -> JSONResponse:
        del request
        return _response(
            _review_status(error.code),
            ApiProblem(code=error.code.value, message=str(error)),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        del request
        issues = tuple(
            ApiValidationIssue(
                location=tuple(str(item) for item in detail.get("loc", ())),
                error_type=str(detail.get("type", "value_error")),
                message=str(detail.get("msg", "invalid request value"))[:500],
            )
            for detail in error.errors()
        )
        return _response(
            422,
            ApiProblem(
                code="request_validation_failed",
                message="the request does not match the documented API contract",
                validation_issues=issues,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        del request
        return _response(
            error.status_code,
            ApiProblem(code="http_error", message="the HTTP request cannot be served"),
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
        del request, error
        return _response(
            503,
            ApiProblem(
                code="database_unavailable",
                message="the local database is temporarily unavailable",
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        del request, error
        return _response(
            500,
            ApiProblem(
                code="internal_error",
                message="an unexpected internal error occurred",
            ),
        )


__all__ = ["register_exception_handlers"]
