"""FastAPI application factory.

Startup order matters and is explicit: build config, install logging, create the
Redis pool and database engine, then the services that depend on them, then
start background work. Shutdown reverses it and drains the click buffer so a
rolling deploy does not throw away counted clicks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from app import __version__
from app.api import routes_redirect, routes_system
from app.api.v1 import routes_urls
from app.cache.redis_client import RedisGateway
from app.cache.url_cache import CACHE_NAME, UrlCache
from app.core.config import Settings, get_settings
from app.core.context import get_request_id
from app.core.errors import AppError, build_error_body
from app.core.logging import configure_logging, get_logger
from app.core.metrics import app_info, postgres_errors_total, seed_counters
from app.db.session import Database
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.security import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.ratelimit.limiter import RateLimiter
from app.services.click_tracker import ClickTracker
from app.services.url_service import UrlService

logger = get_logger(__name__)

API_DESCRIPTION = """
A distributed URL shortener built to demonstrate production backend and
distributed-systems practice rather than a minimal demo.

### How a request flows
`Nginx (load balancer) -> one of N stateless FastAPI replicas -> Redis -> PostgreSQL`

### What is actually distributed
* **Rate limiting** -- a Redis token bucket evaluated inside a Lua script, so
  the limit is global across replicas rather than per process. The refill clock
  is Redis's own clock, so replica clock skew cannot leak tokens.
* **Caching** -- cache-aside on the redirect path, shared by all replicas, with
  negative caching for unknown codes.
* **Click counting** -- buffered in Redis and written back in batches, so the
  hot path never contends on a single-row UPDATE.

Every replica is stateless: no session affinity is required and any replica can
serve any request. The `X-Served-By` response header names the replica that
answered, and `X-Request-ID` correlates the response with the structured logs.

### Errors
Every non-2xx response uses one envelope:
`{"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}`
"""

TAGS_METADATA = [
    {"name": "urls", "description": "Create, inspect, list, and delete shortened URLs."},
    {"name": "redirect", "description": "The public redirect hot path."},
    {"name": "system", "description": "Liveness, readiness, and Prometheus metrics."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Connect, verify, and start background work; drain in reverse on exit.

    The component objects themselves are built in `create_app` -- neither the
    asyncpg engine nor the Redis pool opens a socket until first use, and the
    rate-limit middleware needs the limiter at construction time.
    """
    settings: Settings = app.state.settings
    database: Database = app.state.database
    redis: RedisGateway = app.state.redis
    clicks: ClickTracker = app.state.click_tracker

    app_info.labels(
        version=__version__,
        instance=settings.instance_id,
        env=settings.app_env,
    ).set(1)
    seed_counters(CACHE_NAME)

    # Probe dependencies but do not refuse to start. A replica that boots during
    # a database blip should come up and report itself unready via /ready,
    # rather than crash-loop and be unable to serve the moment things recover.
    postgres_ok = await database.ping()
    redis_ok = await redis.ping()
    clicks.start()

    logger.info(
        "application started",
        extra={
            "version": __version__,
            "env": settings.app_env,
            "cache_enabled": settings.cache_enabled,
            "rate_limit_enabled": settings.rate_limit_enabled,
            "click_flush_enabled": settings.click_flush_enabled,
            "postgres_ok": postgres_ok,
            "redis_ok": redis_ok,
        },
    )

    try:
        yield
    finally:
        # Uvicorn has already stopped accepting connections and waited for
        # in-flight requests before the lifespan shutdown runs, so draining
        # here cannot lose a request that is still being served.
        logger.info("application shutting down")
        await clicks.stop()
        await database.dispose()
        await redis.close()
        logger.info("application shutdown complete")


