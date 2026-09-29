"""Destination-URL validation -- the SSRF and malicious-scheme boundary.

Threat model for a URL shortener is unusual: the server never fetches the
destination, so classic server-side SSRF does not apply. The victim is the
*visitor's browser*, which we are about to send somewhere on their behalf.
That makes three classes of input dangerous:

1. Non-HTTP schemes (`javascript:`, `data:`, `file:`) -- turn a shortlink into
   a stored-XSS or local-file vector once a client follows it.
2. Private/loopback/link-local targets -- lets an attacker use our domain's
   reputation to aim a victim's browser at their own router, or at
   169.254.169.254 from inside a cloud network.
3. Embedded credentials -- `https://paypal.com@evil.example` renders as PayPal
   in a preview and lands on the attacker's host.

Each test below maps to one of those.
"""

from __future__ import annotations

import pytest

from app.core.errors import InvalidURLError
from app.services.url_validation import validate_destination_url
from tests.conftest import make_settings


def reason_for(raw_url: str, **overrides: object) -> str:
    """Validate and return the machine-readable rejection reason."""
    with pytest.raises(InvalidURLError) as excinfo:
        validate_destination_url(raw_url, make_settings(**overrides))
    details = excinfo.value.details or {}
    return str(details.get("reason"))


class TestAccepts:
    @pytest.mark.parametrize(
        "raw_url",
        [
            "https://example.com",
            "https://example.com/",
            "http://example.com/deep/path?query=1&other=2#frag",
            "https://sub.domain.example.co.uk/path",
            "https://example.com:8443/path",
            "https://8.8.8.8/",  # public IP literal is fine
            "https://example.com/path%20with%20encoding",
            "https://example.com/unicode/%E2%9C%93",
        ],
    )
    def test_accepts_ordinary_public_urls(self, raw_url: str) -> None:
        result = validate_destination_url(raw_url, make_settings())
        assert result.scheme in {"http", "https"}
        assert result.host

    def test_strips_surrounding_whitespace(self) -> None:
        result = validate_destination_url("  https://example.com/x  ", make_settings())
        assert result.normalized == "https://example.com/x"

    def test_host_is_lowercased_but_path_case_is_preserved(self) -> None:
        """Hosts are case-insensitive; paths are not.

        Lowercasing the path would break destinations that use case-sensitive
        identifiers (S3 keys, git refs, base64 tokens).
        """
        result = validate_destination_url("https://ExAmPle.COM/CaseSensitive", make_settings())
        assert result.host == "example.com"
        assert "/CaseSensitive" in result.normalized


class TestSchemes:
    @pytest.mark.parametrize(
        "raw_url",
        [
            "javascript:alert(document.cookie)",
            "JavaScript:alert(1)",
            "  javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com",
            "vbscript:msgbox(1)",
            "blob:https://example.com/uuid",
            "about:blank",
            "chrome://settings",
            "intent://scan#Intent;scheme=zxing;end",
        ],
    )
    def test_rejects_non_http_schemes(self, raw_url: str) -> None:
        assert reason_for(raw_url) in {"scheme_not_allowed", "missing_host", "invalid_host"}

    def test_rejects_missing_scheme(self) -> None:
        """The backend does not guess a scheme.

        The frontend helpfully prefixes `https://` for humans, but the API
        stays strict: silently upgrading `example.com` server-side would mean
        two clients sending the same string get different destinations.
        """
        assert reason_for("example.com/path") == "missing_scheme"

    def test_scheme_allowlist_is_configurable(self) -> None:
        settings = make_settings(allowed_url_schemes=("https",))
        assert validate_destination_url("https://example.com", settings).scheme == "https"
        with pytest.raises(InvalidURLError):
            validate_destination_url("http://example.com", settings)


