"""Rate-limit policies.

Three policies rather than one, because the endpoint classes have genuinely
different costs and different abuse profiles:

* `redirect`  -- GET /{code}. Cache-served, ~1 Redis GET. Wide budget; a
                 popular link legitimately generates a lot of traffic from one
                 corporate NAT egress IP.
* `read`      -- JSON API reads. Moderate budget, the "100 req/min" default the
                 brief asks for.
* `write`     -- POST/DELETE. Each one is a PostgreSQL write and permanently
                 consumes a short code from the keyspace, so it gets the
                 tightest budget.

Failure mode is also per policy -- see `fail_open` below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.config import Settings


class PolicyName(StrEnum):
    READ = "read"
    WRITE = "write"
    REDIRECT = "redirect"


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """A single token-bucket configuration."""

    name: PolicyName
    #: Sustained quota: `requests` per `window_seconds`.
    requests: int
    window_seconds: int
    #: Bucket capacity. Equal to `requests` by default, which means a client may
    #: spend its whole minute's quota instantly and then be throttled to the
    #: refill rate -- the behaviour most users expect from "100/min".
    burst: int
    #: What to do when Redis cannot be reached.
    #:
    #: True (reads/redirects): serve the request. A Redis outage already costs
    #: us the cache; also refusing all traffic would turn a degradation into an
    #: outage, and these endpoints are cheap and idempotent.
    #:
    #: False (writes): reject with 503. Writes hit PostgreSQL and burn short
    #: codes, so with no limiter available the safe move is to shed them rather
    #: than expose an unmetered write path to the internet.
    fail_open: bool

    @property
    def refill_per_second(self) -> float:
        return self.requests / self.window_seconds

    @property
    def ttl_seconds(self) -> int:
        """Idle expiry for the bucket key.

        Must be at least the time needed to refill from empty to full,
        otherwise deleting the key would silently grant a fresh full bucket.
        """
        return max(self.window_seconds * 2, 60)

    @property
    def header_value(self) -> str:
        """RFC-8941-style policy description for the X-RateLimit-Policy header."""
        return (
            f"{self.requests};w={self.window_seconds};burst={self.burst};"
            f"policy={self.name.value};algorithm=token_bucket"
        )


def build_policies(settings: Settings) -> dict[PolicyName, RateLimitPolicy]:
    """Materialise the policy table from configuration."""
    return {
        PolicyName.READ: RateLimitPolicy(
            name=PolicyName.READ,
            requests=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            burst=settings.effective_rate_limit_burst,
            fail_open=settings.rate_limit_fail_open,
        ),
        PolicyName.WRITE: RateLimitPolicy(
            name=PolicyName.WRITE,
            requests=settings.rate_limit_write_requests,
            window_seconds=settings.rate_limit_write_window_seconds,
            burst=settings.effective_write_rate_limit_burst,
            fail_open=settings.rate_limit_write_fail_open,
        ),
        PolicyName.REDIRECT: RateLimitPolicy(
            name=PolicyName.REDIRECT,
            requests=settings.rate_limit_redirect_requests,
            window_seconds=settings.rate_limit_redirect_window_seconds,
            burst=settings.effective_redirect_rate_limit_burst,
            fail_open=settings.rate_limit_fail_open,
        ),
    }