def _register_exception_handlers(app: FastAPI) -> None:
    """One JSON envelope for every failure mode."""

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error(
                "request failed with server error",
                extra={"error_code": exc.error_code, "status_code": exc.status_code},
            )
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_body(
                error_code=exc.error_code,
                message=exc.message,
                request_id=get_request_id(),
                details=exc.details,
            ),
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's raw error list is reshaped into `field -> message` pairs:
        # the frontend renders these next to the offending input, and the raw
        # form (with `loc` tuples and `ctx` objects) is awkward for that and can
        # echo back submitted values.
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
                "message": str(error.get("msg", "invalid value")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            content=build_error_body(
                error_code="validation_error",
                message="Request validation failed.",
                request_id=get_request_id(),
                details={"fields": fields},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {400: "bad_request", 404: "not_found", 405: "method_not_allowed"}
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_body(
                error_code=codes.get(exc.status_code, f"http_{exc.status_code}"),
                message=str(exc.detail),
                request_id=get_request_id(),
            ),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        # A constraint violation that escaped the service layer is a client
        # problem (bad data), not a 500. Constraint names are not leaked.
        postgres_errors_total.labels(operation="integrity").inc()
        logger.warning("database constraint violated", extra={"reason": type(exc).__name__})
        return JSONResponse(
            status_code=HTTP_400_BAD_REQUEST,
            content=build_error_body(
                error_code="constraint_violation",
                message="The request violates a data constraint.",
                request_id=get_request_id(),
            ),
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        postgres_errors_total.labels(operation="request").inc()
        logger.error("database error", extra={"reason": type(exc).__name__})
        return JSONResponse(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            content=build_error_body(
                error_code="dependency_unavailable",
                message="The database is currently unavailable. Please retry shortly.",
                request_id=get_request_id(),
                details={"dependency": "postgres"},
            ),
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # The message is deliberately generic; the detail lives in the log line
        # keyed by request_id, so a client cannot fish for internals.
        logger.exception("unhandled exception", extra={"reason": type(exc).__name__})
        return JSONResponse(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            content=build_error_body(
                error_code="internal_error",
                message="An unexpected error occurred.",
                request_id=get_request_id(),
            ),
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application."""
    resolved = settings or get_settings()
    configure_logging(resolved)

    redis = RedisGateway(resolved)
    database = Database(resolved)
    cache = UrlCache(redis, resolved)
    clicks = ClickTracker(redis, database, resolved)
    rate_limiter = RateLimiter(redis, resolved)
    url_service = UrlService(db=database, cache=cache, clicks=clicks, settings=resolved)

    # Middleware order is outermost-first.
    #
    # 1. RequestContext  -- must wrap everything so that 429s and 5xx are still
    #                       logged, timed, and counted.
    # 2. SecurityHeaders -- applies to every response, including errors.
    # 3. CORS            -- must sit OUTSIDE the rate limiter. A 429 without
    #                       CORS headers is unreadable to the browser, so the
    #                       React app could not display the message.
    # 4. RateLimit       -- sheds load before any request work happens.
    # 5. BodySizeLimit   -- innermost, so oversized bodies still consume a token
    #                       (they are the cheapest thing to make expensive).
    middleware = [
        Middleware(RequestContextMiddleware, settings=resolved),
        Middleware(SecurityHeadersMiddleware, settings=resolved),
        Middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_allow_origins),
            allow_credentials=False,  # no cookies/auth today; keeps `*` misuse impossible
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-Request-ID"],
            expose_headers=[
                "X-Request-ID",
                "X-Served-By",
                "Retry-After",
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
                "X-RateLimit-Policy",
                "X-RateLimit-Bucket",
                "X-Cache",
            ],
            max_age=600,
        ),
        Middleware(RateLimitMiddleware, limiter=rate_limiter, settings=resolved),
        Middleware(BodySizeLimitMiddleware, settings=resolved),
    ]

    app = FastAPI(
        title="Distributed URL Shortener API",
        version=__version__,
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        docs_url="/docs" if resolved.docs_enabled else None,
        redoc_url="/redoc" if resolved.docs_enabled else None,
        openapi_url="/openapi.json" if resolved.docs_enabled else None,
        middleware=middleware,
        # Errors are rendered by our own handlers, so the default HTML
        # traceback page (which leaks source) is never used.
        debug=False,
        # Starlette's automatic trailing-slash redirect is off for two reasons.
        # It fought with nginx: nginx 301s `/api` to `/api/` (its rule for a
        # prefix proxy_pass location), Starlette 307d `/api/` back to `/api`,
        # and clients bounced between them until they gave up. It also built
        # the Location from the proxied Host, which drops a published port.
        # An API is better off with exact paths and a plain 404 than with
        # guessed redirects.
        redirect_slashes=False,
    )
    app.state.settings = resolved
    app.state.redis = redis
    app.state.database = database
    app.state.cache = cache
    app.state.click_tracker = clicks
    app.state.rate_limiter = rate_limiter
    app.state.url_service = url_service

    _register_exception_handlers(app)

    app.include_router(routes_system.router)
    app.include_router(routes_urls.router)
    # Registered last: `/{code}` is a catch-all and must not shadow real routes.
    app.include_router(routes_redirect.router)

    return app


app = create_app()
