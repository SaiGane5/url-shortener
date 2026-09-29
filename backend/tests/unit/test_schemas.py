"""Pydantic request/response contract.

Client-facing validation only. These schemas are a fast, cheap first filter;
the service layer re-validates everything that matters for security (SSRF,
scheme allowlist, length), because a schema that is relaxed for usability must
never be the only gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.url import UrlCreateRequest, UrlResponse, UrlStatus


class TestUrlCreateRequest:
    def test_minimal_payload(self) -> None:
        request = UrlCreateRequest(url="https://example.com/x")

        assert request.url == "https://example.com/x"
        assert request.expires_at is None
        assert request.custom_alias is None

    def test_rejects_missing_url(self) -> None:
        with pytest.raises(ValidationError):
            UrlCreateRequest()  # type: ignore[call-arg]

    def test_rejects_empty_url(self) -> None:
        with pytest.raises(ValidationError):
            UrlCreateRequest(url="")

    def test_whitespace_only_url_is_left_to_the_service(self) -> None:
        """Structurally a non-empty string; semantically empty.

        The schema deliberately does not try to be the semantic gate --
        `validate_destination_url` strips and rejects it with a precise
        `reason` the frontend can act on. Asserted here so the division of
        responsibility is explicit rather than accidental.
        """
        assert UrlCreateRequest(url="   ").url == "   "

    def test_rejects_absurdly_long_url_before_the_service_sees_it(self) -> None:
        """A structural ceiling, independent of the configured URL_MAX_LENGTH.

        Stops a multi-megabyte string being parsed and validated field by
        field when it can never be valid.
        """
        with pytest.raises(ValidationError):
            UrlCreateRequest(url="https://example.com/" + "a" * 100_000)

    def test_accepts_future_expiry(self) -> None:
        expires = datetime.now(UTC) + timedelta(days=1)
        assert UrlCreateRequest(url="https://example.com", expires_at=expires).expires_at

    def test_rejects_past_expiry(self) -> None:
        past = datetime.now(UTC) - timedelta(days=1)
        with pytest.raises(ValidationError):
            UrlCreateRequest(url="https://example.com", expires_at=past)

    def test_treats_naive_datetime_as_utc(self) -> None:
        """Browsers send `datetime-local` without a zone.

        Rejecting naive input would make the frontend's native date picker
        unusable; assuming UTC is predictable and documented.
        """
        naive = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None)
        request = UrlCreateRequest(url="https://example.com", expires_at=naive)

        assert request.expires_at is not None
        assert request.expires_at.tzinfo is not None

    def test_accepts_valid_alias(self) -> None:
        assert UrlCreateRequest(url="https://example.com", custom_alias="my-link").custom_alias

    @pytest.mark.parametrize("alias", ["ab", "has space", "has/slash", "a" * 64])
    def test_rejects_structurally_invalid_alias(self, alias: str) -> None:
        with pytest.raises(ValidationError):
            UrlCreateRequest(url="https://example.com", custom_alias=alias)

    def test_rejects_unknown_fields(self) -> None:
        """Strict rather than forgiving, on purpose.

        Silently ignoring extras makes a client typo fail *quietly*: send
        `expiresAt` instead of `expires_at` and you get a permanent link when
        you asked for an expiring one, with a 201 telling you it worked.
        Failing the request surfaces the mistake at development time.
        """
        with pytest.raises(ValidationError):
            UrlCreateRequest(
                url="https://example.com",
                expiresAt="2030-01-01T00:00:00Z",  # type: ignore[call-arg]
            )


class TestUrlResponse:
    def test_serialises_timestamps_as_utc_iso8601(self) -> None:
        """The React client parses these with `new Date(...)`.

        A missing zone would be interpreted as local time in the browser and
        show the wrong creation date.
        """
        now = datetime.now(UTC)
        response = UrlResponse(
            short_code="aB3xY7z",
            short_url="http://localhost:8080/aB3xY7z",
            original_url="https://example.com/x",
            created_at=now,
            updated_at=now,
            expires_at=None,
            click_count=0,
            status=UrlStatus.ACTIVE,
        )
        dumped = response.model_dump(mode="json")

        assert dumped["created_at"].endswith("Z")
        assert dumped["expires_at"] is None
        assert dumped["status"] == "active"

    def test_status_is_a_string_on_the_wire(self) -> None:
        """StrEnum keeps the JSON a plain string rather than an object."""
        assert UrlStatus.ACTIVE.value == "active"
        assert UrlStatus.EXPIRED.value == "expired"
        assert isinstance(UrlStatus.ACTIVE, str)
