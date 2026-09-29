"""`GET /{code}` -- the redirect hot path.

Registered last so that every real route (`/api/...`, `/health`, `/docs`, ...)
wins over the catch-all, and reserved codes are rejected in
`app/services/short_code.py` as a second line of defence.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, status
from fastapi.responses import RedirectResponse

from app.api.deps import UrlServiceDep
from app.schemas.url import ErrorResponse

router = APIRouter(tags=["redirect"])


@router.get(
    "/{code}",
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    response_class=RedirectResponse,
    summary="Redirect to the destination URL",
    responses={
        307: {"description": "Redirect to the destination URL."},
        404: {
            "model": ErrorResponse,
            "description": (
                "No such link: unknown, deleted, malformed, or a reserved word. "
                "All four collapse to 404 so the response cannot be used to "
                "probe which codes are structurally valid or reserved."
            ),
        },
        410: {"model": ErrorResponse, "description": "The link existed but has expired."},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded."},
    },
    description=(
        "Resolves a short code and redirects to its destination.\n\n"
        "**Read path (cache-aside).** Redis is consulted first; a hit answers without "
        "touching PostgreSQL. A miss reads the authoritative row, populates Redis, and "
        "then answers. Unknown codes are cached as a short-lived tombstone so that a bot "
        "walking random codes cannot turn every request into a database query.\n\n"
        "**307, not 301.** A 301 is cached by the browser *forever*, which means "
        "deleting or expiring a link would not stop repeat visitors, and every click "
        "after the first would be invisible to our metrics. 307 keeps the service "
        "authoritative on every click and keeps the counter honest. The trade-off is "
        "one request per click instead of zero -- exactly the load the Redis cache "
        "exists to absorb.\n\n"
        "**Counting.** The click is buffered in Redis and folded into PostgreSQL by a "
        "background task, so a viral link does not serialise its traffic behind a "
        "single-row UPDATE.\n\n"
        "**Rate limiting.** Uses the wide `redirect` policy, because a popular link "
        "legitimately produces heavy traffic from one corporate NAT address."
    ),
)
async def redirect_to_destination(
    code: Annotated[
        str,
        Path(
            # Deliberately no min/max_length constraint. FastAPI enforces those
            # before the handler runs and answers 422, but for a *path* segment
            # the honest answer is 404: an over-long code names no link, and it
            # is not a malformed request. `validate_short_code` owns the length
            # and charset rules so that every unresolvable path -- bad charset,
            # too long, reserved word -- gets one consistent 404.
            description=(
                "The short code. Codes are base62 and at most "
                "`SHORT_CODE_MAX_LENGTH` (default 16) characters; anything "
                "else cannot exist and returns 404."
            ),
            examples=["aB3xY7z"],
        ),
    ],
    service: UrlServiceDep,
) -> RedirectResponse:
    resolution = await service.resolve_for_redirect(code)
    return RedirectResponse(
        url=resolution.target_url,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={
            # Belt and braces: the destination was validated at creation time,
            # but telling intermediaries not to store the redirect keeps a
            # deleted link from living on in a CDN.
            "Cache-Control": "no-store, max-age=0",
            # Makes cache-aside behaviour verifiable per request instead of
            # only in aggregate via Prometheus: the first hit on a code reports
            # `miss`, subsequent ones `hit`. Used by docs/TESTING.md and by the
            # k6 redirect scenario to compute a client-side hit ratio.
            "X-Cache": resolution.cache_state.value,
        },
    )
