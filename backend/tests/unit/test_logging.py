"""Structured logging and redaction.

Logs are the one place where sensitive data leaks by accident rather than by
attack: a well-meaning `logger.info("creating", extra={"url": raw_url})` ships
a user's session token to whatever aggregates the logs, where it is retained
far longer than in any database. These tests pin the redaction rules so that
cannot regress quietly.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from app.core.context import request_id_var, route_var
from app.core.logging import JsonFormatter, safe_url


@pytest.fixture
def formatter() -> JsonFormatter:
    return JsonFormatter(service="url-shortener", environment="test", instance_id="api-1")


def emit(formatter: JsonFormatter, **extra: Any) -> dict[str, Any]:
    """Format one INFO record and return the parsed JSON payload."""
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event happened",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    parsed: dict[str, Any] = json.loads(formatter.format(record))
    return parsed


class TestSafeUrl:
    def test_keeps_scheme_host_and_path(self) -> None:
        assert safe_url("https://example.com/a/b") == "https://example.com/a/b"

    def test_drops_the_query_string(self) -> None:
        """Query strings routinely carry tokens, emails, and session ids.

        This is the single most valuable redaction in the system: shortened
        URLs are user-submitted and frequently contain exactly these.
        """
        result = safe_url("https://example.com/reset?token=super-secret&email=a@b.com")

        assert "super-secret" not in result
        assert "a@b.com" not in result
        assert result == "https://example.com/reset?[redacted]"

    def test_drops_the_fragment(self) -> None:
        """OAuth implicit flow puts access tokens in the fragment."""
        result = safe_url("https://example.com/cb#access_token=secret123")
        assert "secret123" not in result

    def test_truncates_a_very_long_path(self) -> None:
        result = safe_url("https://example.com/" + "a" * 500)

        assert len(result) < 200
        assert result.endswith("...")

    def test_handles_unparseable_input_without_raising(self) -> None:
        """A logging helper must never be the thing that breaks a request."""
        for value in ("", "not a url", "://", "\x00"):
            assert safe_url(value) == "[unparseable-url]"

    def test_does_not_leak_credentials_in_userinfo(self) -> None:
        """Regression: built from `netloc`, this leaked the password verbatim.

        Such URLs are rejected by validation, but `safe_url` is the sanctioned
        way to log *any* user-submitted URL and runs on paths that execute
        before validation, so it has to be safe unaided.
        """
        result = safe_url("https://user:hunter2@example.com/x")

        assert "hunter2" not in result
        assert "user" not in result
        assert result == "https://example.com/x"

    def test_preserves_a_non_default_port(self) -> None:
        assert safe_url("https://example.com:8443/x") == "https://example.com:8443/x"

    def test_handles_ipv6_hosts(self) -> None:
        assert safe_url("http://[::1]:8080/x") == "http://[::1]:8080/x"


class TestJsonFormatter:
    def test_emits_the_expected_envelope(self, formatter: JsonFormatter) -> None:
        payload = emit(formatter)

        assert payload["level"] == "INFO"
        assert payload["logger"] == "app.test"
        assert payload["message"] == "event happened"
        assert payload["service"] == "url-shortener"
        assert payload["env"] == "test"
        assert payload["instance"] == "api-1"
        assert payload["timestamp"].endswith("+00:00")

    def test_output_is_a_single_line(self, formatter: JsonFormatter) -> None:
        """Multi-line output breaks line-oriented log shippers."""
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="line one\nline two",
            args=(),
            exc_info=None,
        )
        assert "\n" not in formatter.format(record)

    def test_includes_structured_extras(self, formatter: JsonFormatter) -> None:
        payload = emit(formatter, short_code="aB3xY7z", duration_ms=12.5, status_code=307)

        assert payload["short_code"] == "aB3xY7z"
        assert payload["duration_ms"] == 12.5
        assert payload["status_code"] == 307

    @pytest.mark.parametrize(
        "field",
        [
            "authorization",
            "cookie",
            "password",
            "secret",
            "token",
            "api_key",
            "access_token",
            "refresh_token",
            "database_url",
            "redis_url",
        ],
    )
    def test_redacts_sensitive_fields(self, formatter: JsonFormatter, field: str) -> None:
        payload = emit(formatter, **{field: "super-secret-value"})

        assert payload[field] == "[redacted]"
        assert "super-secret-value" not in json.dumps(payload)

    def test_redaction_is_case_insensitive(self, formatter: JsonFormatter) -> None:
        """`Authorization` is the spelling that actually appears in headers."""
        payload = emit(formatter, Authorization="Bearer abc123")

        assert payload["Authorization"] == "[redacted]"
        assert "abc123" not in json.dumps(payload)

    def test_serialises_unexpected_types_instead_of_raising(self, formatter: JsonFormatter) -> None:
        """An un-JSON-able extra must not turn a log call into an exception."""

        class Opaque:
            def __repr__(self) -> str:
                return "<opaque>"

        payload = emit(formatter, thing=Opaque())
        assert payload["thing"] == "<opaque>"

    def test_includes_exception_details(self, formatter: JsonFormatter) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = logging.LogRecord(
                name="app.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="failed",
                args=(),
                exc_info=sys.exc_info(),
            )
            payload = json.loads(formatter.format(record))

        assert payload["error"]["type"] == "ValueError"
        assert payload["error"]["message"] == "boom"
        assert "stack" in payload["error"]


class TestRequestCorrelation:
    def test_includes_request_id_from_context(self, formatter: JsonFormatter) -> None:
        """The whole point of the contextvar: correlation without plumbing.

        Any log call anywhere in the request lifecycle carries the id without
        the caller passing it.
        """
        token = request_id_var.set("0123456789abcdef")
        try:
            payload = emit(formatter)
        finally:
            request_id_var.reset(token)

        assert payload["request_id"] == "0123456789abcdef"

    def test_includes_route_from_context(self, formatter: JsonFormatter) -> None:
        token = route_var.set("/api/v1/urls")
        try:
            payload = emit(formatter)
        finally:
            route_var.reset(token)

        assert payload["route"] == "/api/v1/urls"

    def test_omits_correlation_fields_when_unset(self, formatter: JsonFormatter) -> None:
        """Outside a request (startup, background tasks) there is no id.

        Emitting an empty string would create a misleading series in log
        aggregators that facet on the field.
        """
        payload = emit(formatter)

        assert "request_id" not in payload
        assert "route" not in payload
