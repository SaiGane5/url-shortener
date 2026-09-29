"""End-to-end HTTP behaviour of the URL API, against real PostgreSQL + Redis.

Driven through the ASGI app with httpx so routing, middleware, dependency
wiring, exception handlers, and serialisation are all in the path -- the same
code a real request traverses, minus the network hop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.integration]


async def create_url(client: AsyncClient, url: str, **extra: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/urls", json={"url": url, **extra})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


class TestCreate:
    async def test_creates_a_short_url(self, client: AsyncClient) -> None:
        body = await create_url(client, "https://example.com/a/very/long/path?x=1")

        assert body["original_url"] == "https://example.com/a/very/long/path?x=1"
        assert body["short_code"]
        assert body["short_url"].endswith(body["short_code"])
        assert body["click_count"] == 0
        assert body["status"] == "active"
        assert body["expires_at"] is None
        assert body["created_at"] and body["updated_at"]

    async def test_short_url_is_built_from_public_base_url(self, app_factory: Any) -> None:
        """The replica's own hostname must never leak into the returned link.

        Behind a load balancer the container host is an implementation detail;
        the link has to be the public origin or it is useless when pasted.
        """
        client = await app_factory(public_base_url="https://links.example.test")
        body = await create_url(client, "https://example.com/x")

        assert body["short_url"] == f"https://links.example.test/{body['short_code']}"

    async def test_repeated_creates_yield_distinct_codes(self, client: AsyncClient) -> None:
        """Deliberately not deduplicating identical destinations.

        Two users shortening the same URL get separate codes so that either
        can delete or expire theirs without affecting the other.
        """
        codes = set()
        for _ in range(5):
            body = await create_url(client, "https://example.com/same")
            codes.add(body["short_code"])
        assert len(codes) == 5

    async def test_normalises_the_destination(self, client: AsyncClient) -> None:
        body = await create_url(client, "  https://ExAmPle.COM  ")
        assert body["original_url"] == "https://example.com/"

    async def test_accepts_a_future_expiry(self, client: AsyncClient) -> None:
        expires = datetime.now(UTC) + timedelta(days=7)
        body = await create_url(client, "https://example.com/x", expires_at=expires.isoformat())

        assert body["expires_at"] is not None
        assert body["status"] == "active"

    async def test_accepts_a_custom_alias(self, client: AsyncClient) -> None:
        body = await create_url(client, "https://example.com/x", custom_alias="my-cool-link")
        assert body["short_code"] == "my-cool-link"

    async def test_rejects_a_duplicate_custom_alias(self, client: AsyncClient) -> None:
        await create_url(client, "https://example.com/first", custom_alias="taken-alias")
        response = await client.post(
            "/api/v1/urls",
            json={"url": "https://example.com/second", "custom_alias": "taken-alias"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "alias_taken"

    async def test_rejects_custom_alias_when_disabled(self, app_factory: Any) -> None:
        """400, not 409.

        A 409 would tell the client "that one is taken, try another", and it
        would keep trying forever against a deployment that has the feature
        switched off entirely.
        """
        client = await app_factory(allow_custom_alias=False)
        response = await client.post(
            "/api/v1/urls",
            json={"url": "https://example.com/x", "custom_alias": "some-alias"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "custom_alias_disabled"

    async def test_rejects_reserved_alias(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/urls",
            json={"url": "https://example.com/x", "custom_alias": "metrics"},
        )
        assert response.status_code in {400, 409, 422}


class TestCreateValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            {},  # missing url
            {"url": ""},
            {"url": "   "},
            {"url": "not-a-url"},
            {"url": "javascript:alert(1)"},
            {"url": "file:///etc/passwd"},
            {"url": "http://127.0.0.1/admin"},
            {"url": "http://localhost:8000/"},
            {"url": "http://169.254.169.254/latest/meta-data/"},
            {"url": "https://user:pass@example.com/"},
            {"url": "http://127.1/admin"},  # legacy loopback form
            {"url": "https://example.com", "custom_alias": "no"},  # too short
            {"url": "https://example.com", "custom_alias": "bad/alias"},
            {"url": "https://example.com", "expires_at": "not-a-date"},
        ],
    )
    async def test_rejects_invalid_payloads(
        self, client: AsyncClient, payload: dict[str, Any]
    ) -> None:
        response = await client.post("/api/v1/urls", json=payload)
        assert response.status_code in {400, 422}, response.text
        assert "error" in response.json()

    async def test_rejects_past_expiry(self, client: AsyncClient) -> None:
        past = datetime.now(UTC) - timedelta(days=1)
        response = await client.post(
            "/api/v1/urls",
            json={"url": "https://example.com/x", "expires_at": past.isoformat()},
        )
        assert response.status_code == 422

    async def test_rejects_url_over_length_limit(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/urls",
            json={"url": "https://example.com/" + "a" * 5000},
        )
        assert response.status_code in {400, 422}

    async def test_rejects_malformed_json(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/urls",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code in {400, 422}
        assert response.json()["error"]["code"]

    async def test_error_envelope_is_consistent(self, client: AsyncClient) -> None:
        """Every error, whatever its origin, must share one shape.

        The React client branches on `error.code`, so a bare FastAPI 422 body
        or a raw string would break it.
        """
        response = await client.post("/api/v1/urls", json={"url": "javascript:alert(1)"})
        error = response.json()["error"]

        assert isinstance(error["code"], str)
        assert isinstance(error["message"], str)
        assert error["request_id"]

    async def test_validation_error_does_not_echo_the_payload(self, client: AsyncClient) -> None:
        """An error message that reflects input turns the API into an XSS relay
        for anything that renders it unescaped.
        """
        payload = {"url": "javascript:alert('<img src=x onerror=alert(1)>')"}
        response = await client.post("/api/v1/urls", json=payload)

        assert "onerror" not in response.text
        assert "<img" not in response.text


class TestRedirect:
    async def test_redirects_to_the_destination(self, client: AsyncClient) -> None:
        created = await create_url(client, "https://example.com/destination")
        response = await client.get(f"/{created['short_code']}")

        assert response.status_code == 307
        assert response.headers["location"] == "https://example.com/destination"

    async def test_redirect_is_not_cacheable_by_intermediaries(self, client: AsyncClient) -> None:
        """A cached redirect would outlive deletion and hide clicks."""
        created = await create_url(client, "https://example.com/x")
        response = await client.get(f"/{created['short_code']}")

        assert "no-store" in response.headers["cache-control"]

    async def test_unknown_code_returns_404(self, client: AsyncClient) -> None:
        response = await client.get("/doesNotExist")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_malformed_code_returns_404_without_touching_storage(
        self, client: AsyncClient
    ) -> None:
        """404 rather than 422, and rejected before any Redis/PostgreSQL call."""
        response = await client.get("/" + "a" * 40)
        assert response.status_code == 404

    @pytest.mark.parametrize("path", ["/docs", "/api", "/metrics", "/favicon.ico"])
    async def test_reserved_paths_are_not_treated_as_short_codes(
        self, app_factory: Any, path: str
    ) -> None:
        """With docs and metrics disabled these fall through to the catch-all.

        They must answer 404, not "malformed short code" -- otherwise the
        response confirms which words are reserved.
        """
        client = await app_factory(docs_enabled=False, metrics_enabled=False)
        assert (await client.get(path)).status_code == 404

    async def test_expired_link_returns_410(self, client: AsyncClient) -> None:
        """410 Gone, not 404: the link existed, and the distinction is useful.

        Rather than back-dating the row (which the
        `ck_urls_expires_after_created` constraint rightly forbids), create the
        link with a very short TTL and wait for it to lapse.
        """
        expires = datetime.now(UTC) + timedelta(seconds=2)
        created = await create_url(
            client,
            "https://example.com/expiring",
            expires_at=expires.isoformat(),
        )

        # Cache TTL is clamped to the link's expiry, so the eagerly written
        # entry lapses with it rather than outliving the link.
        await asyncio.sleep(2.5)

        response = await client.get(f"/{created['short_code']}")
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "expired"

    async def test_expired_link_metadata_reports_expired_status(self, client: AsyncClient) -> None:
        expires = datetime.now(UTC) + timedelta(seconds=2)
        created = await create_url(
            client,
            "https://example.com/expiring-meta",
            expires_at=expires.isoformat(),
        )
        await asyncio.sleep(2.5)

        body = (await client.get(f"/api/v1/urls/{created['short_code']}")).json()
        assert body["status"] == "expired"

    async def test_counts_clicks(self, app_factory: Any) -> None:
        """Counts are buffered in Redis, so read them back through the API."""
        client = await app_factory(click_flush_enabled=True)
        created = await create_url(client, "https://example.com/counted")

        for _ in range(5):
            await client.get(f"/{created['short_code']}")

        metadata = await client.get(f"/api/v1/urls/{created['short_code']}")
        assert metadata.json()["click_count"] == 5

    async def test_reports_cache_state_per_request(self, client: AsyncClient) -> None:
        created = await create_url(client, "https://example.com/cache-state")
        response = await client.get(f"/{created['short_code']}")
        assert response.headers["x-cache"] in {"hit", "miss"}


class TestMetadata:
    async def test_returns_metadata(self, client: AsyncClient) -> None:
        created = await create_url(client, "https://example.com/meta")
        response = await client.get(f"/api/v1/urls/{created['short_code']}")

        assert response.status_code == 200
        body = response.json()
        assert body["short_code"] == created["short_code"]
        assert body["original_url"] == "https://example.com/meta"
        assert body["status"] == "active"

    async def test_unknown_code_returns_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/urls/nothingHere")
        assert response.status_code == 404

    async def test_metadata_does_not_count_as_a_click(self, client: AsyncClient) -> None:
        """Inspecting a link must not inflate its analytics."""
        created = await create_url(client, "https://example.com/x")
        for _ in range(3):
            await client.get(f"/api/v1/urls/{created['short_code']}")

        body = (await client.get(f"/api/v1/urls/{created['short_code']}")).json()
        assert body["click_count"] == 0


class TestDelete:
    async def test_deletes_and_makes_the_link_unresolvable(self, client: AsyncClient) -> None:
        created = await create_url(client, "https://example.com/doomed")
        code = created["short_code"]

        assert (await client.get(f"/{code}")).status_code == 307
        assert (await client.delete(f"/api/v1/urls/{code}")).status_code == 204
        assert (await client.get(f"/{code}")).status_code == 404
        assert (await client.get(f"/api/v1/urls/{code}")).status_code == 404

    async def test_delete_invalidates_the_cache_immediately(self, client: AsyncClient) -> None:
        """The bug this guards: a warm cache entry serving a deleted link.

        The redirect below would still succeed if deletion only wrote to
        PostgreSQL, because the code was cached by the preceding request.
        """
        created = await create_url(client, "https://example.com/cached-then-deleted")
        code = created["short_code"]

        await client.get(f"/{code}")  # guarantee a warm entry
        await client.delete(f"/api/v1/urls/{code}")

        assert (await client.get(f"/{code}")).status_code == 404

    async def test_delete_is_idempotent_then_404(self, client: AsyncClient) -> None:
        created = await create_url(client, "https://example.com/x")
        code = created["short_code"]

        assert (await client.delete(f"/api/v1/urls/{code}")).status_code == 204
        assert (await client.delete(f"/api/v1/urls/{code}")).status_code == 404

    async def test_deleting_unknown_code_returns_404(self, client: AsyncClient) -> None:
        assert (await client.delete("/api/v1/urls/neverExisted")).status_code == 404

    async def test_deleted_code_is_not_reissued(self, client: AsyncClient) -> None:
        """Soft delete keeps the code retired.

        Recycling it would let a new link inherit an old link's inbound
        traffic -- a link-hijacking vector, and the reason deletion sets
        `deleted_at` instead of removing the row.
        """
        created = await create_url(client, "https://example.com/retired", custom_alias="retired-1")
        await client.delete(f"/api/v1/urls/{created['short_code']}")

        response = await client.post(
            "/api/v1/urls",
            json={"url": "https://evil.example/takeover", "custom_alias": "retired-1"},
        )
        assert response.status_code == 409


class TestList:
    async def test_lists_recent_urls_newest_first(self, client: AsyncClient) -> None:
        for index in range(5):
            await create_url(client, f"https://example.com/list/{index}")

        response = await client.get("/api/v1/urls")
        assert response.status_code == 200
        items = response.json()["items"]

        assert len(items) == 5
        timestamps = [item["created_at"] for item in items]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_respects_the_limit_parameter(self, client: AsyncClient) -> None:
        for index in range(6):
            await create_url(client, f"https://example.com/limit/{index}")

        items = (await client.get("/api/v1/urls", params={"limit": 3})).json()["items"]
        assert len(items) == 3

    async def test_excludes_deleted_urls(self, client: AsyncClient) -> None:
        created = await create_url(client, "https://example.com/gone")
        await client.delete(f"/api/v1/urls/{created['short_code']}")

        items = (await client.get("/api/v1/urls")).json()["items"]
        assert all(item["short_code"] != created["short_code"] for item in items)

    async def test_empty_list_is_a_valid_response(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/urls")
        assert response.status_code == 200
        assert response.json()["items"] == []


class TestSecurityHeaders:
    async def test_responses_carry_security_headers(self, client: AsyncClient) -> None:
        response = await client.get("/health")

        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "content-security-policy" in response.headers

    async def test_every_response_carries_a_request_id(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.headers["x-request-id"]

    async def test_client_request_id_is_propagated(self, client: AsyncClient) -> None:
        """Lets a caller correlate its own logs with ours."""
        supplied = "0123456789abcdef0123456789abcdef"
        response = await client.get("/health", headers={"X-Request-ID": supplied})
        assert response.headers["x-request-id"] == supplied

    async def test_oversized_body_is_rejected(self, app_factory: Any) -> None:
        client = await app_factory(max_request_body_bytes=1024)
        response = await client.post(
            "/api/v1/urls",
            content=b'{"url":"https://example.com/' + b"a" * 4096 + b'"}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code in {400, 413, 422}
