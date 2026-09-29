"""Short-code generation.

Algorithm: cryptographically random base62
------------------------------------------
Each code is `SHORT_CODE_LENGTH` characters drawn uniformly from
`[A-Za-z0-9]` using `secrets.choice` (the OS CSPRNG).

Why random and not a monotonic counter encoded in base62?

* A counter leaks volume. `aB3` -> `aB4` tells any observer exactly how many
  links exist and lets them enumerate every link ever created. For a service
  whose links are unguessable-by-obscurity, that is the whole security model
  gone.
* A shared counter is distributed state. Either a sequence in PostgreSQL (a
  write per attempt, and a single point of contention) or a Redis INCR block
  allocator (extra failure mode: allocated-but-unused ranges on restart).
  Random generation needs *no* coordination, which is what keeps the API
  replicas independent and stateless.

Collision handling -- collisions are assumed, not wished away
-------------------------------------------------------------
With a 62-character alphabet and length 7 the keyspace is 62^7 =
3,521,614,606,208 (~3.5e12). By the birthday bound, the probability that a
*new* code collides with an existing one is n / 62^7:

    stored links     P(collision on next insert)
    1,000,000        ~1 in 3,500,000   (2.8e-7)
    10,000,000       ~1 in 350,000     (2.8e-6)
    100,000,000      ~1 in 35,000      (2.8e-5)

So at 100M links roughly 1 create in 35,000 needs a second attempt, and the
chance of `SHORT_CODE_MAX_ATTEMPTS`=5 consecutive collisions is (2.8e-5)^5 --
around 1e-23. Those are the numbers, but they are not the safety mechanism.
The safety mechanism is the UNIQUE index on `urls.short_code`: generation
proposes, PostgreSQL decides. Two replicas racing on the same code produce one
successful INSERT and one `IntegrityError`, and the loser retries. That is
correct even if the RNG were badly broken.
"""

from __future__ import annotations

import secrets
import string
from typing import Final

from app.core.config import Settings
from app.core.errors import InvalidShortCodeError, ShortCodeNotFoundError

#: Base62. Deliberately no `-`/`_`: those are legal in a URL path but are the
#: characters most often mangled by autolinkers, chat clients, and
#: double-click-to-select. Custom aliases may use them; generated codes do not.
ALPHABET: Final = string.ascii_uppercase + string.ascii_lowercase + string.digits

#: Codes that would shadow a real route (or a well-known browser request) and
#: are therefore never generated and never accepted as a custom alias.
RESERVED_CODES: Final = frozenset(
    {
        "api",
        "assets",
        "admin",
        "docs",
        "redoc",
        "openapi.json",
        "health",
        "healthz",
        "ready",
        "readyz",
        "metrics",
        "static",
        "urls",
        "favicon.ico",
        "robots.txt",
        "sitemap.xml",
        "index.html",
        "login",
        "signup",
        "about",
    }
)

_ALIAS_ALLOWED_CHARS: Final = frozenset(ALPHABET + "-_")


def generate_short_code(length: int) -> str:
    """Generate one candidate short code. Uniqueness is the database's job."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def keyspace_size(length: int) -> int:
    """Total number of distinct codes of the given length."""
    return int(len(ALPHABET) ** length)


def collision_probability(stored_count: int, length: int) -> float:
    """Probability that the next generated code collides with an existing one."""
    return stored_count / keyspace_size(length)


def validate_short_code(code: str, settings: Settings, *, is_custom_alias: bool = False) -> str:
    """Validate a short code from a URL path, or a requested custom alias.

    Applied before any I/O, so an impossible code costs no Redis or
    PostgreSQL work -- which is what makes 404-scanning traffic cheap.

    The two callers want different answers to the same rejection, so the
    outcome depends on `is_custom_alias`:

    * **Custom alias (creation).** The code is a *form field*, and the user can
      fix it. Raise `InvalidShortCodeError` (422) with a message saying what
      is wrong.
    * **Lookup (`GET /{code}`).** The code is a *URL path*. A path that cannot
      correspond to any link is simply not found -- returning 422 would claim
      the client sent a malformed request when it merely visited a dead link,
      would put non-errors in the 4xx error-rate panel, and would disclose
      which words are reserved (`/docs` on a deployment with docs disabled
      would answer differently from `/dcos`). Raise `ShortCodeNotFoundError`
      (404) for every rejection.
    """

    def reject(message: str) -> InvalidShortCodeError | ShortCodeNotFoundError:
        if is_custom_alias:
            return InvalidShortCodeError(message)
        return ShortCodeNotFoundError()

    if not code:
        raise reject("Short code must not be empty.")

    if len(code) > settings.short_code_max_length:
        raise reject(f"Short code must be at most {settings.short_code_max_length} characters.")

    if is_custom_alias and len(code) < 4:
        raise reject("Custom alias must be at least 4 characters.")

    if not _ALIAS_ALLOWED_CHARS.issuperset(code):
        raise reject("Short code may only contain letters, digits, hyphens, and underscores.")

    if code.lower() in RESERVED_CODES:
        raise reject("That short code is reserved.")

    return code
