"""Prometheus metric definitions.

Cardinality rules enforced here:
* `route` is always the *matched route template* (`/api/v1/urls/{code}`), never
  the raw path -- otherwise every short code would create a new time series.
* No metric is ever labelled with a client IP, a short code, or a destination
  URL. Those are high-cardinality and, in the URL case, user data.
* Each container runs a single application process and is scraped
  individually, so we deliberately avoid prometheus_client's multiprocess
  collector (see docs/ENGINEERING_DECISIONS.md).
"""

from __future__ import annotations

from typing import Final

from prometheus_client import REGISTRY as DEFAULT_REGISTRY
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY: CollectorRegistry = DEFAULT_REGISTRY

_LATENCY_BUCKETS = (
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    10.0,
)

_DEPENDENCY_BUCKETS = (
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
)

# ------------------------------------------------------------------ HTTP ---

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled.",
    labelnames=("method", "route", "status", "status_class"),
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds, measured inside the application.",
    labelnames=("method", "route"),
    buckets=_LATENCY_BUCKETS,
)

# Unlabelled on purpose: this is incremented before routing happens, so the
# route template is not yet known, and the raw path is unbounded.
http_requests_in_progress = Gauge(
    "http_requests_in_progress",
    "HTTP requests currently being processed by this instance.",
)

http_request_body_bytes_rejected_total = Counter(
    "http_request_body_bytes_rejected_total",
    "Requests rejected because the body exceeded MAX_REQUEST_BODY_BYTES.",
)

# ----------------------------------------------------------- application ---

urls_created_total = Counter(
    "urls_created_total",
    "Short URLs successfully created.",
    labelnames=("alias_kind",),  # generated | custom
)

urls_deleted_total = Counter(
    "urls_deleted_total",
    "Short URLs deleted.",
)

url_redirects_total = Counter(
    "url_redirects_total",
    "Redirect attempts by outcome.",
    labelnames=("result",),  # redirected | not_found | expired
)

short_code_collisions_total = Counter(
    "short_code_collisions_total",
    "Short-code generation attempts that lost a uniqueness race.",
)

short_code_generation_failures_total = Counter(
    "short_code_generation_failures_total",
    "Create requests that exhausted every short-code generation attempt.",
)

url_validation_rejections_total = Counter(
    "url_validation_rejections_total",
    "Destination URLs rejected by validation, by reason.",
    labelnames=("reason",),
)

clicks_recorded_total = Counter(
    "clicks_recorded_total",
    "Click increments accepted, by write path.",
    labelnames=("path",),  # redis_buffer | direct_db
)

clicks_flushed_total = Counter(
    "clicks_flushed_total",
    "Buffered click increments successfully persisted to PostgreSQL.",
)

click_flush_failures_total = Counter(
    "click_flush_failures_total",
    "Click-buffer flush cycles that failed and were requeued.",
)

# ----------------------------------------------------------------- cache ---

cache_hits_total = Counter(
    "cache_hits_total",
    "Cache lookups served from Redis.",
    labelnames=("cache", "kind"),  # kind: value | negative
)

cache_misses_total = Counter(
    "cache_misses_total",
    "Cache lookups that had to fall back to PostgreSQL.",
    labelnames=("cache",),
)

cache_errors_total = Counter(
    "cache_errors_total",
    "Cache operations that failed (Redis unreachable, decode failure, ...).",
    labelnames=("cache", "operation"),
)

cache_invalidations_total = Counter(
    "cache_invalidations_total",
    "Explicit cache invalidations performed.",
    labelnames=("cache", "reason"),
)

# ------------------------------------------------------------ rate limit ---

rate_limit_decisions_total = Counter(
    "rate_limit_decisions_total",
    "Rate-limiter decisions by policy and outcome.",
    labelnames=("policy", "decision"),  # allowed | limited | failed_open | failed_closed
)

rate_limit_violations_total = Counter(
    "rate_limit_violations_total",
    "Requests rejected with HTTP 429 by the distributed rate limiter.",
    labelnames=("policy",),
)

rate_limit_evaluation_seconds = Histogram(
    "rate_limit_evaluation_seconds",
    "Latency of the Redis token-bucket evaluation.",
    buckets=_DEPENDENCY_BUCKETS,
)

# ---------------------------------------------------------- dependencies ---

redis_errors_total = Counter(
    "redis_errors_total",
    "Redis operations that raised, by logical operation.",
    labelnames=("operation",),
)

postgres_errors_total = Counter(
    "postgres_errors_total",
    "PostgreSQL operations that raised, by logical operation.",
    labelnames=("operation",),
)

dependency_latency_seconds = Histogram(
    "dependency_latency_seconds",
    "Backing-service call latency in seconds.",
    labelnames=("dependency", "operation"),
    buckets=_DEPENDENCY_BUCKETS,
)

dependency_up = Gauge(
    "dependency_up",
    "1 if the dependency responded to the most recent readiness probe, else 0.",
    labelnames=("dependency",),
)

app_info = Gauge(
    "app_build_info",
    "Static build/deployment metadata (always 1).",
    labelnames=("version", "instance", "env"),
)


def status_class(status_code: int) -> str:
    """Bucket a status code into `2xx`/`4xx`/`5xx` for cheap error-rate queries."""
    return f"{status_code // 100}xx"


# A labelled prometheus_client counter emits no series until the first
# `.inc()`, so a healthy process exports *nothing* for its error counters.
# On a dashboard that is indistinguishable from a typo'd metric name: both
# render "No data". Seeding the known label values at startup makes a healthy
# system draw a flat zero and a broken query stay visibly empty.
_SEEDED_CACHE_ERROR_OPERATIONS: Final = (
    "get",
    "decode",
    "set",
    "set_missing",
    "invalidate",
)
_SEEDED_REDIS_ERROR_OPERATIONS: Final = ("ping", "rate_limit", "click_record", "click_claim")
_SEEDED_POSTGRES_ERROR_OPERATIONS: Final = ("ping", "request", "integrity")

# The hit-ratio panel divides by `hits + misses`. On a replica that has served
# no redirects yet, both series are absent and the whole expression -- not just
# one term -- evaluates to nothing, so scaling out briefly blanks the panel.
# Seeding makes a fresh replica report an honest 0/0 instead of disappearing.
_SEEDED_CACHE_HIT_KINDS: Final = ("value", "negative")


def seed_counters(cache_name: str) -> None:
    """Materialise known counter series at zero so dashboards read unambiguously.

    Only the statically known label values are seeded; dynamically labelled
    operations still appear on first use.
    """
    for kind in _SEEDED_CACHE_HIT_KINDS:
        cache_hits_total.labels(cache=cache_name, kind=kind)
    cache_misses_total.labels(cache=cache_name)
    for operation in _SEEDED_CACHE_ERROR_OPERATIONS:
        cache_errors_total.labels(cache=cache_name, operation=operation)
    for operation in _SEEDED_REDIS_ERROR_OPERATIONS:
        redis_errors_total.labels(operation=operation)
    for operation in _SEEDED_POSTGRES_ERROR_OPERATIONS:
        postgres_errors_total.labels(operation=operation)
