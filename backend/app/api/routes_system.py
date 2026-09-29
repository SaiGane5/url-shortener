"""Liveness, readiness, and metrics endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app import __version__
from app.api.deps import DatabaseDep, RedisDep, SettingsDep
from app.core.metrics import REGISTRY
from app.schemas.url import DependencyStatus, HealthResponse, ReadyResponse

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Returns 200 as long as the process is running and its event loop is responsive. "
        "**Deliberately checks no dependencies.** A liveness probe that fails when "
        "PostgreSQL is down would make the orchestrator kill and restart every healthy "
        "replica during a database incident, turning a partial outage into a total one. "
        "Dependency state belongs in `/ready`."
    ),
)
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        instance=settings.instance_id,
    )


@router.get(
    "/ready",
    response_model=ReadyResponse,
    summary="Readiness probe",
    responses={
        200: {"description": "Ready, possibly in a degraded (cache-less) mode."},
        503: {
            "model": ReadyResponse,
            "description": "PostgreSQL is unreachable; this instance cannot serve traffic.",
        },
    },
    description=(
        "Verifies both backing services and reports each one.\n\n"
        "* **PostgreSQL unreachable -> 503.** It is the source of truth; without it "
        "nothing can be created and cache misses cannot be served.\n"
        "* **Redis unreachable -> 200 with `degraded: true`.** The service still "
        "resolves links straight from PostgreSQL. Removing the instance from the load "
        "balancer would not help, because every replica shares the same Redis, so "
        "failing readiness would just take the whole service offline over a "
        "cache outage."
    ),
)
async def ready(
    response: Response,
    database: DatabaseDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> ReadyResponse:
    # Probe both concurrently: readiness should cost one round trip, not two.
    postgres_ok, redis_ok = await asyncio.gather(database.ping(), redis.ping())

    if not postgres_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        overall = "unavailable"
    elif not redis_ok:
        overall = "degraded"
    else:
        overall = "ready"

    return ReadyResponse(
        status=overall,
        instance=settings.instance_id,
        dependencies=DependencyStatus(postgres=postgres_ok, redis=redis_ok),
        degraded=postgres_ok and not redis_ok,
    )


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    response_class=Response,
    responses={
        200: {
            "content": {"text/plain": {}},
            "description": "Metrics in the Prometheus text exposition format.",
        }
    },
    description=(
        "Prometheus exposition endpoint for this instance only. Each replica is scraped "
        "individually (see prometheus/prometheus.yml), so counters are per-instance and "
        "are summed in PromQL. Nginx restricts this path to the internal network."
    ),
)
async def metrics(settings: SettingsDep) -> Response:
    if not settings.metrics_enabled:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
