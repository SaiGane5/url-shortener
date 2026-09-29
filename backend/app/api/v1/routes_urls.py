"""`/api/v1/urls` -- create, read, list, and delete shortened URLs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.api.deps import SettingsDep, UrlServiceDep
from app.core.config import Settings
from app.schemas.url import (
    ErrorResponse,
    UrlCreateRequest,
    UrlListResponse,
    UrlResponse,
)
from app.services.url_service import UrlRecord

router = APIRouter(prefix="/api/v1/urls", tags=["urls"])

CodePath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=16,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="The short code, e.g. `aB3xY7z`.",
        examples=["aB3xY7z"],
    ),
]

_RATE_LIMIT_NOTE = (
    "\n\n**Rate limiting.** Enforced per client IP by a Redis token bucket shared by "
    "every API replica. Rejections return `429` with `Retry-After` and "
    "`X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset` / "
    "`X-RateLimit-Policy` headers. Those headers are present on successful responses "
    "too, so a client can back off before being throttled."
)

_COMMON_ERRORS: dict[int | str, dict[str, object]] = {
    422: {"model": ErrorResponse, "description": "Request or URL failed validation."},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    503: {
        "model": ErrorResponse,
        "description": "A backing dependency is unavailable.",
    },
}


def _to_response(record: UrlRecord, settings: Settings) -> UrlResponse:
    return UrlResponse(
        short_code=record.short_code,
        short_url=f"{settings.public_base_url}/{record.short_code}",
        original_url=record.original_url,
        created_at=record.created_at,
        updated_at=record.updated_at,
        expires_at=record.expires_at,
        click_count=record.click_count,
        status=record.status,
    )


@router.post(
    "",
    response_model=UrlResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a shortened URL",
    responses={
        201: {"description": "Short URL created."},
        409: {
            "model": ErrorResponse,
            "description": "The requested `custom_alias` is already taken.",
        },
        **_COMMON_ERRORS,
    },
    description=(
        "Creates a short code for a destination URL.\n\n"
        "**Validation.** Only `http` and `https` are accepted, so `javascript:` and "
        "`data:` payloads are rejected. URLs embedding credentials "
        "(`https://user:pass@host`) are rejected because browsers render the host after "
        "the `@`, which makes them ideal phishing bait. By default destinations that "
        "resolve to loopback, link-local, or RFC1918 addresses are refused "
        "(`ALLOW_PRIVATE_URL_TARGETS=false`).\n\n"
        "**Codes.** Omit `custom_alias` to receive a random 7-character base62 code. "
        "Uniqueness is guaranteed by a UNIQUE index, and a collision is retried "
        "transparently.\n\n"
        "**Idempotency.** This endpoint is *not* idempotent: submitting the same URL "
        "twice yields two independent short codes. That is intentional, so two users "
        "can shorten the same destination without sharing a click counter." + _RATE_LIMIT_NOTE
    ),
)
async def create_url(
    payload: UrlCreateRequest,
    service: UrlServiceDep,
    settings: SettingsDep,
) -> UrlResponse:
    record = await service.create(
        raw_url=payload.url,
        expires_at=payload.expires_at,
        custom_alias=payload.custom_alias,
    )
    return _to_response(record, settings)


@router.get(
    "",
    response_model=UrlListResponse,
    summary="List recently created URLs",
    responses=_COMMON_ERRORS,
    description=(
        "Newest-first listing, used by the frontend's 'recent links' panel.\n\n"
        "Keyset (cursor) pagination, not `OFFSET`: `OFFSET n` makes PostgreSQL walk and "
        "discard n rows, so deep pages get linearly slower. The cursor encodes "
        "`(created_at, id)`, which matches the `ix_urls_created_at_id` index exactly "
        "and is stable while new rows are being inserted." + _RATE_LIMIT_NOTE
    ),
)
async def list_urls(
    service: UrlServiceDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size.")] = 20,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor from a previous response's `next_cursor`."),
    ] = None,
) -> UrlListResponse:
    page = await service.list_recent(limit=limit, cursor=cursor)
    return UrlListResponse(
        items=[_to_response(record, settings) for record in page.items],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{code}",
    response_model=UrlResponse,
    summary="Get metadata for a short code",
    responses={
        404: {"model": ErrorResponse, "description": "Unknown or deleted short code."},
        **_COMMON_ERRORS,
    },
    description=(
        "Returns the stored metadata for a short code, including the click count.\n\n"
        "Unlike the redirect endpoint this reads PostgreSQL rather than the cache, "
        "because the cache deliberately does not store `click_count` (it changes on "
        "every redirect). The response adds any increments still buffered in Redis to "
        "the persisted total, so the number is correct even between write-back flushes."
        '\n\nAn expired link is still returned here, with `status: "expired"`, so the UI '
        "can explain *why* a link stopped working. Only deleted links 404." + _RATE_LIMIT_NOTE
    ),
)
async def get_url(
    code: CodePath,
    service: UrlServiceDep,
    settings: SettingsDep,
) -> UrlResponse:
    record = await service.get_metadata(code)
    return _to_response(record, settings)


@router.delete(
    "/{code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a short code",
    responses={
        204: {"description": "Deleted. The cache entry is invalidated as part of the request."},
        404: {"model": ErrorResponse, "description": "Unknown or already-deleted short code."},
        **_COMMON_ERRORS,
    },
    description=(
        "Deletes a short URL and invalidates its cache entry.\n\n"
        "**Soft delete.** The row is marked deleted rather than removed, which "
        "permanently retires the code. Hard-deleting would return the code to the "
        "random keyspace, and a later regeneration of the same code would silently "
        "redirect traffic from links already pasted into emails and chat logs to a new "
        "destination.\n\n"
        "**Ordering.** PostgreSQL is committed first, then Redis is invalidated. "
        "Invalidating first would let a concurrent reader repopulate the cache from the "
        "still-live row and outlive the delete." + _RATE_LIMIT_NOTE
    ),
)
async def delete_url(code: CodePath, service: UrlServiceDep) -> Response:
    await service.delete(code)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
