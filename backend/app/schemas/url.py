"""Request/response schemas.

Pydantic does the *structural* validation (types, lengths, required fields) so
that malformed input never reaches application code. Semantic validation --
"is this URL one we are willing to shorten?" -- lives in
`app/services/url_validation.py`, because it depends on runtime configuration
and needs to emit its own metrics and error codes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Hard structural ceiling. The *effective* limit is `URL_MAX_LENGTH` and is
#: enforced by the service, which knows the configured value.
URL_ABSOLUTE_MAX_LENGTH = 8192


class UrlStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"


class UrlCreateRequest(BaseModel):
    """Body of `POST /api/v1/urls`."""

    model_config = ConfigDict(
        extra="forbid",  # typos become 422s instead of silently ignored fields
        json_schema_extra={
            "examples": [
                {"url": "https://example.com/some/very/long/url", "expires_at": None},
                {
                    "url": "https://docs.python.org/3/library/asyncio.html",
                    "expires_at": "2030-01-01T00:00:00Z",
                    "custom_alias": "asyncio-docs",
                },
            ]
        },
    )

    url: Annotated[
        str,
        Field(
            min_length=1,
            max_length=URL_ABSOLUTE_MAX_LENGTH,
            description="Absolute destination URL. Must use http or https.",
        ),
    ]
    expires_at: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "Optional expiry as an ISO-8601 timestamp. Must be in the future. "
                "A naive timestamp is interpreted as UTC. Null means the link never expires."
            ),
        ),
    ] = None
    custom_alias: Annotated[
        str | None,
        Field(
            default=None,
            min_length=4,
            max_length=16,
            pattern=r"^[A-Za-z0-9_-]+$",
            description=(
                "Optional human-chosen short code. Returns 409 if already taken. "
                "Omit to receive a randomly generated code."
            ),
        ),
    ] = None

    @field_validator("expires_at")
    @classmethod
    def _must_be_future_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        # Treat naive input as UTC rather than rejecting it: `datetime-local`
        # inputs in browsers submit naive values, and the alternative is a
        # confusing 422 for a perfectly reasonable form submission.
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        if normalized <= datetime.now(UTC):
            msg = "expires_at must be in the future"
            raise ValueError(msg)
        return normalized


class UrlResponse(BaseModel):
    """A shortened URL as returned by the API."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "short_code": "aB3xY7z",
                    "short_url": "http://localhost:8080/aB3xY7z",
                    "original_url": "https://example.com/some/very/long/url",
                    "created_at": "2026-09-04T10:00:00Z",
                    "updated_at": "2026-09-04T10:00:00Z",
                    "expires_at": None,
                    "click_count": 0,
                    "status": "active",
                }
            ]
        }
    )

    short_code: str = Field(description="The generated or requested short code.")
    short_url: str = Field(description="Fully qualified short URL, built from PUBLIC_BASE_URL.")
    original_url: str = Field(description="Normalised destination URL.")
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    click_count: int = Field(
        description=(
            "Total redirects served. Includes increments still buffered in Redis, "
            "so it is accurate even between write-back flushes."
        )
    )
    status: UrlStatus


class UrlListResponse(BaseModel):
    """Keyset-paginated listing of recently created URLs."""

    items: list[UrlResponse]
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor. Pass as `cursor` to fetch the next page; null when exhausted.",
    )


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable description, safe to display.")
    details: dict[str, Any] | None = None
    request_id: str | None = Field(
        default=None,
        description="Correlates this response with the server's structured logs.",
    )


class ErrorResponse(BaseModel):
    """The single error envelope used by every non-2xx API response."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "rate_limited",
                        "message": "Rate limit exceeded. Please slow down and retry shortly.",
                        "details": {"policy": "write"},
                        "request_id": "5f2b1c9a4d7e4f3a8b0c1d2e3f405162",
                    }
                }
            ]
        }
    )

    error: ErrorDetail


class DependencyStatus(BaseModel):
    postgres: bool
    redis: bool


class HealthResponse(BaseModel):
    """Liveness. Intentionally dependency-free -- see the route docstring."""

    status: str = "ok"
    service: str
    version: str
    instance: str


class ReadyResponse(BaseModel):
    """Readiness, including the state of each required dependency."""

    status: str
    instance: str
    dependencies: DependencyStatus
    degraded: bool = Field(
        description="True when the instance is serving but with reduced capability (e.g. no cache)."
    )
