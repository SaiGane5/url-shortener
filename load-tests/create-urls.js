// Scenario A -- URL creation throughput.
//
// Exercises the write path end to end: Pydantic validation, SSRF checks,
// collision-safe short-code generation, the INSERT, and cache priming. Every
// iteration writes a unique row, so this is the scenario that shows what
// PostgreSQL write throughput and connection-pool sizing actually cost us.
//
// Run the stack with the load-test profile first (scripts/load-test.sh does
// this), otherwise the 20-writes/minute production policy sheds nearly all of
// this traffic and the numbers measure the rate limiter instead of the API.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import {
  BASE_URL,
  JSON_HEADERS,
  instanceCounts,
  instanceThresholds,
  servedBy,
  targetUrl,
} from './lib/config.js';

const createLatency = new Trend('url_create_latency', true);
const createdTotal = new Counter('urls_created');
const rateLimited = new Counter('rate_limited_responses');
const createSuccess = new Rate('url_create_success');
const servedRequests = new Counter('served_requests');

export const options = {
  scenarios: {
    creation: {
      executor: 'ramping-vus',
      startVUs: 5,
      stages: [
        { duration: __ENV.RAMP_DURATION || '15s', target: Number(__ENV.VUS || 30) },
        { duration: __ENV.DURATION || '45s', target: Number(__ENV.VUS || 30) },
        { duration: '10s', target: 0 },
      ],
      gracefulRampDown: '10s',
    },
  },
  thresholds: Object.assign(
    {
      // Writes hit PostgreSQL synchronously, so the budget is looser than reads.
      'http_req_duration{name:create}': ['p(95)<500', 'p(99)<1000'],
      url_create_success: ['rate>0.99'],
      http_req_failed: ['rate<0.01'],
    },
    instanceThresholds('served_requests'),
  ),
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  const seed = `${__VU}-${__ITER}-${Date.now()}`;
  const response = http.post(
    `${BASE_URL}/api/v1/urls`,
    JSON.stringify({ url: targetUrl(seed) }),
    { headers: JSON_HEADERS, tags: { name: 'create' } },
  );

  createLatency.add(response.timings.duration);
  createSuccess.add(response.status === 201);

  if (response.status === 429) {
    rateLimited.add(1);
  }
  if (response.status === 201) {
    createdTotal.add(1);
  }
  servedRequests.add(1, { instance: servedBy(response) });

  check(response, {
    'status is 201': (r) => r.status === 201,
    'returns a short code': (r) => Boolean(r.json('short_code')),
    'returns the short URL': (r) => Boolean(r.json('short_url')),
    'echoes the original URL': (r) => Boolean(r.json('original_url')),
    'click count starts at zero': (r) => r.json('click_count') === 0,
    'carries rate-limit headers': (r) => Boolean(r.headers['X-Ratelimit-Limit']),
  });
}

export function handleSummary(data) {
  const latency = data.metrics.url_create_latency;
  const summary = {
    scenario: 'A -- URL creation',
    base_url: BASE_URL,
    requests: data.metrics.http_reqs ? data.metrics.http_reqs.values.count : 0,
    rps: data.metrics.http_reqs ? data.metrics.http_reqs.values.rate : 0,
    latency_ms: latency
      ? {
          avg: latency.values.avg,
          p50: latency.values.med,
          p95: latency.values['p(95)'],
          p99: latency.values['p(99)'],
          max: latency.values.max,
        }
      : null,
    error_rate: data.metrics.url_create_success
      ? 1 - data.metrics.url_create_success.values.rate
      : 0,
    urls_created: data.metrics.urls_created ? data.metrics.urls_created.values.count : 0,
    rate_limited: data.metrics.rate_limited_responses
      ? data.metrics.rate_limited_responses.values.count
      : 0,
    served_by: instanceCounts(data, 'served_requests'),
  };

  return {
    stdout: `\n=== Scenario A: URL creation ===\n${JSON.stringify(summary, null, 2)}\n`,
    'summary.json': JSON.stringify(summary, null, 2),
  };
}
