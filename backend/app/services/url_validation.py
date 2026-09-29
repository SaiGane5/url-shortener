"""Destination-URL validation.

Threat model
------------
This service does not fetch the URLs it stores, so classic server-side SSRF
(the server being tricked into calling an internal endpoint) is not the
exposure. The exposure is that we hand out a short link that someone *else's*
browser will follow, which makes us a laundering service for:

* `javascript:` / `data:` / `vbscript:` payloads -- blocked by scheme allowlist.
* Credential-phishing URLs such as `https://paypal.com@evil.example` -- blocked
  by rejecting userinfo, because browsers show the part after `@` as the real
  host while humans read the part before it.
* Internal targets (`http://169.254.169.254/...`, `http://10.0.0.5/admin`)
  aimed at a victim who *is* inside the perimeter -- blocked by default via
  `ALLOW_PRIVATE_URL_TARGETS=false`.

Deliberate non-goal: we do NOT resolve hostnames at creation time.
1. It puts a DNS round-trip on the write path.
2. It would be security theatre. Validation happens at create time but
   resolution happens in the visitor's browser at click time, so an attacker
   can point a public hostname at a private address afterwards (DNS
   rebinding). A check that can be trivially bypassed but *looks* like a
   control is worse than a documented limitation.

The honest mitigation for internal targets is therefore: block IP literals and
known-internal names (cheap, no bypass), and treat hostname-based internal
targets as accepted residual risk (documented in docs/PRODUCTION_READINESS.md).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.core.config import Settings
from app.core.errors import InvalidURLError
from app.core.metrics import url_validation_rejections_total

#: Hostnames (and suffixes) that resolve inside a private network by convention.
_INTERNAL_HOST_SUFFIXES = (
    "localhost",
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
)

#: Cloud instance-metadata endpoints, blocked by name as well as by IP literal.
_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

_CONTROL_CHARS = frozenset(chr(code) for code in [*range(0, 32), 127])


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    """A destination URL that passed validation, in normalised form."""

    normalized: str
    host: str
    scheme: str


def _reject(reason: str, message: str) -> InvalidURLError:
    url_validation_rejections_total.labels(reason=reason).inc()
    return InvalidURLError(message, details={"reason": reason})


def _permissive_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse the *legacy* IPv4 forms that real resolvers accept.

    SECURITY: `ipaddress.ip_address` only accepts dotted-quad, but
    `inet_aton` -- and therefore browsers, curl, and libc `getaddrinfo` --
    also accept shorthand, octal, and hex forms. All of these reach loopback:

        127.0.0.1   127.1   0177.0.0.1   0x7f.0.0.1   2130706433

    Checking only the strict form leaves `http://127.1/` looking like an
    ordinary hostname (it even contains a dot, so the dotless-host check
    misses it too), which is a complete bypass of the loopback/private
    blocklist. Normalising here means every form is judged as the address it
    actually resolves to.
    """
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed)


def _is_private_host(host: str, *, allow_private: bool) -> str | None:
    """Return a rejection reason if `host` looks internal, else None."""
    lowered = host.lower().strip("[]")

    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(
            lowered
        )
    except ValueError:
        address = _permissive_ipv4(lowered)

    if address is not None:
        if allow_private:
            return None
        if address.is_loopback:
            return "loopback_target"
        if address.is_link_local:
            # Covers 169.254.169.254, the cloud metadata service.
            return "link_local_target"
        if address.is_private:
            return "private_target"
        if address.is_reserved or address.is_multicast or address.is_unspecified:
            return "reserved_target"
        return None

    if allow_private:
        return None
    if lowered in _METADATA_HOSTS:
        return "metadata_target"
    if lowered == "localhost" or any(
        lowered.endswith(suffix) for suffix in _INTERNAL_HOST_SUFFIXES if suffix.startswith(".")
    ):
        return "internal_hostname"
    return None


def validate_destination_url(raw_url: str, settings: Settings) -> ValidatedUrl:
    """Validate and normalise a user-submitted destination URL.

    Raises:
        InvalidURLError: with a machine-readable `details.reason`.
    """
    if not raw_url or not raw_url.strip():
        raise _reject("empty", "URL must not be empty.")

    candidate = raw_url.strip()

    if len(candidate) > settings.url_max_length:
        raise _reject(
            "too_long",
            f"URL must be at most {settings.url_max_length} characters.",
        )

    if any(char in _CONTROL_CHARS for char in candidate):
        raise _reject("control_characters", "URL must not contain control characters.")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise _reject("unparseable", "URL could not be parsed.") from exc

    scheme = parts.scheme.lower()
    if not scheme:
        raise _reject(
            "missing_scheme",
            "URL must include a scheme, for example https://example.com.",
        )
    if scheme not in settings.allowed_url_schemes:
        raise _reject(
            "scheme_not_allowed",
            f"URL scheme '{scheme}' is not allowed. "
            f"Allowed schemes: {', '.join(settings.allowed_url_schemes)}.",
        )

    if parts.username or parts.password:
        raise _reject(
            "userinfo_present",
            "URLs containing credentials (user:pass@host) are not allowed.",
        )

    try:
        hostname = parts.hostname
    except ValueError as exc:  # malformed IPv6 literal
        raise _reject("invalid_host", "URL host is not valid.") from exc

    if not hostname:
        raise _reject("missing_host", "URL must include a host.")

    # A dotless, non-IP host (e.g. `http://intranet/`) can only resolve through
    # a local search domain, so it is internal by construction.
    try:
        ipaddress.ip_address(hostname)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False

    if not is_ip_literal and "." not in hostname and not settings.allow_private_url_targets:
        raise _reject("dotless_hostname", "URL host must be a fully qualified domain name.")

    try:
        port = parts.port
    except ValueError as exc:
        raise _reject("invalid_port", "URL port is not valid.") from exc

    if reason := _is_private_host(hostname, allow_private=settings.allow_private_url_targets):
        raise _reject(
            reason,
            "URLs pointing at private, loopback, or internal addresses are not allowed.",
        )

    # Normalise: lowercase scheme and host (both case-insensitive per RFC 3986),
    # preserve path/query/fragment byte-for-byte (they are case-sensitive and
    # rewriting them could break the destination), and default an empty path
    # to "/" so `https://a.com` and `https://a.com/` are one entry.
    netloc = hostname.lower()
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"  # re-bracket IPv6
    if port is not None:
        netloc = f"{netloc}:{port}"

    normalized = urlunsplit((scheme, netloc, parts.path or "/", parts.query, parts.fragment))

    if len(normalized) > settings.url_max_length:
        raise _reject(
            "too_long",
            f"URL must be at most {settings.url_max_length} characters.",
        )

    return ValidatedUrl(normalized=normalized, host=netloc, scheme=scheme)
