// Shared configuration and helpers for every k6 scenario.
//
// Everything is driven by environment variables (`-e NAME=value`) so the same
// script can be pointed at the local stack, a staging deployment, or a single
// API container directly, without editing code.

/** Base URL under test. Defaults to the Nginx load balancer in docker-compose. */
export const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';

/** A long, realistic target URL. Length matters: it is what gets stored and cached. */
export function targetUrl(seed) {
  return (
    `https://example.com/catalog/${seed}/product` +
    `?utm_source=loadtest&utm_campaign=k6&ref=${seed}`
  );
}

/**
 * JSON POST with the headers the API expects.
 *
 * `responseCallback` is set so that 4xx codes we deliberately provoke (notably
 * 429 in the rate-limiter scenario) are not counted as `http_req_failed`. Each
 * scenario asserts the statuses it cares about with explicit checks instead.
 */
export const JSON_HEADERS = { 'Content-Type': 'application/json' };

/** Parse the rate-limit headers the API attaches to every response. */
export function rateLimitHeaders(response) {
  return {
    limit: response.headers['X-Ratelimit-Limit'],
    remaining: response.headers['X-Ratelimit-Remaining'],
    reset: response.headers['X-Ratelimit-Reset'],
    policy: response.headers['X-Ratelimit-Policy'],
    bucket: response.headers['X-Ratelimit-Bucket'],
    retryAfter: response.headers['Retry-After'],
  };
}

/** Which API container served a response, from the header Nginx passes through. */
export function servedBy(response) {
  return response.headers['X-Served-By'] || 'unknown';
}

/**
 * The API instances we expect to see, for load-distribution assertions.
 * Override with `-e INSTANCES=api-1,api-2,api-3` when scaling out.
 */
export const INSTANCES = (__ENV.INSTANCES || 'api-1,api-2').split(',');

/** `unknown` is included so a missing header shows up rather than vanishing. */
export const TRACKED_INSTANCES = [...INSTANCES, 'unknown'];

/**
 * Thresholds that make per-instance counts visible to `handleSummary()`.
 *
 * `handleSummary()` runs in its own JS context, so module-level tallies built
 * up during iterations are always empty by the time it runs -- only engine
 * metrics cross that boundary. Tagged counters do cross it, but a tagged
 * submetric is only materialised in the summary if something references it, so
 * each one gets a trivially-true threshold purely to bring it into existence.
 */
export function instanceThresholds(metric) {
  const thresholds = {};
  for (const instance of TRACKED_INSTANCES) {
    thresholds[`${metric}{instance:${instance}}`] = ['count>=0'];
  }
  return thresholds;
}

/** Reads the per-instance counts back out of a k6 summary. */
export function instanceCounts(data, metric) {
  const counts = {};
  for (const instance of TRACKED_INSTANCES) {
    const submetric = data.metrics[`${metric}{instance:${instance}}`];
    const count = submetric ? submetric.values.count : 0;
    if (count > 0) {
      counts[instance] = count;
    }
  }
  return counts;
}

/** Fraction of traffic each instance served, from `instanceCounts()`. */
export function trafficShares(counts) {
  const total = Object.values(counts).reduce((sum, value) => sum + value, 0);
  const shares = {};
  for (const [instance, count] of Object.entries(counts)) {
    shares[instance] = total > 0 ? count / total : 0;
  }
  return shares;
}

/** Whether a redirect was served from Redis or fell through to PostgreSQL. */
export function cacheState(response) {
  return response.headers['X-Cache'] || 'unknown';
}

/**
 * Every value `X-Cache` can carry (app/cache/url_cache.py::CacheState).
 *
 * `disabled` is what the baseline benchmark arm reports, and `error` is what a
 * Redis outage reports, so both are legitimate rather than signs of a broken
 * run -- only `unknown`, meaning the header was absent entirely, is a failure.
 */
export const CACHE_STATES = ['hit', 'negative_hit', 'miss', 'disabled', 'error'];

/**
 * Create one short URL and return its code, or null on failure.
 *
 * Used by scenarios that need a corpus to read against. Deliberately tolerant:
 * setup() runs once and a single failure should surface as a clear error rather
 * than an exception mid-iteration.
 */
export function createShortUrl(http, seed) {
  const response = http.post(
    `${BASE_URL}/api/v1/urls`,
    JSON.stringify({ url: targetUrl(seed) }),
    { headers: JSON_HEADERS, tags: { name: 'setup_create' } },
  );
  if (response.status !== 201) {
    return null;
  }
  return response.json('short_code');
}
