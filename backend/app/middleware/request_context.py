"""Request-id propagation, access logging, and HTTP metrics.

Implemented as raw ASGI middleware rather than Starlette's
`BaseHTTPMiddleware`. `BaseHTTPMiddleware` wraps each request in an extra
anyio task group and streams the response through a memory object stream; on a
sub-millisecond redirect endpoint that overhead is a measurable fraction of the
request. Raw ASGI middleware just wraps `send`.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.context import request_id_var, route_var
from app.core.logging import get_logger
from app.core.metrics import (
    http_request_duration_seconds,
    http_requests_in_progress,
    http_requests_total,
    status_class,
)

logger = get_logger("app.access")

REQUEST_ID_HEADER = "x-request-id"

#: Paths whose access logs are dropped. Kubernetes-style probes and Prometheus
#: scrapes would otherwise dominate the log volume (a 15s scrape interval times
#: N replicas) while carrying no diagnostic value.
_QUIET_PATHS = frozenset({"/health", "/ready", "/metrics"})


def _route_template(scope: Scope) -> str:
    """Low-cardinality route label for metrics and logs.

    Uses the matched route's template (`/api/v1/urls/{code}`), and falls back to
    the literal string "unmatched" when nothing matched. The fallback matters:
    the raw path of an unmatched request is attacker-controlled, so labelling
    metrics with it would let anyone create unbounded time series.
    """
    route: Any = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return "unmatched"


class RequestContextMiddleware:
    """Assigns a request id, times the request, logs it, and records metrics."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._instance_id = settings.instance_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Honour an inbound request id so a trace survives the Nginx hop and
        # can be correlated with a browser-side log line.
        incoming = dict(scope.get("headers") or {})
        raw_id = incoming.get(REQUEST_ID_HEADER.encode())
        request_id = (
            raw_id.decode("latin-1")[:64] if raw_id else uuid.uuid4().hex  # bound untrusted input
        )

        id_token = request_id_var.set(request_id)
        method: str = scope.get("method", "GET")
        path: str = scope.get("path", "/")
        started = time.perf_counter()
        status_code = 500

        def _capture(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers.append("X-Request-ID", request_id)
                # Which replica answered. This is how docs/TESTING.md proves
                # that Nginx is actually load balancing.
                headers.append("X-Served-By", self._instance_id)

        async def send_wrapper(message: Message) -> None:
            _capture(message)
            await send(message)

        route_token = route_var.set(None)
        try:
            http_requests_in_progress.inc()
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration = time.perf_counter() - started
            route = _route_template(scope)
            logger.exception(
                "request failed",
                extra={
                    "http_method": method,
                    "route": route,
                    "status_code": 500,
                    "duration_ms": round(duration * 1000, 3),
                },
            )
            self._record(method, route, 500, duration)
            raise
        else:
            duration = time.perf_counter() - started
            route = _route_template(scope)
            route_var.set(route)
            self._record(method, route, status_code, duration)
            if path not in _QUIET_PATHS:
                logger.info(
                    "request completed",
                    extra={
                        "http_method": method,
                        "route": route,
                        "status_code": status_code,
                        "status_class": status_class(status_code),
                        "duration_ms": round(duration * 1000, 3),
                    },
                )
        finally:
            http_requests_in_progress.dec()
            route_var.reset(route_token)
            request_id_var.reset(id_token)

    @staticmethod
    def _record(method: str, route: str, status: int, duration: float) -> None:
        http_requests_total.labels(
            method=method,
            route=route,
            status=str(status),
            status_class=status_class(status),
        ).inc()
        http_request_duration_seconds.labels(method=method, route=route).observe(duration)
