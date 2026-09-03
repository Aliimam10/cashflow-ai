"""Tests for the typed loopback-only Streamlit API client."""

from __future__ import annotations

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
    api_base_url,
)
from cashflow_ai.schemas.api import HealthResponse


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
