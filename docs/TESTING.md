# Testing guide

Written for a reviewer who wants to verify the claims in this repository rather
than take them on trust. Every command below was run against this stack and
the output shown is real.

**There is no public deployment.** No cloud credentials were available in the
build environment, so all URLs here are local. See
[section 11](#11-deployment-status) and
[FINAL_VERIFICATION.md](FINAL_VERIFICATION.md) for what deploying would
involve.

---

## 0. Start the stack

```bash
git clone <this-repo> && cd url-shortener
cp .env.example .env
cp frontend/.env.example frontend/.env
docker compose up --build
```

First build takes a few minutes. Wait for the API containers to report
healthy, then confirm:

```bash
curl -fsS http://localhost:8080/health && echo OK
```

### Where everything is

| Service | URL | Notes |
| --- | --- | --- |
| **React frontend** | <http://localhost:8080> | Same origin as the API — see below |
| API (via load balancer) | <http://localhost:8080> | Nginx over both API replicas |
| Swagger UI | <http://localhost:8080/docs> | Interactive OpenAPI |
| ReDoc | <http://localhost:8080/redoc> | |
| OpenAPI JSON | <http://localhost:8080/openapi.json> | |
| Liveness | <http://localhost:8080/health> | |
| Readiness | <http://localhost:8080/ready> | Checks PostgreSQL and Redis |
| Metrics | <http://localhost:8080/metrics> | Prometheus format |
| Prometheus | <http://localhost:9090> | |
| Grafana | <http://localhost:3000> | Credentials from `.env` |
| api-1 direct | <http://localhost:8001> | Bypasses the load balancer |
| api-2 direct | <http://localhost:8002> | Bypasses the load balancer |
| PostgreSQL | `localhost:5433` | 5433 to avoid clashing with a local server |
| Redis | `localhost:6380` | 6380 for the same reason |

**The frontend and API share one origin.** Nginx serves the React bundle at
`/` and proxies `/api/`, `/health`, `/docs` and the `/{code}` redirect to the
API replicas. The frontend container publishes no host port of its own; it is
reachable only through Nginx, exactly as in a real deployment. Same-origin
means the browser sends no cross-origin requests in the default local setup, so
CORS is configured for the case where the frontend is hosted on a separate
domain.

---

## 1. Quick smoke test

Nine steps covering the whole lifecycle. Should take under two minutes.

### 1.1 Health and readiness

```bash
curl -s http://localhost:8080/health | python3 -m json.tool
curl -s http://localhost:8080/ready  | python3 -m json.tool
```

`/health` answers if the process is alive. `/ready` additionally pings
PostgreSQL and Redis and reports each dependency.

> **Note on `/ready` semantics.** PostgreSQL down ⇒ 503 (not ready). Redis down
> ⇒ **200 with a degraded status**. That is deliberate: all replicas share one
> Redis, so removing them all from the load balancer would turn a degradation
> into an outage without helping anything.

### 1.2 Create a short URL through the API

```bash
curl -sS -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/some/very/long/url?with=params"}' \
  | python3 -m json.tool
```

Expected — HTTP 201:

```json
{
    "short_code": "9Eas5z0",
    "short_url": "http://localhost:8080/9Eas5z0",
    "original_url": "https://example.com/some/very/long/url?with=params",
    "created_at": "2026-09-04T04:35:12.481920Z",
    "expires_at": null,
    "click_count": 0,
    "status": "active"
}
```

Save the code for the rest of this section:

```bash
CODE=$(curl -sS -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/smoke-test"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["short_code"])')
echo "created /$CODE"
```

### 1.3 Follow the redirect

```bash
curl -sS -i "http://localhost:8080/$CODE" | head -20
```

Expected: **HTTP 307** with `location: https://example.com/smoke-test`, plus
`x-cache`, `x-served-by`, `x-request-id` and the `x-ratelimit-*` headers.

307 rather than 301 is deliberate: a permanent redirect is cached by browsers
indefinitely, after which deletion and expiry cannot be enforced and click
counts stop arriving.

### 1.4 Read the metadata

```bash
curl -sS "http://localhost:8080/api/v1/urls/$CODE" | python3 -m json.tool
```

`click_count` reflects the redirects performed. It includes any counts still
buffered in Redis, so it is live rather than up to five seconds stale.

### 1.5 Delete it

```bash
curl -sS -i -X DELETE "http://localhost:8080/api/v1/urls/$CODE" | head -3
```

Expected: **HTTP 204**.

### 1.6 Verify deletion took effect everywhere

```bash
curl -s -o /dev/null -w 'redirect:  %{http_code}\n' "http://localhost:8080/$CODE"
curl -s -o /dev/null -w 'metadata:  %{http_code}\n' "http://localhost:8080/api/v1/urls/$CODE"
```

Expected: **404** for both. This also proves cache invalidation works — the
entry was cached by step 1.3, and delete evicted it rather than waiting for the
TTL. Repeat the redirect a few times: still 404, from every replica.

### 1.7 Error handling

```bash
# Not a valid URL
curl -s -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' -d '{"url":"not-a-url"}' | python3 -m json.tool

# A blocked scheme
curl -s -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' -d '{"url":"javascript:alert(1)"}' | python3 -m json.tool

# An internal target (SSRF)
curl -s -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' -d '{"url":"http://169.254.169.254/latest/meta-data/"}' \
  | python3 -m json.tool

# A permissive IPv4 form of loopback -- must also be blocked
curl -s -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' -d '{"url":"http://127.1/admin"}' | python3 -m json.tool

# A code that does not exist
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/doesnotexist
```

Every error uses the same envelope, with a `request_id` that appears in the
logs:

```json
{
    "error": {
        "code": "validation_error",
        "message": "URL failed validation",
        "details": {"reason": "scheme_not_allowed"},
        "request_id": "0fd7aea3d11dc5360e5de5b5d811fada"
    }
}
```

The last one returns **404**, not 422, and so do over-long and reserved codes
(`/docs`, `/api`). Distinguishing them would leak which paths are reserved.

### 1.8 The API docs

Open <http://localhost:8080/docs>. Every endpoint carries descriptions,
schemas, example payloads, error responses and the rate-limit policy that
applies. Requests can be issued from the page.

### 1.9 Browser check

Open <http://localhost:8080>, paste a URL, submit. Full frontend script in
[section 7](#7-frontend).

---

## 2. Rate limiter

### 2.1 Trigger a 429

The write policy is the strictest (20 per 60 s by default), so it is quickest
to exhaust. Clear the buckets first so the burst starts from a full budget:

```bash
docker compose exec redis redis-cli --scan --pattern 'rl:*' | xargs -r docker compose exec -T redis redis-cli DEL

for i in $(seq 1 30); do
  printf '%2d  ' "$i"
  curl -s -o /dev/null -X POST http://localhost:8080/api/v1/urls \
    -H 'Content-Type: application/json' \
    -d "{\"url\":\"https://example.com/rl-$i\"}" \
    -w 'status=%{http_code}  remaining=%header{x-ratelimit-remaining}  served_by=%header{x-served-by}\n'
done
```

Expected: roughly the first 20 return `201` with `remaining` counting down to
0, then every subsequent request returns `429`.

### 2.2 What to check on the 429

```bash
curl -s -i -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com/blocked"}' \
  | grep -iE 'HTTP/|x-ratelimit|retry-after'
```

| Header | Meaning |
| --- | --- |
| `x-ratelimit-limit` | The configured budget, e.g. `20` |
| `x-ratelimit-remaining` | `0` when throttled |
| `x-ratelimit-reset` | Seconds until the bucket refills fully |
| `x-ratelimit-policy` | e.g. `20;w=60;burst=20;policy=write;algorithm=token_bucket` |
| `x-ratelimit-bucket` | A hash of the client identity — see below |
| `retry-after` | Present **only** on 429 |

Body: `{"error": {"code": "rate_limited", …}}`.

> `x-ratelimit-reset` may look small relative to the window. With a 20/60 s
> budget the refill rate is one token every 3 s, and `reset` reports the time
> to refill, not the wall-clock end of a fixed window — the limiter is a token
> bucket, not a fixed window.

### 2.3 Confirm Redis is enforcing a *shared* limit

This is the part that distinguishes a real distributed limiter from a
per-process one. The bucket identity is exposed as a header so it can be looked
up directly:

```bash
BUCKET=$(curl -s -o /dev/null -D- http://localhost:8080/api/v1/urls \
  | grep -i '^x-ratelimit-bucket:' | tr -d '\r' | cut -d' ' -f2)
echo "bucket=$BUCKET"

# The token bucket itself, in Redis -- one key, shared by both replicas
docker compose exec redis redis-cli --scan --pattern "rl:*$BUCKET*"
docker compose exec redis redis-cli HGETALL "$(docker compose exec -T redis redis-cli --scan --pattern "rl:*$BUCKET*" | head -1 | tr -d '\r')"
```

The identity is hashed rather than stored as a raw IP, so Redis holds no
personal data while the limit stays debuggable.

**The decisive test** — exhaust the budget through the load balancer, then
check whether a *specific* replica will still serve a write:

```bash
docker compose exec redis redis-cli --scan --pattern 'rl:*' | xargs -r docker compose exec -T redis redis-cli DEL

# Drain the budget through Nginx (hits both replicas)
for i in $(seq 1 25); do
  curl -s -o /dev/null -X POST http://localhost:8080/api/v1/urls \
    -H 'Content-Type: application/json' -d "{\"url\":\"https://example.com/drain-$i\"}"
done

# Now ask each replica directly. Both must refuse: the budget is shared.
for port in 8001 8002; do
  printf 'api on :%s -> ' "$port"
  curl -s -o /dev/null -X POST "http://localhost:$port/api/v1/urls" \
    -H 'Content-Type: application/json' -d '{"url":"https://example.com/after-drain"}' \
    -w '%{http_code}\n'
done
```

Expected: **429 from both**. With an in-memory limiter each replica would have
its own budget, and this step would return 201.

The automated version of this test is Scenario C:

```bash
./scripts/load-test.sh rate-limit
```

Measured result (`docs/performance/runs/full-suite/c-rate-limit/summary.json`):

```text
Sent: 60   Allowed: 20   Throttled: 40
Configured limit (from X-RateLimit-Limit): 20
Allowed within a single shared budget: True
Instances that served traffic: ['api-1', 'api-2']
```

Exactly 20 allowed against a limit of 20, while both replicas served traffic. A
per-process limiter would have allowed about 40.

### 2.4 How the frontend shows it

Submit the form on <http://localhost:8080> repeatedly (about 20 times) until
throttled. The UI shows a specific rate-limit message with the retry delay
taken from `Retry-After`, not a generic failure. This works because CORS sits
*outside* the rate limiter in the middleware stack — without CORS headers on
the 429 the browser would block the response and the app could not read it.

---

## 3. Distributed scaling

Verifying that this is real:

```text
   api-1  ─┐
            ├─  Redis (shared limiter + cache)  ─  PostgreSQL
   api-2  ─┘
```

### 3.1 Both replicas serve traffic

```bash
for i in $(seq 1 20); do
  curl -s -o /dev/null -D- http://localhost:8080/health | grep -i '^x-served-by:'
done | sort | uniq -c
```

Expected: roughly 10 each. Measured in Scenario D: **2251 / 2250** — Nginx
round-robin over two upstreams.

### 3.2 Shared cache: created on one replica, resolved by the other

```bash
# Create directly against api-1, bypassing the load balancer
CODE=$(curl -sS -X POST http://localhost:8001/api/v1/urls \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/cross-instance"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["short_code"])')

# Resolve directly against api-2
curl -s -o /dev/null -D- "http://localhost:8002/$CODE" \
  | grep -iE '^(location|x-cache|x-served-by):'
```

Expected: `307`, the correct `location`, `x-served-by: api-2`, and
`x-cache: hit` — api-2 never saw the write, so the entry can only have come
from shared Redis.

Scenario D counts this automatically: **4569 cross-instance resolves** in a
45-second run.

### 3.3 No process-local state: kill a replica mid-traffic

```bash
docker compose stop api-1

# Traffic keeps flowing through the survivor
for i in $(seq 1 10); do
  curl -s -o /dev/null -w '%{http_code} ' http://localhost:8080/health
done; echo

CODE=$(curl -sS -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com/one-replica"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["short_code"])')
curl -s -o /dev/null -w 'redirect: %{http_code}\n' "http://localhost:8080/$CODE"

docker compose start api-1
sleep 5
# The code created while api-1 was down resolves on api-1 with no special action
curl -s -o /dev/null -D- "http://localhost:8001/$CODE" | grep -i '^x-served-by:'
```

Expected: all 200s during the outage (Nginx routes around the dead upstream
after `max_fails`), and the new code resolves on the restarted replica
immediately — it holds no state, so there is nothing to rebuild.

### 3.4 Per-replica metrics

```bash
open http://localhost:3000    # dashboard: "URL shortener — service overview"
```

Panels **Request rate per replica** and **In-flight requests per replica** show
two series. In Prometheus directly:

```promql
sum(rate(http_requests_total[1m])) by (replica)
```

### 3.5 Structured logs, correlated by request ID

```bash
docker compose logs api-1 --tail 5 | tail -1 | python3 -m json.tool
```

Each line is JSON with `timestamp`, `level`, `request_id`, `method`, `route`,
`status`, `duration_ms` and `replica`. Note `route` is the *template*
(`/{code}`), never the raw path — raw paths in a metric label would let any
client exhaust Prometheus's memory.

URLs are never logged in full. They are reduced to host plus a truncated hash,
because query strings can carry tokens and personal data.

---

## 4. Cache

### 4.1 Demonstrate miss then hit

Creation primes the cache, so the first read of a new code is already a hit.
To *see* a miss, evict the key:

```bash
CODE=$(curl -sS -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com/cache-demo"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["short_code"])')

printf '1. right after create:           '
curl -sS -o /dev/null -D- "http://localhost:8080/$CODE" | grep -i '^x-cache:'

docker compose exec -T redis redis-cli DEL "url:v1:$CODE"

printf '2. after deleting the cache key: '
curl -sS -o /dev/null -D- "http://localhost:8080/$CODE" | grep -i '^x-cache:'

printf '3. next request:                 '
curl -sS -o /dev/null -D- "http://localhost:8080/$CODE" | grep -i '^x-cache:'
```

Actual output:

```text
code=9Eas5z0
1. right after create:           x-cache: hit
2. after deleting the cache key: x-cache: miss
3. next request:                 x-cache: hit
```

Step 2 is the miss: it queried PostgreSQL and repopulated Redis, which is why
step 3 is a hit again.

### 4.2 Inspect the cache directly

```bash
docker compose exec redis redis-cli KEYS 'url:v1:*' | head
docker compose exec redis redis-cli GET  "url:v1:$CODE" | python3 -m json.tool
docker compose exec redis redis-cli TTL  "url:v1:$CODE"
```

Two things to notice:

- **`click_count` is absent** from the cached value. It changes on every
  redirect, so caching it would serve stale counts or force an invalidation per
  click.
- **The TTL is 300–360 s**, not exactly 300. Per-key jitter stops entries
  created together from expiring together and causing a synchronised stampede.

### 4.3 Negative caching

```bash
curl -s -o /dev/null -w 'first:  %{http_code}\n' http://localhost:8080/nosuchcode
docker compose exec redis redis-cli KEYS 'url:v1:nosuchcode*'
curl -s -o /dev/null -w 'second: %{http_code}\n' http://localhost:8080/nosuchcode
```

A tombstone is stored with a 30 s TTL, so repeated probes for absent codes do
not reach the database every time.

### 4.4 Where to see the metrics

```bash
curl -s http://localhost:8080/metrics | grep -E '^cache_(hits|misses|errors)_total'
```

In Prometheus:

```promql
sum(rate(cache_hits_total[5m])) / (sum(rate(cache_hits_total[5m])) + sum(rate(cache_misses_total[5m])))
```

Grafana panels: **Cache hit ratio** (stat), **Cache hits vs misses**
(timeseries), **Cache hit ratio over time**.

Measured hit ratio in the benchmark: **1.0000** (4501 hits, 0 misses). Genuine
but flattered by the workload — 50 codes read repeatedly, with creation
priming the cache. See [PERFORMANCE.md](PERFORMANCE.md) §4.

---

## 5. Database and indexes

### 5.1 Inspect the schema

```bash
docker compose exec postgres psql -U urlshort -d urlshort -c '\d urls'
```

### 5.2 List the indexes with sizes and usage

```bash
docker compose exec postgres psql -U urlshort -d urlshort -c "
SELECT indexrelname AS index,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size,
       idx_scan AS scans
FROM pg_stat_user_indexes
WHERE relname = 'urls'
ORDER BY pg_relation_size(indexrelid) DESC;"
```

Actual output on a 250,000-row table:

```text
           index           |    size    | scans
---------------------------+------------+-------
 ix_urls_created_at_id     | 11 MB      |     0
 uq_urls_short_code        | 7168 kB    |   538
 pk_urls                   | 5696 kB    |    12
 ix_urls_expires_at_active | 8192 bytes |     0
```

Two things worth pausing on:

- `ix_urls_expires_at_active` is **8 KB versus 11 MB**. It is a *partial* index
  (`WHERE expires_at IS NOT NULL AND deleted_at IS NULL`), so it only stores
  rows that can ever match. A full index would hold a quarter of a million NULL
  entries the sweeper can never use.
- `ix_urls_created_at_id` shows **0 scans**. On current usage it does not pay
  for itself; it is retained because the alternative for the listing endpoint
  is a full scan plus a sort that degrades without bound. Stated plainly rather
  than hidden — see [DATABASE_AND_SHARDING.md](DATABASE_AND_SHARDING.md) §2.

### 5.3 EXPLAIN ANALYZE — with and without the index

```bash
CODE=$(docker compose exec -T postgres psql -U urlshort -d urlshort -tAc \
  "SELECT short_code FROM urls WHERE deleted_at IS NULL LIMIT 1;" | tr -d '[:space:]')

docker compose exec postgres psql -U urlshort -d urlshort -c "
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, short_code, original_url, expires_at, created_at, click_count
FROM urls WHERE short_code = '$CODE' AND deleted_at IS NULL;"
```

**With the index** (measured, 250,000 rows):

```text
Index Scan using uq_urls_short_code on urls  (cost=0.42..8.44 rows=1 width=112)
                                             (actual time=1.129..1.130 rows=1 loops=1)
  Index Cond: ((short_code)::text = '298Np3p'::text)
  Buffers: shared hit=1 read=3
Execution Time: 1.194 ms
```

To see the counterfactual, drop the constraint and repeat (`./scripts/benchmark.sh`
does this automatically and restores it via a trap):

```bash
docker compose exec postgres psql -U urlshort -d urlshort -c \
  "ALTER TABLE urls DROP CONSTRAINT uq_urls_short_code;"
# ... re-run the EXPLAIN above ...
docker compose exec postgres psql -U urlshort -d urlshort -c \
  "ALTER TABLE urls ADD CONSTRAINT uq_urls_short_code UNIQUE (short_code);"
```

**Without the index** (measured):

```text
Gather  (cost=1000.00..7033.18 rows=1 width=112) (actual time=12.850..14.761 rows=1 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=4731
  ->  Parallel Seq Scan on urls  (actual time=10.067..10.068 rows=0 loops=3)
        Rows Removed by Filter: 83333
Execution Time: 14.888 ms
```

**What to look for.** Not primarily the 12.5× time difference —
`Buffers: shared hit=4731` versus `4`. The scan reads ~37 MB of pages *per
lookup* and recruits two parallel workers, so one redirect briefly occupies
three cores. That is why baseline latency collapses under concurrency instead
of staying near 15 ms.

Also look for `Index Scan` versus `Seq Scan`, and `Rows Removed by Filter:
83333` per worker — 250,000 rows examined to return one.

### 5.4 Constraints are real, not decorative

```bash
docker compose exec postgres psql -U urlshort -d urlshort -c \
  "UPDATE urls SET click_count = -1 WHERE id = (SELECT min(id) FROM urls);"

docker compose exec postgres psql -U urlshort -d urlshort -c \
  "INSERT INTO urls (short_code, original_url) VALUES ('bad!!', 'https://example.com');"
```

Both are rejected — by `ck_urls_click_count_non_negative` and
`ck_urls_short_code_charset`.

### 5.5 Migrations match the models

```bash
cd backend && alembic upgrade head && alembic current
```

An integration test applies the real migration to a throwaway database and
diffs it against the ORM metadata, so a model change without a migration fails
the build:

```bash
cd backend && pytest tests/integration/test_schema_and_indexes.py -v
```

---

## 6. Automated test suites

### 6.1 Backend

Needs PostgreSQL and Redis reachable (the compose stack provides both):

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock.txt -r requirements-dev.lock.txt

ruff check app tests           # lint
ruff format --check app tests  # formatting
mypy app tests                 # types
pytest tests/ -q               # everything
```

Measured result: **324 passed**, zero lint findings, zero type errors.

By suite:

```bash
pytest tests/unit -q                                  # no external dependencies
pytest tests/integration -q                           # real PostgreSQL + Redis
pytest tests/integration/test_concurrency.py -v       # races
pytest tests/integration/test_failure_modes.py -v     # 17 dependency-failure tests
pytest tests/integration/test_rate_limiter.py -v      # limiter against real Redis
pytest tests/integration/test_schema_and_indexes.py -v # schema contract + query plans
```

What the notable ones actually assert:

| Test file | Verifies |
| --- | --- |
| `test_concurrency.py` | Concurrent creation yields distinct codes; buffered clicks reach PostgreSQL durably |
| `test_failure_modes.py` | Redis down ⇒ redirects still work, writes 503; PostgreSQL down ⇒ 503 not 500 |
| `test_rate_limiter.py` | Token-bucket refill, the write policy being stricter than read, header correctness |
| `test_schema_and_indexes.py` | Every expected index and constraint exists; redirects use the index; listing does not sort; the migration matches the models |
| `test_url_validation.py` | SSRF blocklist, including permissive IPv4 forms like `127.1` |
| `test_short_code.py` | Collision probability matches the documented table |

### 6.2 Frontend

```bash
cd frontend
npm ci
npm run lint
npm run format:check
npm run typecheck
npm run test
npm run build
```

Measured result: **65 tests passed**, clean lint, clean types, successful build.

Covers form rendering, client-side validation, successful submission, API error
handling, rate-limit (429) handling, copy-to-clipboard, loading states and the
generated-URL display.

### 6.3 CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs the same
commands on push and PR: `backend`, `frontend`, `docker` (image builds, compose
validation, and a stack smoke test asserting both replicas serve traffic) and
an advisory `security` job. Nothing depends on a developer's machine.

---

## 7. Frontend

Open <http://localhost:8080>.

| What to test | How | Expected |
| --- | --- | --- |
| Client-side validation | Submit empty; submit `not a url` | Inline message, no request sent; the field is marked invalid and described by the error |
| Scheme inference | Enter `example.com` | Auto-prefixed to `https://` |
| Loading state | Submit and watch the button | Disabled, spinner, no double submit |
| Success state | Submit a valid URL | Short URL shown with copy and open controls |
| Copy to clipboard | Click **Copy** | Confirmation appears; paste elsewhere to verify. Falls back to `execCommand` where the Clipboard API is unavailable |
| Open the link | Click the short URL | Redirects to the destination |
| Metadata page | Visit `/urls/<code>` | Short code, original URL, created, expires, click count, status |
| Not found | Visit `/urls/nosuchcode` | Friendly not-found state, not a stack trace |
| API error | `docker compose stop api-1 api-2`, then submit | Actionable error message, retry affordance for degraded errors |
| Rate limiting | Submit ~20 times quickly | Specific rate-limit message including the retry delay |
| Keyboard only | Tab through and submit with Enter | Every control reachable; focus visible throughout |
| Screen-reader semantics | Inspect the form | Labels tied to inputs, `aria-invalid`, `aria-describedby`, errors in a live region |
| Responsive layout | Resize to 375 px | Single column, nothing clipped, targets remain tappable |
| No secrets | View source / devtools | Only `VITE_API_BASE_URL`; no keys |

The API base URL comes from `VITE_API_BASE_URL` (see
`frontend/.env.example`) — no production URL is hard-coded.

---

## 8. Load testing

Requires only Docker; k6 runs in a container on the compose network.

```bash
./scripts/load-test.sh --help          # options
./scripts/load-test.sh all             # all four scenarios (~4 min)
./scripts/load-test.sh redirects       # just one
```

Results are written to `docs/performance/runs/<timestamp>/` with a summary
table, per-scenario JSON and the raw k6 output.

| Scenario | Script | What it tests |
| --- | --- | --- |
| A — creation | `load-tests/create-urls.js` | Write path: validation, code generation, INSERT, cache priming |
| B — redirects | `load-tests/redirects.js` | Hot path throughput, latency and cache hit ratio |
| C — rate limiter | `load-tests/rate-limit.js` | 429 behaviour, header correctness, shared budget across replicas |
| D — distributed | `load-tests/distributed.js` | Mixed traffic, load distribution, cross-instance resolution |

### Measured results

From `docs/performance/runs/full-suite/`:

| Scenario | Requests | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
| --- | --: | --: | --: | --: | --: | --: |
| A — URL creation | 54,522 | 778.9 | 12.89 | 104.82 | 225.18 | 0.0000 |
| B — redirects | 11,714 | 194.4 | 1.65 | 9.78 | 199.00 | 0.0000 |
| D — mixed distributed | 68,183 | 1,509.2 | 20.49 | 74.09 | 130.32 | 0.0000 |

Scenario C is pass/fail rather than throughput: 60 sent, 20 allowed against a
limit of 20, 40 throttled, both replicas serving.

### Interpreting the output

- **RPS** — achieved throughput. Scenarios B and D use a fixed arrival rate, so
  achieved ≈ offered means the system kept up; achieved well below offered
  means saturation.
- **p95 / p99** — the tail is what users notice. A p50 of 1.65 ms with a p99 of
  199 ms in Scenario B is a real signal: most requests are fast, a few are much
  slower.
- **Errors** — should be 0.0000. Anything else needs explaining.
- **`served_by`** — should be roughly even. Skew means load balancing is off.
- **Cache hit ratio** — should approach 1.0 on a repeated corpus.

To probe saturation deliberately:

```bash
RPS=1500 MAX_VUS=400 ./scripts/load-test.sh redirects
```

Measured ceiling: **~1326 req/s** with p95 774 ms, p99 1704 ms and zero errors
— the system queues rather than failing.

> **A caveat worth reading before trusting any of these numbers.** k6 shares
> the Docker VM with the stack it measures, and the host used for these runs
> was also running unrelated work (load average 6.4–20.4 on 8 cores). Two
> identical back-to-back runs differed by 3–7×. This is why the benchmark
> interleaves its arms and reports medians. See
> [PERFORMANCE.md](PERFORMANCE.md) §6.

---

## 9. Performance comparison — baseline vs optimized

```bash
./scripts/benchmark.sh                              # defaults: 100 req/s, 3 repeats (~6 min)
SEED_ROWS=250000 REPEATS=3 RPS=800 ./scripts/benchmark.sh   # saturation
```

What it does, automatically:

1. Seeds the table to 250,000 rows (idempotent — skipped if already there).
2. **Baseline arm:** `CACHE_ENABLED=false`, `CLICK_FLUSH_ENABLED=false`, and
   the UNIQUE index on `short_code` dropped in SQL.
3. **Optimized arm:** cache on, index restored, clicks buffered.
4. Alternates the arms for the configured number of repeats, reports medians.
5. Captures `EXPLAIN (ANALYZE, BUFFERS)` for both configurations.
6. Restores the index from a trap, so an interrupted run still leaves the
   schema correct.

Results land in `docs/performance/benchmarks/<timestamp>/README.md`.

### Measured: controlled load, 100 req/s, median of 3

| Version | RPS | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
| --- | --: | --: | --: | --: | --: | --: |
| Baseline | 98.5 | 173.35 | 100.99 | 530.74 | 1083.96 | 0.0000 |
| Optimized | 99.5 | 4.72 | 2.28 | 5.39 | 99.11 | 0.0000 |

p50 latency: **−97.7%** (44× faster). Throughput is near-identical *and that is
expected* — with a fixed arrival rate both arms absorb 100 req/s, so the
difference can only show in latency.

### Measured: saturation, 800 req/s, median of 2

| Version | RPS | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
| --- | --: | --: | --: | --: | --: | --: |
| Baseline | 89.6 | 1662.43 | 1563.58 | 3499.72 | 5256.91 | 0.0004 |
| Optimized | 777.3 | 11.86 | 1.09 | 49.38 | 264.60 | 0.0000 |

Throughput: **+767.8%** (8.7×). The baseline saturates at roughly an eighth of
the offered load with multi-second latency; the optimized configuration absorbs
777 of 800 at a 1.09 ms p50.

Verify the arms really differed:

```bash
grep -h load_average docs/performance/benchmarks/*/*/conditions.txt
cat docs/performance/benchmarks/controlled-100rps/explain-baseline.txt
cat docs/performance/benchmarks/controlled-100rps/explain-optimized.txt
```

---

## 10. Failure testing

### 10.1 Redis down

```bash
docker compose stop redis
```

| Behaviour | Expected |
| --- | --- |
| `GET /{code}` (cached or not) | **Works.** 200/307. `x-cache: error`, falls through to PostgreSQL |
| `POST /api/v1/urls` | **503** `dependency_unavailable` — the write limiter **fails closed** |
| `/health` | 200 |
| `/ready` | **200, degraded.** Redis is reported unhealthy but the replica stays in service |
| Click counts | Fall back to direct database increments |
| Frontend | Shows a degraded-service message with a retry affordance |

```bash
CODE=<an existing code>
curl -s -o /dev/null -D- "http://localhost:8080/$CODE" | grep -iE '^(HTTP|x-cache)'
curl -s -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com/no-redis"}' \
  -w '\nstatus=%{http_code}\n'
curl -s http://localhost:8080/ready | python3 -m json.tool
docker compose start redis
```

Why writes fail closed but reads fail open: a redirect is cheap and
availability is the product; writes consume durable storage and permanent short
codes, so an unlimited write path during an outage is a real hazard. Both are
configurable — see [ARCHITECTURE.md](ARCHITECTURE.md) §3.

### 10.2 PostgreSQL down

```bash
docker compose stop postgres
```

| Behaviour | Expected |
| --- | --- |
| `GET /{code}` **cached** | **Works** — served from Redis, never touches the database |
| `GET /{code}` **not cached** | **503** `dependency_unavailable` |
| `POST /api/v1/urls` | **503** |
| `/ready` | **503** — PostgreSQL is required |

This is the clearest demonstration of the cache's value: cached redirects keep
working with the database completely gone.

```bash
docker compose start postgres
```

### 10.3 One API replica down

```bash
docker compose stop api-1
for i in $(seq 1 10); do curl -s -o /dev/null -w '%{http_code} ' http://localhost:8080/health; done; echo
docker compose start api-1
```

Expected: all 200s. Nginx marks the upstream failed after `max_fails` and
routes to the survivor. Grafana's **Replicas up** panel drops to 1.

### 10.4 Frontend container down

```bash
docker compose stop frontend
curl -s -o /dev/null -w 'SPA:        %{http_code}\n' http://localhost:8080/
curl -s -o /dev/null -w 'API health: %{http_code}\n' http://localhost:8080/health
docker compose start frontend
```

Expected: the SPA route returns **502** (Nginx has no upstream to serve the
bundle from) while `/health` and every other API route keep returning **200**.
Because Nginx routes by path, losing the frontend cannot affect the API — and
API clients such as `curl` or an integration are unaffected by a frontend
outage.

### 10.5 Expired and nonexistent links

```bash
# Expires in 2 seconds
CODE=$(curl -sS -X POST http://localhost:8080/api/v1/urls \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"https://example.com/short-lived\",\"expires_at\":\"$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=2)).isoformat().replace("+00:00","Z"))')\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["short_code"])')

curl -s -o /dev/null -w 'before expiry: %{http_code}\n' "http://localhost:8080/$CODE"
sleep 3
curl -s -o /dev/null -w 'after expiry:  %{http_code}\n' "http://localhost:8080/$CODE"
curl -s -o /dev/null -w 'nonexistent:   %{http_code}\n' http://localhost:8080/zzzzzzz
```

Expected: `307`, then **410 Gone** (distinct from 404 — the link existed and
has expired), then `404`.

### 10.6 What Prometheus and Grafana should show

| Failure | Panel / query |
| --- | --- |
| Redis down | **Dependency errors** rises; `redis_errors_total` increments; **Cache hit ratio** collapses |
| PostgreSQL down | **Dependency errors** rises; `postgres_errors_total` increments; **5xx error rate** rises |
| Replica down | **Replicas up** → 1; **Request rate per replica** loses a series |
| Rate limiting | **429 / sec** rises; **Rate-limit decisions by policy** shows throttled |

Error counters are seeded to zero at startup, so a healthy dashboard shows a
truthful `0` rather than an ambiguous "No data".

---

## 11. Deployment status

**Not deployed publicly.** No cloud credentials were available, and inventing
URLs would be worse than saying so.

What exists instead:

- A complete, verified local deployment — the entire stack runs with
  `docker compose up --build` and every check in this document passes against it.
- Production Compose configuration and exact, reproducible deployment
  instructions in [FINAL_VERIFICATION.md](FINAL_VERIFICATION.md) §11, including
  HTTPS via Caddy with automatic certificates.

---

## 12. One-command verification

```bash
./scripts/verify.sh
```

Runs the full gate — backend lint, types and tests; frontend lint, types, tests
and build; Docker builds; stack startup; health, readiness, CRUD, cache, rate
limit and multi-instance checks — and prints a pass/fail table. Results are
recorded in [FINAL_VERIFICATION.md](FINAL_VERIFICATION.md).
