"""FastAPI dependencies.

Long-lived components (engine, Redis pool, service objects) are built once in
the lifespan handler and stashed on `app.state`. These accessors are the typed
seam between that container and the route functions, which keeps routes
trivially unit-testable: override the dependency, inject a fake.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.cache.redis_client import RedisGateway
from app.core.config import Settings
from app.db.session import Database
from app.ratelimit.limiter import RateLimiter
from app.services.click_tracker import ClickTracker
from app.services.url_service import UrlService


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_redis(request: Request) -> RedisGateway:
    redis: RedisGateway = request.app.state.redis
    return redis


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


def get_click_tracker(request: Request) -> ClickTracker:
    tracker: ClickTracker = request.app.state.click_tracker
    return tracker


def get_url_service(request: Request) -> UrlService:
    service: UrlService = request.app.state.url_service
    return service


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DatabaseDep = Annotated[Database, Depends(get_database)]
RedisDep = Annotated[RedisGateway, Depends(get_redis)]
UrlServiceDep = Annotated[UrlService, Depends(get_url_service)]