class TestSsrfTargets:
    @pytest.mark.parametrize(
        ("raw_url", "expected_reason"),
        [
            ("http://127.0.0.1/admin", "loopback_target"),
            ("http://[::1]/admin", "loopback_target"),
            ("http://10.0.0.5/internal", "private_target"),
            ("http://172.16.31.9/internal", "private_target"),
            ("http://192.168.1.1/router", "private_target"),
            ("http://169.254.169.254/latest/meta-data/", "link_local_target"),
            ("http://[fe80::1]/", "link_local_target"),
            ("http://[fd00::1]/", "private_target"),
            ("http://service.internal/", "internal_hostname"),
            ("http://printer.local/", "internal_hostname"),
            ("http://db.lan/", "internal_hostname"),
            ("http://metadata.google.internal/", "metadata_target"),
            # 0.0.0.0/8 ("this network") is reported as private by
            # ipaddress before the unspecified check is reached.
            ("http://0.0.0.0/", "private_target"),
            # Dotless hosts are caught earlier than the address classifier,
            # because they can only resolve via a local search domain.
            ("http://localhost:8000/", "dotless_hostname"),
            ("http://LOCALHOST:8000/", "dotless_hostname"),
            ("http://intranet/", "dotless_hostname"),
        ],
    )
    def test_rejects_internal_targets(self, raw_url: str, expected_reason: str) -> None:
        assert reason_for(raw_url) == expected_reason

    @pytest.mark.parametrize(
        "raw_url",
        [
            "http://127.1/admin",  # shorthand
            "http://0177.0.0.1/",  # octal
            "http://0x7f.0.0.1/",  # hex
            "http://127.0.1/",  # 3-part shorthand
        ],
    )
    def test_rejects_legacy_ipv4_forms_of_loopback(self, raw_url: str) -> None:
        """Regression: these bypassed the blocklist entirely.

        `ipaddress.ip_address` rejects all four, so they used to fall through
        to the hostname path -- and because `127.1` contains a dot, the
        dotless-host check did not catch them either. Browsers and curl
        resolve every one of them to 127.0.0.1.
        """
        assert reason_for(raw_url) == "loopback_target"

    def test_rejects_legacy_ipv4_forms_of_private_ranges(self) -> None:
        assert reason_for("http://10.1/") == "private_target"

    @pytest.mark.parametrize(
        "raw_url",
        [
            "http://127.0.0.1/admin",
            "http://localhost:8000/",
            "http://10.0.0.5/",
        ],
    )
    def test_private_targets_allowed_when_explicitly_enabled(self, raw_url: str) -> None:
        """The escape hatch exists for local development and load testing.

        docs/ENGINEERING_DECISIONS.md records that it must never be enabled in
        a deployment that accepts untrusted input.
        """
        settings = make_settings(allow_private_url_targets=True)
        assert validate_destination_url(raw_url, settings).host

    def test_decimal_encoded_loopback_is_rejected(self) -> None:
        """2130706433 == 127.0.0.1; caught as dotless before address parsing."""
        assert reason_for("http://2130706433/") == "dotless_hostname"

    def test_public_ip_is_not_treated_as_private(self) -> None:
        """Guards against an over-broad private-range check.

        8.8.8.8 and 1.1.1.1 sit next to reserved blocks; a sloppy CIDR test
        catches them and would break legitimate links.
        """
        for raw_url in ("https://8.8.8.8/", "https://1.1.1.1/", "https://172.32.0.1/"):
            assert validate_destination_url(raw_url, make_settings()).host


class TestCredentialsAndSpoofing:
    @pytest.mark.parametrize(
        "raw_url",
        [
            "https://paypal.com@evil.example/login",
            "https://user:password@example.com/",
            "https://admin@example.com/",
        ],
    )
    def test_rejects_embedded_credentials(self, raw_url: str) -> None:
        assert reason_for(raw_url) == "userinfo_present"


class TestMalformedInput:
    @pytest.mark.parametrize("raw_url", ["", "   ", "\t\n"])
    def test_rejects_empty(self, raw_url: str) -> None:
        assert reason_for(raw_url) == "empty"

    def test_rejects_urls_over_the_length_limit(self) -> None:
        settings = make_settings(url_max_length=64)
        long_url = "https://example.com/" + "a" * 100
        with pytest.raises(InvalidURLError) as excinfo:
            validate_destination_url(long_url, settings)
        assert (excinfo.value.details or {}).get("reason") == "too_long"

    def test_accepts_url_exactly_at_the_limit(self) -> None:
        """Off-by-one guard: the limit is inclusive."""
        url = "https://example.com/" + "a" * 20  # 40 chars; min allowed limit is 32
        settings = make_settings(url_max_length=len(url))
        assert validate_destination_url(url, settings).host == "example.com"

    def test_rejects_url_one_character_over_the_limit(self) -> None:
        url = "https://example.com/" + "a" * 20
        settings = make_settings(url_max_length=len(url) - 1)
        with pytest.raises(InvalidURLError):
            validate_destination_url(url, settings)

    @pytest.mark.parametrize(
        "raw_url",
        [
            "https://exa\x00mple.com/",
            "https://example.com/\npath",
            "https://example.com/\rpath",
            "https://example.com/\x1bpath",
        ],
    )
    def test_rejects_control_characters(self, raw_url: str) -> None:
        """Newlines in a stored URL become header injection on redirect.

        The value ends up in a `Location:` header, so CR/LF must never survive
        validation.
        """
        assert reason_for(raw_url) == "control_characters"

    @pytest.mark.parametrize(
        "raw_url",
        [
            "https://",
            "http:///path",
            "https://:8080/",
        ],
    )
    def test_rejects_urls_without_a_host(self, raw_url: str) -> None:
        assert reason_for(raw_url) in {"missing_host", "invalid_host"}

    def test_rejects_malformed_ipv6_literal(self) -> None:
        assert reason_for("http://[::1/") in {"invalid_host", "missing_host", "unparseable"}
