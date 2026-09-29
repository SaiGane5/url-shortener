"""Application exception hierarchy and the single API error envelope.

Every failure the client can observe -- validation, 404, 410, 429, 503 --
serialises to the same JSON shape so the React client has exactly one error
parser (see frontend/src/api/client.ts).
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all errors that map to a deliberate HTTP response."""

    status_code: int = 500
    error_code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details
        self.headers = headers or {}
        super().__init__(self.message)


class InvalidURLError(AppError):
    """The submitted destination URL is not one we are willing to shorten."""

    status_code = 422
    error_code = "invalid_url"
    message = "The provided URL is not valid."


class InvalidShortCodeError(AppError):
    status_code = 422
    error_code = "invalid_short_code"
    message = "The provided short code is not valid."


class ShortCodeNotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    message = "No URL exists for that short code."


class ShortCodeExpiredError(AppError):
    """410 rather than 404: the resource existed and is intentionally gone."""

    status_code = 410
    error_code = "expired"
    message = "This short URL has expired."


class ShortCodeConflictError(AppError):
    status_code = 409
    error_code = "conflict"
    message = "That short code is already in use."


class AliasUnavailableError(ShortCodeConflictError):
    """The specific, user-actionable case of a conflict: the alias is taken.

    Distinct from the parent because the parent is also raised for *generated*
    code collisions, which the service retries and the client never sees.
    Giving the alias case its own code lets the frontend say "pick another
    alias" instead of showing a generic conflict.
    """

    error_code = "alias_taken"
    message = "That alias is already taken. Please choose another."


class CustomAliasDisabledError(AppError):
    """Aliases are switched off for this deployment.

    400, not 409: nothing is in conflict, and a client that reads 409 would
    reasonably retry with a different alias -- which would fail identically,
    forever.
    """

    status_code = 400
    error_code = "custom_alias_disabled"
    message = "Custom aliases are not enabled on this deployment."


class ShortCodeGenerationError(AppError):
    """Every generation attempt collided -- almost certainly means the keyspace
    is saturated and `SHORT_CODE_LENGTH` needs to grow."""

    status_code = 503
    error_code = "short_code_generation_failed"
    message = "Could not allocate a unique short code. Please retry."


class RateLimitExceededError(AppError):
    status_code = 429
    error_code = "rate_limited"
    message = "Rate limit exceeded. Please slow down and retry shortly."


class PayloadTooLargeError(AppError):
    status_code = 413
    error_code = "payload_too_large"
    message = "Request body is too large."


class DependencyUnavailableError(AppError):
    """A required backing service (PostgreSQL) is unreachable."""

    status_code = 503
    error_code = "dependency_unavailable"
    message = "A required dependency is currently unavailable."

    def __init__(
        self,
        dependency: str,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = {"dependency": dependency, **(details or {})}
        super().__init__(message, details=merged, headers={"Retry-After": "5"})


def build_error_body(
    *,
    error_code: str,
    message: str,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical error envelope."""
    body: dict[str, Any] = {"error": {"code": error_code, "message": message}}
    if details:
        body["error"]["details"] = details
    if request_id:
        body["error"]["request_id"] = request_id
    return body
