"""Transport safeguards for the unauthenticated loopback API."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

WORKSPACE_PATH_PREFIX = "/api/v1/workspaces"
WORKSPACE_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class _WorkspaceRequestTooLargeError(Exception):
    """Stop a request stream before multipart parsing can grow without bound."""


def _problem(message: str, *, status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "validation_issues": [],
            "page_numbers": [],
        },
    )


def _request_hostname(scope: Scope) -> str | None:
    raw_host = Headers(scope=scope).get("host")
    if raw_host is None:
        return None
    try:
        parsed = urlsplit(f"//{raw_host}")
        hostname = parsed.hostname
        # Accessing port also validates malformed and out-of-range values.
        _ = parsed.port
    except ValueError:
        return None
    if (
        hostname is None
        or parsed.username is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return hostname.rstrip(".").casefold()


class LocalApiSecurityMiddleware:
    """Restrict Host headers, bound workspace bodies, and disable response caching."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: Sequence[str],
        max_workspace_request_bytes: int,
    ) -> None:
        """Configure a strict local Host allowlist and workspace body ceiling."""
        if not allowed_hosts:
            raise ValueError("at least one trusted API host is required")
        if max_workspace_request_bytes < 1:
            raise ValueError("workspace request limit must be positive")
        self._app = app
        self._allowed_hosts = frozenset(
            host.rstrip(".").casefold() for host in allowed_hosts
        )
        self._max_workspace_request_bytes = max_workspace_request_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Apply local-only transport protections to one ASGI exchange."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        workspace_request = path.startswith(WORKSPACE_PATH_PREFIX)

        async def privacy_send(message: Message) -> None:
            if workspace_request and message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["cache-control"] = "no-store, max-age=0"
                headers["pragma"] = "no-cache"
            await send(message)

        if _request_hostname(scope) not in self._allowed_hosts:
            response = _problem(
                "the API accepts requests only for its configured loopback host",
                status_code=400,
                code="invalid_host_header",
            )
            await response(scope, receive, privacy_send)
            return

        mutation = (
            workspace_request
            and str(scope.get("method", "GET")).upper() in WORKSPACE_MUTATION_METHODS
        )
        if not mutation:
            await self._app(scope, receive, privacy_send)
            return

        content_length = Headers(scope=scope).get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = -1
            if declared_length < 0:
                response = _problem(
                    "the workspace request length is invalid",
                    status_code=400,
                    code="invalid_content_length",
                )
                await response(scope, receive, privacy_send)
                return
            if declared_length > self._max_workspace_request_bytes:
                response = _problem(
                    "the workspace request is too large",
                    status_code=413,
                    code="workspace_request_too_large",
                )
                await response(scope, receive, privacy_send)
                return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_workspace_request_bytes:
                    raise _WorkspaceRequestTooLargeError
            return message

        try:
            await self._app(scope, limited_receive, privacy_send)
        except _WorkspaceRequestTooLargeError:
            response = _problem(
                "the workspace request is too large",
                status_code=413,
                code="workspace_request_too_large",
            )
            await response(scope, receive, privacy_send)


__all__ = ["LocalApiSecurityMiddleware"]
