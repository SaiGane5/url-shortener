// Scenario B -- redirect throughput and cache hit ratio.
//
// This is the hot path: the one endpoint that has to be fast, and the one the
// Redis cache exists for. setup() creates a small corpus of codes, then every
// VU reads from it at random, which is what makes the cache hit ratio
// meaningful -- the first touch of each code is a miss, everything after is a
// hit, so the ratio should climb towards 1 and stay there.
//
// The same script backs the baseline-vs-optimized benchmark: with
// CACHE_ENABLED=false the stack serves every request from PostgreSQL, and the
// cache-hit threshold is disabled via -e EXPECT_CACHE=false.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import {
  BASE_URL,
  CACHE_STATES,
  cacheState,
  createShortUrl,
  instanceCounts,
  instanceThresholds,
  servedBy,
} from './lib/config.js';

const redirectLatency = new Trend('redirect_latency', true);
const redirectRequests = new Counter('redirect_requests');
const cacheHitRate = new Rate('cache_hit_ratio');
const redirectSuccess = new Rate('redirect_success');
const cacheHits = new Counter('cache_hits');
const cacheMisses = new Counter('cache_misses');
const servedRequests = new Counter('served_requests');

const CORPUS_SIZE = Number(__ENV.CORPUS_SIZE || 50);
const EXPECT_CACHE = (__ENV.EXPECT_CACHE || 'true') !== 'false';

export const options = {
  scenarios: {
    redirects: {
      executor: 'constant-arrival-rate',
      // A fixed arrival rate rather than fixed VUs: it keeps the offered load
      // identical between the baseline and optimized runs, so the comparison
      // measures latency and error rate rather than how fast each config
      // happened to spin its VUs.
      //
      // 400 req/s is a deliberately sustainable operating point. The API runs
      // one uvicorn worker per container and there are two containers, so only
      // two CPUs ever serve requests; measured saturation is around 1,300 req/s
      // (docs/PERFORMANCE.md). Sitting well below it keeps the latency
      // thresholds below meaningful as an SLO.
      //
      // To probe saturation instead: `RPS=1500 MAX_VUS=400`. Expect the p95
      // threshold to be crossed, which is the intended observation.
      rate: Number(__ENV.RPS || 400),
      timeUnit: '1s',
      duration: __ENV.DURATION || '60s',
      preAllocatedVUs: Number(__ENV.VUS || 60),
      // Capped deliberately, and low. k6 shares the Docker VM with the stack it
      // is measuring, so letting it spawn hundreds of VUs when responses slow
      // down starts a feedback spiral: the generator steals CPU from the API,
      // the API slows further, k6 spawns more VUs. Results then swing wildly
      // between runs for reasons that have nothing to do with the application.
      // With a low cap, an overloaded system shows up as dropped iterations --
      // an honest and stable signal instead.
      maxVUs: Number(__ENV.MAX_VUS || 100),
    },
  },
  thresholds: Object.assign(
    {
      'http_req_duration{name:redirect}': ['p(95)<150', 'p(99)<300'],
      redirect_success: ['rate>0.99'],
      http_req_failed: ['rate<0.01'],
    },
    // Only assert a hit ratio when the cache is actually meant to be on.
    EXPECT_CACHE ? { cache_hit_ratio: ['rate>0.90'] } : {},
    instanceThresholds('served_requests'),
  ),
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  const codes = [];
  for (let index = 0; index < CORPUS_SIZE; index += 1) {
    const code = createShortUrl(http, `redirect-corpus-${index}-${Date.now()}`);
    if (code) {
      codes.push(code);
    }
  }
  if (codes.length === 0) {
    throw new Error(
      'setup could not create any short URLs -- is the stack up and the write ' +
        'rate limit raised? See scripts/load-test.sh',
    );
  }
  return { codes };
}

export default function (data) {
  const code = data.codes[Math.floor(Math.random() * data.codes.length)];
  const response = http.get(`${BASE_URL}/${code}`, {
    // The point is to measure our redirect, not to follow it out to example.com.
    redirects: 0,
    tags: { name: 'redirect' },
  });

  redirectLatency.add(response.timings.duration);
  redirectRequests.add(1);
  redirectSuccess.add(response.status === 307);

  const state = cacheState(response);
  if (state === 'hit') {
    cacheHits.add(1);
    cacheHitRate.add(true);
  } else if (state === 'miss') {
    cacheMisses.add(1);
    cacheHitRate.add(false);
  }

  servedRequests.add(1, { instance: servedBy(response) });

  check(response, {
    'status is 307': (r) => r.status === 307,
    'has a Location header': (r) => Boolean(r.headers.Location),
    'Location points at the original URL': (r) =>
      (r.headers.Location || '').startsWith('https://example.com/'),
    'reports cache state': (r) => CACHE_STATES.includes(cacheState(r)),
  });
}

export function handleSummary(data) {
  const hits = data.metrics.cache_hits ? data.metrics.cache_hits.values.count : 0;
  const misses = data.metrics.cache_misses ? data.metrics.cache_misses.values.count : 0;

  // Report the redirect-only metrics, not the global `http_req_duration`.
  // setup() issues CORPUS_SIZE writes to build the corpus, and those are slow
  // POSTs against PostgreSQL -- folding them into the hot-path latency would
  // skew the baseline arm of the benchmark more than the optimized one, since
  // the baseline creates its corpus without the short_code index.
  const latency = data.metrics.redirect_latency;
  const requests = data.metrics.redirect_requests
    ? data.metrics.redirect_requests.values.count
    : 0;
  const summary = {
    scenario: 'B -- redirects',
    base_url: BASE_URL,
    cache_expected: EXPECT_CACHE,
    corpus_size: CORPUS_SIZE,
    offered_rps: Number(__ENV.RPS || 200),
    requests,
    rps: data.metrics.redirect_requests ? data.metrics.redirect_requests.values.rate : 0,
    latency_ms: latency
      ? {
          avg: latency.values.avg,
          p50: latency.values.med,
          p95: latency.values['p(95)'],
          p99: latency.values['p(99)'],
          max: latency.values.max,
        }
      : null,
    // Redirect failures specifically, so a hiccup during corpus creation does
    // not read as an error on the path under test.
    error_rate: data.metrics.redirect_success
      ? 1 - data.metrics.redirect_success.values.rate
      : 0,
    cache: {
      hits,
      misses,
      hit_ratio: hits + misses > 0 ? hits / (hits + misses) : null,
    },
    served_by: instanceCounts(data, 'served_requests'),
  };

  return {
    stdout: `\n=== Scenario B: redirects ===\n${JSON.stringify(summary, null, 2)}\n`,
    'summary.json': JSON.stringify(summary, null, 2),
  };
}
