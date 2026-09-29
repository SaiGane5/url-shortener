"""Security headers and request-body size limiting."""

from __future__ import annotations

import json

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.context import get_request_id
from app.core.errors import PayloadTooLargeError, build_error_body
from app.core.logging import get_logger
from app.core.metrics import http_request_body_bytes_rejected_total

logger = get_logger(__name__)

#: Applied to every response, including redirects and errors.
#:
#: `Content-Security-Policy: default-src 'none'` is safe and strict here because
#: the API only ever returns JSON and 3xx redirects -- it never returns a
#: document that needs to load resources. The React app is served by its own
#: Nginx with its own (necessarily looser) policy.
_BASE_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}

#: Swagger UI / ReDoc are real HTML documents that load JS and CSS from a CDN,
#: so they need their own policy. Kept narrow: scripts and styles from jsDelivr
#: only, images inline/data, nothing else.
_DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)

_DOCS_PATHS = frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"})


class SecurityHeadersMiddleware:
    """Adds hardening headers to every response."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._settings = settings
        # HSTS is only meaningful over TLS, and setting it in local HTTP dev
        # would poison the browser's cache for localhost across projects.
        self._hsts = settings.is_production

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_docs = scope.get("path", "") in _DOCS_PATHS

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _BASE_HEADERS.items():
                    if name == "Content-Security-Policy" and is_docs:
                        headers.setdefault(name, _DOCS_CSP)
                    else:
                        headers.setdefault(name, value)
                if self._hsts:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodySizeLimitMiddleware:
    """Rejects oversized request bodies before they are buffered.

    Checks `Content-Length` up front (cheap, catches honest clients) *and*
    counts bytes as they stream in (catches chunked uploads that omit or lie
    about the header). Without the streaming check, a chunked request could
    push unbounded data into memory before Pydantic ever sees it.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._max_bytes = settings.max_request_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or {})
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send)
                return

        received = 0
        response_started = False

        async def receive_wrapper() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    # Stop buffering immediately. Normally this is caught by the
                    # app's exception handler and turned into a 413; the guard
                    # below covers the case where it escapes instead.
                    raise PayloadTooLargeError
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except PayloadTooLargeError:
            if response_started:
                raise
            await self._reject(send)

    async def _reject(self, send: Send) -> None:
        http_request_body_bytes_rejected_total.inc()
        logger.warning("request body rejected", extra={"max_bytes": self._max_bytes})
        error = PayloadTooLargeError(f"Request body must be at most {self._max_bytes} bytes.")
        body = json.dumps(
            build_error_body(
                error_code=error.error_code,
                message=error.message,
                request_id=get_request_id(),
            )
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": error.status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
