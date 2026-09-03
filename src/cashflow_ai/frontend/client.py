"""Typed, privacy-safe client for the local CashFlow AI HTTP API."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import TracebackType
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel

from cashflow_ai.config import Settings
from cashflow_ai.schemas.api import (
    ApiProblem,
    HealthResponse,
    ReadinessResponse,
    UserProfileResponse,
)


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
        accepted_statuses: frozenset[int] = frozenset(),
    ) -> ResponseT:
        _validate_path(path)
        try:
            response = self._client.request(
                method,
                path,
                params=params,
                json=None if body is None else body.model_dump(mode="json"),
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


__all__ = [
    "ApiClient",
    "ApiClientError",
    "ApiClientErrorCode",
    "api_base_url",
]
