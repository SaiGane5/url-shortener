"""Rate-limiting middleware.

Middleware rather than a per-route dependency, for two reasons:

1. Load shedding should happen before FastAPI does any request work -- body
   parsing, validation, dependency resolution. A dependency runs *after* some of
   that; middleware runs before all of it.
2. It cannot be forgotten. A new route is rate limited by default, which is the
   right direction for a limiter to fail.

The cost is that the matched route template is not known yet, so the policy is
selected from method + path prefix (see `_select_policy`). That mapping is
small, explicit, and tested.
"""

from __future__ import annotations

import json

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.context import get_request_id
from app.core.errors import (
    DependencyUnavailableError,
    RateLimitExceededError,
    build_error_body,
)
from app.core.logging import get_logger
from app.ratelimit.limiter import RateLimiter
from app.ratelimit.policy import PolicyName

logger = get_logger(__name__)

#: Never rate limited. Probes and metrics scrapes come from the orchestrator and
#: from Prometheus; throttling them would make the platform blind exactly when
#: something is wrong. They are reachable only from inside the compose network
#: (see nginx/nginx.conf, which restricts /metrics).
_EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/ready",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/docs/oauth2-redirect",
        "/favicon.ico",
    }
)

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RateLimitMiddleware:
    """Applies the distributed token-bucket limiter to inbound requests."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter, settings: Settings) -> None:
        self.app = app
        self._limiter = limiter
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "/")
        method: str = scope.get("method", "GET")

        if path in _EXEMPT_PATHS or method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        policy_name = _select_policy(method, path)
        identity = client_identity(scope, self._settings)
        decision = await self._limiter.check(identity, policy_name)

        if decision.allowed:
            # Surface the budget on successful responses too, so a client can
            # back off before it gets rejected.
            await self._app_with_headers(scope, receive, send, decision.headers())
            return

        if decision.degraded and not decision.policy.fail_open:
            # Fail-closed path: be explicit that this is a dependency failure
            # (503), not the client exceeding a quota (429).
            error: RateLimitExceededError | DependencyUnavailableError = DependencyUnavailableError(
                "redis",
                "Rate limiting is temporarily unavailable, so write requests are being rejected.",
            )
        else:
            error = RateLimitExceededError(details={"policy": decision.policy.name.value})

        logger.warning(
            "request rejected by rate limiter",
            extra={
                "policy": decision.policy.name.value,
                "route_class": policy_name.value,
                "rate_limit_bucket": decision.bucket,
                "degraded": decision.degraded,
                "status_code": error.status_code,
            },
        )
        await _send_error(send, error, {**decision.headers(), **error.headers})

    async def _app_with_headers(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                mutable = MutableHeaders(scope=message)
                for key, value in headers.items():
                    mutable[key] = value
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _select_policy(method: str, path: str) -> PolicyName:
    """Map a request to a rate-limit policy class."""
    if path.startswith("/api/"):
        return PolicyName.WRITE if method in _WRITE_METHODS else PolicyName.READ
    # Everything else that reaches the API is a short-code redirect.
    return PolicyName.REDIRECT


def client_identity(scope: Scope, settings: Settings) -> str:
    """Determine the rate-limit identity (the client IP) for a request.

    Behind Nginx, `request.client` is the proxy, so the real client IP comes
    from `X-Forwarded-For`. That header is client-writable, so we must not
    simply take the first entry: a client can prepend anything it likes and mint
    a fresh bucket per request.

    Nginx appends the peer address to whatever the client sent, so the last
    entry is always trustworthy, the second-to-last is trustworthy if there are
    two proxies, and so on. We therefore index `TRUSTED_PROXY_HOPS` entries in
    from the right.
    """
    headers = Headers(scope=scope)
    forwarded = headers.get(settings.client_ip_header.lower())
    hops = settings.trusted_proxy_hops

    if forwarded and hops > 0:
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if chain:
            index = max(len(chain) - hops, 0)
            return chain[index]

    client = scope.get("client")
    if client:
        return str(client[0])
    return "unknown"


async def _send_error(
    send: Send,
    error: RateLimitExceededError | DependencyUnavailableError,
    headers: dict[str, str],
) -> None:
    body = json.dumps(
        build_error_body(
            error_code=error.error_code,
            message=error.message,
            request_id=get_request_id(),
            details=error.details,
        )
    ).encode()
    encoded_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    encoded_headers += [
        (key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in headers.items()
    ]
    await send(
        {
            "type": "http.response.start",
            "status": error.status_code,
            "headers": encoded_headers,
        }
    )
    await send({"type": "http.response.body", "body": body})
