"""Typed, environment-driven application configuration.

Every tunable knob lives here so that the same image can run as a local dev
container, a load-test target, or a production instance with nothing but
environment variables changing.
"""

from __future__ import annotations

import json
import socket
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]

# pydantic-settings JSON-decodes "complex" field types (tuple/list/dict) inside
# the env source, *before* field validators run, so a plain `http,https` would
# blow up as invalid JSON. NoDecode hands the raw string to our own validator
# instead, which accepts both comma-separated and JSON list forms.
CsvTuple = Annotated[tuple[str, ...], NoDecode]


class Settings(BaseSettings):
    """Application settings, populated from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app ---
    app_name: str = "url-shortener"
    app_env: Environment = "local"
    debug: bool = False
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # Identifies which API replica served a request. Surfaced in the
    # X-Served-By response header and in logs so that load balancing across
    # instances is observable from the outside (see docs/TESTING.md).
    instance_id: str = Field(default_factory=socket.gethostname)

    # Public origin used to build the returned `short_url`. Behind Nginx this
    # is the load balancer's origin, not the container's own address.
    public_base_url: str = "http://localhost:8080"

    # ----------------------------------------------------------- database ---
    database_url: str = "postgresql+asyncpg://urlshort:urlshort@localhost:5432/urlshort"
    db_pool_size: Annotated[int, Field(ge=1, le=100)] = 10
    db_max_overflow: Annotated[int, Field(ge=0, le=100)] = 10
    db_pool_timeout_seconds: Annotated[float, Field(gt=0)] = 5.0
    db_pool_recycle_seconds: int = 1800
    db_command_timeout_seconds: Annotated[float, Field(gt=0)] = 5.0
    db_echo: bool = False

    # -------------------------------------------------------------- redis ---
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: Annotated[int, Field(ge=1)] = 50
    redis_socket_timeout_seconds: Annotated[float, Field(gt=0)] = 0.25
    redis_connect_timeout_seconds: Annotated[float, Field(gt=0)] = 0.25

    # -------------------------------------------------------------- cache ---
    # cache_enabled=false is what the BASELINE benchmark run uses: it forces
    # every redirect through PostgreSQL. See docs/PERFORMANCE.md.
    cache_enabled: bool = True
    cache_key_prefix: str = "url"
    cache_schema_version: int = 1
    cache_ttl_seconds: Annotated[int, Field(ge=1)] = 300
    # Random 0..jitter seconds added per key so that a batch of keys written
    # together does not expire together (cache stampede mitigation).
    cache_ttl_jitter_seconds: Annotated[int, Field(ge=0)] = 60
    # Short-lived negative cache entry for unknown codes, so scans for random
    # codes cannot turn into one PostgreSQL query per request.
    cache_negative_ttl_seconds: Annotated[int, Field(ge=0)] = 30

    # --------------------------------------------------------- rate limit ---
    rate_limit_enabled: bool = True
    # General read/API policy: `rate_limit_requests` per `rate_limit_window_seconds` per client.
    rate_limit_requests: Annotated[int, Field(ge=1)] = 100
    rate_limit_window_seconds: Annotated[int, Field(ge=1)] = 60
    # Token bucket burst capacity. Defaults to the window quota when unset.
    rate_limit_burst: int | None = None
    # Stricter policy for write endpoints (POST/DELETE), which cost a DB write.
    rate_limit_write_requests: Annotated[int, Field(ge=1)] = 20
    rate_limit_write_window_seconds: Annotated[int, Field(ge=1)] = 60
    rate_limit_write_burst: int | None = None
    # Redirects are the cheap, cacheable hot path and get a much wider budget.
    rate_limit_redirect_requests: Annotated[int, Field(ge=1)] = 1000
    rate_limit_redirect_window_seconds: Annotated[int, Field(ge=1)] = 60
    rate_limit_redirect_burst: int | None = None
    rate_limit_key_prefix: str = "rl"
    # Redis-unavailable behaviour, per endpoint class. Reads/redirects fail
    # OPEN (a cache outage should not become a site outage); writes fail
    # CLOSED (an unmetered write path is worse than a rejected write).
    # Justified in docs/ARCHITECTURE.md and docs/ENGINEERING_DECISIONS.md.
    rate_limit_fail_open: bool = True
    rate_limit_write_fail_open: bool = False
    # Header name Nginx uses to pass the real client IP.
    client_ip_header: str = "X-Forwarded-For"
    # Number of trusted proxy hops in front of the app. The client IP is taken
    # this many entries from the right of X-Forwarded-For, so clients cannot
    # spoof their identity by sending their own header.
    trusted_proxy_hops: Annotated[int, Field(ge=0)] = 1

    # -------------------------------------------------------- short codes ---
    short_code_length: Annotated[int, Field(ge=4, le=16)] = 7
    short_code_max_attempts: Annotated[int, Field(ge=1, le=10)] = 5
    # Codes longer than this are rejected by the redirect route before any I/O.
    short_code_max_length: Annotated[int, Field(ge=4, le=32)] = 16
    # Optional user-supplied aliases are validated against the same charset.
    allow_custom_alias: bool = True

    # ------------------------------------------------ url validation / ssrf ---
    url_max_length: Annotated[int, Field(ge=32, le=8192)] = 2048
    allowed_url_schemes: CsvTuple = ("http", "https")
    # SECURITY: shortened links are followed by other people's browsers, so by
    # default we refuse to mint links that point at loopback, link-local, or
    # RFC1918 addresses. See docs/ENGINEERING_DECISIONS.md ("SSRF").
    allow_private_url_targets: bool = False
    max_request_body_bytes: Annotated[int, Field(ge=256)] = 16_384

    # ------------------------------------------------------ click counting ---
    # Redirects increment a Redis counter (atomic, cheap) that a background
    # task folds into PostgreSQL, instead of doing an UPDATE per redirect.
    click_flush_enabled: bool = True
    click_flush_interval_seconds: Annotated[float, Field(gt=0)] = 5.0
    click_flush_batch_size: Annotated[int, Field(ge=1)] = 500

    # --------------------------------------------------------------- http ---
    cors_allow_origins: CsvTuple = ("http://localhost:5173", "http://localhost:8080")
    docs_enabled: bool = True
    metrics_enabled: bool = True
    # Time given to in-flight requests on SIGTERM before the process exits.
    graceful_shutdown_seconds: Annotated[float, Field(ge=0)] = 10.0

    @field_validator("cors_allow_origins", "allowed_url_schemes", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept `A,B` as well as JSON lists for CsvTuple settings.

        These fields carry NoDecode, so this validator owns the whole
        string -> tuple conversion, JSON form included.
        """
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list):
                msg = "JSON form must be a list"
                raise TypeError(msg)
            return tuple(str(item).strip() for item in parsed)
        return tuple(item.strip() for item in stripped.split(",") if item.strip())

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            msg = f"log_level must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return upper

    @property
    def effective_rate_limit_burst(self) -> int:
        return self.rate_limit_burst or self.rate_limit_requests

    @property
    def effective_write_rate_limit_burst(self) -> int:
        return self.rate_limit_write_burst or self.rate_limit_write_requests

    @property
    def effective_redirect_rate_limit_burst(self) -> int:
        return self.rate_limit_redirect_burst or self.rate_limit_redirect_requests

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached so .env is read once)."""
    return Settings()
