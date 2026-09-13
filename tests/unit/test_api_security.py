"""Transport-boundary tests for the local unauthenticated API."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cashflow_ai.api.security import LocalApiSecurityMiddleware, _request_hostname


def _scope(
    *,
    path: str = "/api/v1/workspaces",
    method: str = "POST",
    host: str | None = "localhost:8000",
    content_length: str | None = None,
    scope_type: str = "http",
) -> Scope:
    headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        headers.append((b"host", host.encode("ascii")))
    if content_length is not None:
        headers.append((b"content-length", content_length.encode("ascii")))
    return cast(
        Scope,
        {
            "type": scope_type,
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        },
    )


def _response_status(messages: list[Message]) -> int:
    return cast(int, messages[0]["status"])


def _response_headers(messages: list[Message]) -> Headers:
    return Headers(raw=cast(list[tuple[bytes, bytes]], messages[0]["headers"]))


def _run(
    middleware: LocalApiSecurityMiddleware,
    scope: Scope,
    *,
    body: bytes = b"",
) -> list[Message]:
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[Message] = []

    async def receive() -> Message:
        if messages:
            return cast(Message, messages.pop(0))
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return sent


def _echo_app(received: list[bytes]) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            received.append(b"non-http")
            return
        request = await receive()
        received.append(cast(bytes, request.get("body", b"")))
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [(b"x-synthetic", b"ok")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    return app


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (None, None),
        ("", None),
        ("LOCALHOST.:8000", "localhost"),
        ("[::1]:8000", "::1"),
        ("localhost:invalid", None),
        ("user@localhost", None),
        ("localhost/path", None),
        ("localhost?query", None),
        ("localhost#fragment", None),
    ],
)
def test_request_hostname_accepts_only_plain_host_authority(
    host: str | None,
    expected: str | None,
) -> None:
    assert _request_hostname(_scope(host=host)) == expected


def test_security_middleware_validates_configuration_and_non_http_passthrough() -> None:
    received: list[bytes] = []
    app = _echo_app(received)
    with pytest.raises(ValueError, match="trusted API host"):
        LocalApiSecurityMiddleware(
            app,
            allowed_hosts=(),
            max_workspace_request_bytes=1,
        )
    with pytest.raises(ValueError, match="must be positive"):
        LocalApiSecurityMiddleware(
            app,
            allowed_hosts=("localhost",),
            max_workspace_request_bytes=0,
        )

    middleware = LocalApiSecurityMiddleware(
        app,
        allowed_hosts=("LOCALHOST.",),
        max_workspace_request_bytes=4,
    )
    assert _run(middleware, _scope(scope_type="lifespan")) == []
    assert received == [b"non-http"]


def test_security_middleware_rejects_host_and_invalid_declared_lengths() -> None:
    middleware = LocalApiSecurityMiddleware(
        _echo_app([]),
        allowed_hosts=("localhost",),
        max_workspace_request_bytes=4,
    )

    untrusted = _run(middleware, _scope(host="attacker.example"))
    assert _response_status(untrusted) == 400
    assert _response_headers(untrusted)["cache-control"] == "no-store, max-age=0"

    for declared_length in ("invalid", "-1"):
        invalid = _run(
            middleware,
            _scope(content_length=declared_length),
        )
        assert _response_status(invalid) == 400

    oversized = _run(middleware, _scope(content_length="5"), body=b"x")
    assert _response_status(oversized) == 413


def test_security_middleware_bounds_streams_and_sets_private_cache_headers() -> None:
    received: list[bytes] = []
    middleware = LocalApiSecurityMiddleware(
        _echo_app(received),
        allowed_hosts=("localhost",),
        max_workspace_request_bytes=4,
    )

    accepted = _run(
        middleware,
        _scope(content_length="4"),
        body=b"1234",
    )
    assert _response_status(accepted) == 204
    assert _response_headers(accepted)["cache-control"] == "no-store, max-age=0"
    assert _response_headers(accepted)["pragma"] == "no-cache"
    assert received == [b"1234"]

    streamed = _run(middleware, _scope(), body=b"12345")
    assert _response_status(streamed) == 413

    public = _run(
        middleware,
        _scope(path="/health", method="GET"),
        body=b"",
    )
    assert _response_status(public) == 204
    assert "cache-control" not in _response_headers(public)

    workspace_get = _run(
        middleware,
        _scope(path="/api/v1/workspaces/example", method="GET"),
    )
    assert _response_status(workspace_get) == 204
    assert _response_headers(workspace_get)["cache-control"] == "no-store, max-age=0"


def test_security_middleware_passes_disconnect_messages_to_mutation_handlers() -> None:
    async def disconnect_reader(scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        request = await receive()
        disconnect = await receive()
        assert request["type"] == "http.request"
        assert disconnect["type"] == "http.disconnect"
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = LocalApiSecurityMiddleware(
        disconnect_reader,
        allowed_hosts=("localhost",),
        max_workspace_request_bytes=4,
    )
    response = _run(middleware, _scope(), body=b"1")
    assert _response_status(response) == 204
