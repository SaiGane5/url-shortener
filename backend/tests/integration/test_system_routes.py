"""Operational endpoints: /health, /ready, /metrics, and the OpenAPI schema.

These are the endpoints other systems depend on -- Docker health checks, the
Nginx upstream probe, and Prometheus -- so their contracts matter as much as
the business routes.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.integration]


class TestHealth:
    async def test_liveness_is_cheap_and_always_ok(self, client: AsyncClient) -> None:
        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"]
        assert body["version"]

    async def test_reports_which_replica_answered(self, client: AsyncClient) -> None:
        """Makes load balancing observable from outside the cluster.

        docs/TESTING.md uses this to prove requests reach both replicas.
        """
        body = (await client.get("/health")).json()
        assert body["instance"] == "test-instance"

    async def test_served_by_header_is_present(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.headers["x-served-by"] == "test-instance"

    async def test_health_is_not_rate_limited(self, app_factory: Any) -> None:
        """Rate-limiting the probe would make the limiter able to kill the app.

        Under load the health check would start returning 429, the orchestrator
        would consider the container unhealthy, and it would be restarted --
        precisely when it is most needed.
        """
        client = await app_factory(
            rate_limit_enabled=True,
            rate_limit_requests=5,
            rate_limit_window_seconds=60,
            rate_limit_burst=5,
        )
        statuses = [(await client.get("/health")).status_code for _ in range(20)]
        assert set(statuses) == {200}


class TestReadiness:
    async def test_ready_when_dependencies_are_reachable(self, client: AsyncClient) -> None:
        response = await client.get("/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["dependencies"] == {"postgres": True, "redis": True}
        assert body["degraded"] is False

    async def test_readiness_is_not_rate_limited(self, app_factory: Any) -> None:
        client = await app_factory(
            rate_limit_enabled=True,
            rate_limit_requests=5,
            rate_limit_window_seconds=60,
            rate_limit_burst=5,
        )
        statuses = [(await client.get("/ready")).status_code for _ in range(20)]
        assert set(statuses) == {200}


class TestMetrics:
    async def test_exposes_prometheus_text_format(self, client: AsyncClient) -> None:
        response = await client.get("/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "# HELP" in response.text
        assert "# TYPE" in response.text

    async def test_exposes_the_metrics_the_dashboard_depends_on(self, client: AsyncClient) -> None:
        """Renaming a metric silently breaks every Grafana panel.

        scripts/verify_dashboard_queries.py checks the same contract from the
        dashboard's side; this catches it in CI without a live Prometheus.
        """
        await client.post("/api/v1/urls", json={"url": "https://example.com/x"})
        body = (await client.get("/metrics")).text

        for metric in (
            "http_requests_total",
            "http_request_duration_seconds",
            "cache_hits_total",
            "cache_misses_total",
            "cache_errors_total",
            "rate_limit_decisions_total",
            "rate_limit_violations_total",
            "urls_created_total",
            "url_redirects_total",
            "redis_errors_total",
            "postgres_errors_total",
            "dependency_latency_seconds",
            "dependency_up",
        ):
            assert metric in body, f"metric {metric} is missing"

    async def test_error_counters_are_seeded(self, client: AsyncClient) -> None:
        """A healthy process must still export its error counters.

        prometheus_client omits a labelled counter until first use, which on a
        dashboard is indistinguishable from a mistyped metric name -- both
        render "No data".

        Presence of the labelled series is the assertion, not its value: the
        registry is process-global, so other tests in the same run may have
        legitimately incremented these.
        """
        body = (await client.get("/metrics")).text

        assert 'postgres_errors_total{operation="ping"}' in body
        assert 'redis_errors_total{operation="ping"}' in body
        assert 'cache_errors_total{cache="url",operation="get"}' in body

    async def test_cache_outcome_counters_are_seeded(self, client: AsyncClient) -> None:
        """The hit-ratio panel needs both terms present, even on an idle replica.

        `hits / (hits + misses)` returns nothing at all when the series are
        absent, so a replica that has served no redirects yet blanks the panel
        instead of contributing a zero. Asserting on the labelled series rather
        than the bare name matters here: prometheus_client still emits the HELP
        and TYPE lines for an unused counter, so a substring check on
        `cache_misses_total` passes even when no series exists.
        """
        body = (await client.get("/metrics")).text

        assert 'cache_misses_total{cache="url"}' in body
        assert 'cache_hits_total{cache="url",kind="value"}' in body
        assert 'cache_hits_total{cache="url",kind="negative"}' in body

    async def test_route_labels_use_templates_not_raw_paths(self, client: AsyncClient) -> None:
        """The single most important cardinality guard in the system.

        Labelling by raw path would create one time series per short code and
        eventually take Prometheus down.
        """
        created = await client.post("/api/v1/urls", json={"url": "https://example.com/x"})
        code = created.json()["short_code"]
        await client.get(f"/{code}")

        body = (await client.get("/metrics")).text

        assert 'route="/{code}"' in body
        assert f'route="/{code}"' not in body

    async def test_unmatched_paths_collapse_to_one_label(self, client: AsyncClient) -> None:
        """404 scanning traffic must not be able to inflate cardinality.

        Paths containing a slash never match the short-code route, so without
        this they would each become their own series.
        """
        for path in ("/a/b/c", "/wp-admin/setup.php", "/../../etc/passwd"):
            await client.get(path)

        body = (await client.get("/metrics")).text
        assert "wp-admin" not in body

    async def test_no_metric_carries_a_short_code_or_url(self, client: AsyncClient) -> None:
        created = await client.post(
            "/api/v1/urls", json={"url": "https://secret.example/private?token=abc123"}
        )
        code = created.json()["short_code"]
        await client.get(f"/{code}")

        body = (await client.get("/metrics")).text

        assert "secret.example" not in body
        assert "abc123" not in body

    async def test_metrics_can_be_disabled(self, app_factory: Any) -> None:
        client = await app_factory(metrics_enabled=False)
        assert (await client.get("/metrics")).status_code == 404


class TestOpenApi:
    async def test_schema_is_served_and_documents_every_route(self, client: AsyncClient) -> None:
        response = await client.get("/openapi.json")

        assert response.status_code == 200
        paths = response.json()["paths"]

        for path in ("/api/v1/urls", "/api/v1/urls/{code}", "/{code}", "/health", "/ready"):
            assert path in paths, f"{path} is missing from the OpenAPI schema"

    async def test_documents_error_responses(self, client: AsyncClient) -> None:
        """An undocumented 429 leaves clients unprepared for the limiter."""
        schema = (await client.get("/openapi.json")).json()
        create = schema["paths"]["/api/v1/urls"]["post"]["responses"]

        assert "201" in create
        assert "422" in create
        assert "429" in create

    async def test_includes_request_and_response_examples(self, client: AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()
        components = schema["components"]["schemas"]

        assert "UrlCreateRequest" in components
        assert "UrlResponse" in components
        assert "ErrorResponse" in components

    async def test_docs_ui_is_available(self, client: AsyncClient) -> None:
        response = await client.get("/docs")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    async def test_docs_can_be_disabled_for_production(self, app_factory: Any) -> None:
        client = await app_factory(docs_enabled=False)

        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404


class TestPathHandling:
    """Trailing-slash redirects are off; see the note in `create_app`."""

    @pytest.mark.parametrize("path", ["/api/v1/urls/", "/health/", "/ready/"])
    async def test_trailing_slash_is_not_redirected(self, client: AsyncClient, path: str) -> None:
        """
        Starlette's slash redirect and nginx's prefix redirect pointed at each
        other, so `/api` bounced between them until the client gave up. Paths
        are exact now, and a wrong one gets an honest 404.
        """
        response = await client.get(path)

        assert response.status_code == 404, (
            f"{path} returned {response.status_code}; a redirect here re-opens "
            "the /api <-> /api/ loop"
        )

    async def test_unmatched_api_path_does_not_become_a_short_code(
        self, client: AsyncClient
    ) -> None:
        """`/api` must 404 rather than being resolved as a short code."""
        assert (await client.get("/api")).status_code == 404
