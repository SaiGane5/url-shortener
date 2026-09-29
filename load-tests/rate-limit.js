// Scenario C -- distributed rate limiter.
//
// Unlike the throughput scenarios, this one must run against the *production*
// rate-limit policy, so do not apply the load-test compose override here.
//
// The interesting assertion is not simply "we eventually see a 429" -- a
// per-process limiter would do that too. It is that the number of *allowed*
// requests matches a single shared budget. Traffic is spread across both API
// containers by Nginx, so an in-memory limiter would allow roughly twice the
// configured burst. Seeing approximately one budget's worth, while the
// `X-Served-By` header proves both containers answered, is what demonstrates
// the limiter state genuinely lives in Redis.
//
// Everything reported in the summary comes from engine metrics rather than
// module-level tallies, because `handleSummary()` runs in a separate JS context
// where anything accumulated during iterations is no longer visible. The
// per-request transcript is written to stdout instead, and so lands in
// k6-output.txt next to the summary.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Gauge, Rate } from 'k6/metrics';
import {
  BASE_URL,
  JSON_HEADERS,
  instanceCounts,
  instanceThresholds,
  rateLimitHeaders,
  servedBy,
  targetUrl,
} from './lib/config.js';

const allowed = new Counter('requests_allowed');
const throttled = new Counter('requests_throttled');
const servedRequests = new Counter('served_requests');
const configuredLimit = new Gauge('configured_limit');
const firstThrottledAt = new Gauge('first_throttled_at');
// Whether the limiter kept identifying this client as the same bucket. If the
// key were derived from anything instance-local, this would flip as Nginx moved
// requests between containers.
const bucketStable = new Rate('bucket_stable');

// Comfortably more than any configured burst, so the bucket is guaranteed to
// drain regardless of which policy is under test.
const REQUESTS = Number(__ENV.REQUESTS || 60);
const POLICY = __ENV.POLICY || 'write';

let firstBucket = null;
let firstThrottleRecorded = false;

export const options = {
  scenarios: {
    burst: {
      // One VU, sequential requests: the allowed/throttled counts stay
      // deterministic and the Remaining header can be checked for monotonic
      // decrease, which parallel VUs would make racy.
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: REQUESTS,
      maxDuration: '2m',
    },
  },
  thresholds: Object.assign(
    {
      // Both must be non-zero: some requests allowed, then the limiter engages.
      requests_allowed: ['count>0'],
      requests_throttled: ['count>0'],
      bucket_stable: ['rate>0.99'],
    },
    instanceThresholds('served_requests'),
  ),
};

function fire() {
  if (POLICY === 'write') {
    const seed = `ratelimit-${__VU}-${__ITER}-${Date.now()}`;
    return http.post(
      `${BASE_URL}/api/v1/urls`,
      JSON.stringify({ url: targetUrl(seed) }),
      { headers: JSON_HEADERS, tags: { name: 'ratelimit_write' } },
    );
  }
  // The read policy, exercised against a path that is cheap and always present.
  return http.get(`${BASE_URL}/api/v1/urls?limit=1`, {
    tags: { name: 'ratelimit_read' },
  });
}

export default function () {
  const response = fire();
  const headers = rateLimitHeaders(response);
  const instance = servedBy(response);

  if (headers.limit) {
    configuredLimit.add(Number(headers.limit));
  }

  if (firstBucket === null) {
    firstBucket = headers.bucket || 'missing';
    console.log(
      `policy=${headers.policy} limit=${headers.limit} bucket=${firstBucket}`,
    );
  }
  bucketStable.add((headers.bucket || 'missing') === firstBucket);

  if (response.status === 429) {
    throttled.add(1);
    if (!firstThrottleRecorded) {
      firstThrottledAt.add(__ITER);
      firstThrottleRecorded = true;
      console.log(`first 429 at request ${__ITER} (served by ${instance})`);
    }
    check(response, {
      '429 carries Retry-After': (r) => Boolean(r.headers['Retry-After']),
      '429 reports zero remaining': () => headers.remaining === '0',
      '429 body is a structured error': (r) => {
        const code = r.json('error.code');
        return code === 'rate_limited' || code === 'rate_limit_exceeded';
      },
    });
  } else {
    allowed.add(1);
    check(response, {
      'allowed request succeeded': (r) => r.status < 400,
      'carries X-RateLimit-Limit': () => Boolean(headers.limit),
      'carries X-RateLimit-Remaining': () => headers.remaining !== undefined,
      'carries X-RateLimit-Policy': () => Boolean(headers.policy),
      'carries a bucket identifier': () => Boolean(headers.bucket),
    });
  }

  servedRequests.add(1, { instance });

  console.log(
    `#${__ITER} status=${response.status} remaining=${headers.remaining} ` +
      `retry_after=${headers.retryAfter || '-'} served_by=${instance}`,
  );
}

export function handleSummary(data) {
  const allowedCount = data.metrics.requests_allowed
    ? data.metrics.requests_allowed.values.count
    : 0;
  const throttledCount = data.metrics.requests_throttled
    ? data.metrics.requests_throttled.values.count
    : 0;
  const limit = data.metrics.configured_limit
    ? data.metrics.configured_limit.values.value
    : null;

  const perInstance = instanceCounts(data, 'served_requests');
  const instances = Object.keys(perInstance);

  const summary = {
    scenario: 'C -- distributed rate limiter',
    base_url: BASE_URL,
    policy: POLICY,
    requests_sent: REQUESTS,
    allowed: allowedCount,
    throttled: throttledCount,
    configured_limit: limit,
    // The distributed-state claim, reduced to one boolean: a per-process
    // limiter behind two instances would allow ~2x the configured burst.
    allowed_within_one_shared_budget: limit ? allowedCount <= limit * 1.5 : null,
    bucket_identity_stable: data.metrics.bucket_stable
      ? data.metrics.bucket_stable.values.rate === 1
      : null,
    first_throttled_at: data.metrics.first_throttled_at
      ? data.metrics.first_throttled_at.values.value
      : null,
    instances_that_served: instances,
    served_by: perInstance,
    checks_passed: data.metrics.checks ? data.metrics.checks.values.passes : 0,
    checks_failed: data.metrics.checks ? data.metrics.checks.values.fails : 0,
  };

  const verdict =
    summary.allowed_within_one_shared_budget && instances.length > 1
      ? 'PASS -- one shared budget enforced across multiple instances'
      : instances.length <= 1
        ? 'INCONCLUSIVE -- only one instance served traffic'
        : 'FAIL -- allowed count exceeds a single shared budget';

  return {
    stdout:
      `\n=== Scenario C: rate limiter ===\n` +
      `allowed=${allowedCount} throttled=${throttledCount} limit=${limit}\n` +
      `instances=${instances.join(', ')}\n${verdict}\n`,
    'summary.json': JSON.stringify(summary, null, 2),
  };
}
