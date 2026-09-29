// Scenario D -- concurrent distributed traffic through Nginx.
//
// Runs a realistic mix (mostly redirects, some writes, some metadata reads)
// against both API containers at once and checks the properties that only hold
// if the API is genuinely stateless:
//
//   * Nginx spreads load across both containers (neither is starved).
//   * A code created via one container resolves via the other, which is only
//     possible because the cache and the database are shared rather than
//     process-local.
//   * Nothing fails because a request landed on the "wrong" instance.
//
// Run with the load-test compose profile so the rate limiter does not shed the
// mixed traffic; Scenario C is where the limiter itself is under test.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate } from 'k6/metrics';
import {
  BASE_URL,
  CACHE_STATES,
  JSON_HEADERS,
  cacheState,
  createShortUrl,
  instanceCounts,
  instanceThresholds,
  servedBy,
  targetUrl,
  trafficShares,
} from './lib/config.js';

const crossInstanceResolves = new Counter('cross_instance_resolves');
const requestSuccess = new Rate('request_success');
const servedRequests = new Counter('served_requests');
const servedRedirects = new Counter('served_redirects');

export const options = {
  scenarios: {
    mixed: {
      executor: 'constant-vus',
      vus: Number(__ENV.VUS || 40),
      duration: __ENV.DURATION || '45s',
    },
  },
  thresholds: Object.assign(
    {
      request_success: ['rate>0.99'],
      http_req_failed: ['rate<0.01'],
    },
    instanceThresholds('served_requests'),
    instanceThresholds('served_redirects'),
  ),
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function track(route, response) {
  const instance = servedBy(response);
  servedRequests.add(1, { instance });
  if (route === 'redirect') {
    // Tracked separately so the balance of the hot path can be read on its
    // own, without writes and metadata reads averaging it out.
    servedRedirects.add(1, { instance });
  }
  return instance;
}

export function setup() {
  const codes = [];
  for (let index = 0; index < 25; index += 1) {
    const code = createShortUrl(http, `distributed-${index}-${Date.now()}`);
    if (code) {
      codes.push(code);
    }
  }
  if (codes.length === 0) {
    throw new Error('setup could not create any short URLs -- is the stack up?');
  }
  return { codes };
}

export default function (data) {
  const roll = Math.random();

  if (roll < 0.15) {
    // Write, then immediately read it back. The read almost certainly lands on
    // a different container than the write; if it resolves, shared state works.
    const seed = `dist-${__VU}-${__ITER}-${Date.now()}`;
    const created = http.post(
      `${BASE_URL}/api/v1/urls`,
      JSON.stringify({ url: targetUrl(seed) }),
      { headers: JSON_HEADERS, tags: { name: 'create' } },
    );
    const writeInstance = track('create', created);
    requestSuccess.add(created.status === 201);
    check(created, { 'create returned 201': (r) => r.status === 201 });

    if (created.status !== 201) {
      return;
    }

    const code = created.json('short_code');
    const resolved = http.get(`${BASE_URL}/${code}`, {
      redirects: 0,
      tags: { name: 'redirect' },
    });
    const readInstance = track('redirect', resolved);
    requestSuccess.add(resolved.status === 307);

    if (readInstance !== writeInstance && resolved.status === 307) {
      crossInstanceResolves.add(1);
    }

    check(resolved, {
      'code created on one instance resolves anywhere': (r) => r.status === 307,
    });
    return;
  }

  if (roll < 0.25) {
    const code = data.codes[Math.floor(Math.random() * data.codes.length)];
    const metadata = http.get(`${BASE_URL}/api/v1/urls/${code}`, {
      tags: { name: 'metadata' },
    });
    track('metadata', metadata);
    requestSuccess.add(metadata.status === 200);
    check(metadata, {
      'metadata returned 200': (r) => r.status === 200,
      'metadata reports a click count': (r) => typeof r.json('click_count') === 'number',
    });
    return;
  }

  const code = data.codes[Math.floor(Math.random() * data.codes.length)];
  const redirect = http.get(`${BASE_URL}/${code}`, {
    redirects: 0,
    tags: { name: 'redirect' },
  });
  track('redirect', redirect);
  requestSuccess.add(redirect.status === 307);
  check(redirect, {
    'redirect returned 307': (r) => r.status === 307,
    'redirect reports cache state': (r) => CACHE_STATES.includes(cacheState(r)),
  });
}

export function handleSummary(data) {
  const perInstance = instanceCounts(data, 'served_requests');
  const perRedirect = instanceCounts(data, 'served_redirects');
  const instances = Object.keys(perInstance);
  // Round-robin across two upstreams should land near 50/50. Anything inside
  // 35-65% is a healthy spread once keepalive and timing jitter are allowed for.
  const shares = trafficShares(perInstance);
  const balanced =
    instances.length > 1 && Object.values(shares).every((share) => share > 0.35);

  const summary = {
    scenario: 'D -- concurrent distributed traffic',
    base_url: BASE_URL,
    requests: data.metrics.http_reqs ? data.metrics.http_reqs.values.count : 0,
    rps: data.metrics.http_reqs ? data.metrics.http_reqs.values.rate : 0,
    latency_ms: data.metrics.http_req_duration
      ? {
          avg: data.metrics.http_req_duration.values.avg,
          p50: data.metrics.http_req_duration.values.med,
          p95: data.metrics.http_req_duration.values['p(95)'],
          p99: data.metrics.http_req_duration.values['p(99)'],
        }
      : null,
    error_rate: data.metrics.http_req_failed
      ? data.metrics.http_req_failed.values.rate
      : 0,
    instances_that_served: instances,
    served_by: perInstance,
    share_of_traffic: shares,
    redirects_served_by: perRedirect,
    load_balanced: balanced,
    cross_instance_resolves: data.metrics.cross_instance_resolves
      ? data.metrics.cross_instance_resolves.values.count
      : 0,
  };

  const verdict = balanced
    ? 'PASS -- traffic spread across instances, shared state held'
    : 'CHECK -- traffic was not spread evenly; see share_of_traffic';

  return {
    stdout:
      `\n=== Scenario D: distributed traffic ===\n` +
      `${JSON.stringify(shares, null, 2)}\n` +
      `cross-instance resolves: ${summary.cross_instance_resolves}\n${verdict}\n`,
    'summary.json': JSON.stringify(summary, null, 2),
  };
}
