# Architecture

A distributed URL shortener with a Redis-backed rate limiter, built to
demonstrate that the interesting problems in a service like this are not
"generate a random string" but concurrency, shared state, cache correctness and
failure behaviour.

---

## 1. System diagram

```text
                             ┌─────────────────┐
      Browser  ───────────▶  │  React SPA      │  static bundle, Nginx-served
                             │  (Vite build)   │
                             └────────┬────────┘
                                      │  fetch() to /api/v1/*
                                      ▼
    ┌──────────────────────────────────────────────────────────────────┐
    │  Nginx  :8080                                                    │
    │    • round-robin over 2 API replicas, passive health checks      │
    │    • routes /api/, /health, /ready, /docs, and the /{code} regex  │
    │    • /metrics restricted to private address ranges               │
    └──────────┬──────────────────────────────────┬────────────────────┘
               │                                  │
               ▼                                  ▼
    ┌──────────────────────┐          ┌──────────────────────┐
    │  api-1               │          │  api-2               │   stateless,
    │  FastAPI + uvicorn   │          │  FastAPI + uvicorn   │   interchangeable
    │  1 worker            │          │  1 worker            │
    └───┬──────────────┬───┘          └───┬──────────────┬───┘
        │              │                  │              │
        │              └──────────┬───────┘              │
        │                         │                      │
        ▼                         ▼                      ▼
  ┌───────────────────────────────────────┐   ┌────────────────────────┐
  │  Redis 7                              │   │  PostgreSQL 17         │
  │   • rate-limit token buckets (Lua)    │   │   • urls (source of    │
  │   • URL cache (cache-aside)           │   │     truth)             │
  │   • negative-cache tombstones         │   │   • UNIQUE short_code  │
  │   • pending click counters + dirty set│   │   • Alembic migrations │
  └───────────────────────────────────────┘   └────────────────────────┘
        ▲                                              ▲
        └──────────── all shared state lives here ─────┘

    ┌──────────────┐  scrapes /metrics    ┌──────────────┐
    │  Prometheus  │◀────── api-1, api-2  │  Grafana     │  provisioned
    │  :9090       │──────────────────────▶│  :3000       │  dashboard
    └──────────────┘                       └──────────────┘

    ┌──────────────┐
    │  k6          │  4 scenarios + baseline/optimized benchmark
    └──────────────┘
```

Nothing above the Redis/PostgreSQL line holds state. Any API container can
serve any request; killing one loses nothing but in-flight requests.

---

## 2. Request flows

### 2a. Creating a short URL — `POST /api/v1/urls`

```text
React form
  └─▶ Nginx ─▶ api-N
        1. RateLimitMiddleware       Redis Lua token bucket, write policy
        2. BodySizeLimitMiddleware   reject oversized bodies before parsing
        3. Pydantic                  shape, types, field limits
        4. url_validation            scheme, host, SSRF blocklist, length
        5. short_code.generate       secrets-based base62
        6. INSERT                    UNIQUE(short_code) is the arbiter
             ├── IntegrityError ─▶ regenerate and retry (longer code on repeat)
             └── success
        7. cache.set                 prime the cache on the write path
        8. 201 + Location-style payload
```

The UNIQUE constraint — not an application-level "does this code exist?" check
— is what makes concurrent creation safe. A pre-flight `SELECT` would be a
time-of-check-to-time-of-use race: two workers could both see a code as free
and both try to insert it. Letting the database reject the loser and retrying
is correct under any level of concurrency.

### 2b. Redirecting — `GET /{code}` (the hot path)

```text
Browser
  └─▶ Nginx ─▶ api-N
        1. RateLimitMiddleware   redirect policy (a looser budget than writes)
        2. validate_short_code   malformed or reserved ⇒ 404, never 422
        3. Redis GET url:v1:{code}
             ├── hit          ─▶ X-Cache: hit         (no database contact)
             ├── negative hit ─▶ 404                  (no database contact)
             └── miss
                   4. SELECT … WHERE short_code = $1 AND deleted_at IS NULL
                        ├── found     ─▶ cache.set, X-Cache: miss
                        └── not found ─▶ negative-cache tombstone, 404
        5. expired?  ─▶ 410 Gone
        6. click:  Redis INCR + SADD to the dirty set   (not a database write)
        7. 307 Temporary Redirect + Location
```

Two details are load-bearing:

- **404, not 422, for a malformed code.** Every unresolvable path returns the
  same 404 whether the code is too long, contains illegal characters, or is a
  reserved word like `docs`. Distinguishing them would leak which paths are
  reserved, and it would make `/docs` return 422 when the docs are disabled.
- **307, not 301.** A permanent redirect is cached by browsers indefinitely,
  after which deletions and expiry are unenforceable and click counts stop
  arriving. Correctness beats saving a round-trip.

### 2c. Click counting

A naive implementation writes `UPDATE urls SET click_count = click_count + 1`
on every redirect, which puts a row lock and a WAL write on the hottest path in
the system. Section 4 of [PERFORMANCE.md](PERFORMANCE.md) shows this is a large
part of why the unoptimized baseline collapses under concurrency.

Instead:

```text
redirect       ─▶ INCR clicks:pending:{code}
                  SADD clicks:dirty {code}

flusher loop   ─▶ SPOP clicks:dirty (batch)        atomically claim codes
   (every 5s,      GETDEL each pending counter     atomically read-and-reset
    per replica)   one bulk UPDATE … FROM (VALUES) one statement, many rows
                   on failure: SADD back + INCRBY restore
```

`SPOP` is what makes this safe with several replicas flushing concurrently: a
code is claimed by exactly one flusher. `GETDEL` reads and clears in one
operation, so concurrent redirects cannot have their increments silently
discarded between a `GET` and a `DEL`.

The remaining exposure is a crash between claiming a batch and persisting it,
which loses at most one interval of counts for the claimed codes. That is a
deliberate trade: click counts are analytics, and the alternative — a database
write per redirect — costs far more than the accuracy is worth. `GET
/api/v1/urls/{code}` adds any pending Redis delta to the stored count, so
reported counts are live rather than up to five seconds stale.

Setting `CLICK_FLUSH_ENABLED=false` reverts to synchronous increments, which is
how the baseline benchmark arm is configured.

---

## 3. Why each component exists

### Why Python and FastAPI?

The workload is I/O-bound: two Redis round-trips and at most one PostgreSQL
query per request. `asyncio` suits that shape, and FastAPI gives Pydantic
validation and OpenAPI generation from the same type hints that mypy checks —
one definition serving validation, documentation and static analysis.

The honest cost is visible in the benchmarks: at saturation the bottleneck is
Python CPU, not the datastores. A compiled runtime would raise per-replica
throughput. The architecture answers that by scaling out rather than up, which
is the point being demonstrated — but it is a real trade, not a free choice.

### Why React?

The frontend is a handful of stateful forms with async submission, loading and
error states. React's component model fits that, and TypeScript lets the API
response types be shared between the client and the assertions in the tests.

No state-management library is used. All server state is fetched per route and
owned by the page component; adding Redux or Zustand would introduce a second
source of truth for data the backend already owns.

### Why Redis?

Two distinct needs happen to want the same tool:

1. **Cache.** Sub-millisecond reads keep the database off the hot path.
2. **Coordination.** Rate limiting requires state shared by all replicas, with
   atomic read-modify-write. Redis provides both, and Lua scripting makes a
   multi-step token-bucket update atomic without optimistic-retry loops.

The alternative for rate limiting is PostgreSQL with `SELECT … FOR UPDATE`,
which would put a serialised write on every single request — the thing the
cache exists to avoid.

### Why PostgreSQL?

Short codes need a uniqueness guarantee under concurrency, and a UNIQUE index
is the cleanest one available. Beyond that: partial indexes for the expiry
sweeper, CHECK constraints as a backstop against service-layer bugs, and
transactions. A document store would push uniqueness enforcement into
application code, which is exactly where it is hardest to get right.

### Why cache-aside?

The access pattern is read-heavy with an unpredictable working set, and the
cache is not authoritative.

- *Read-through* would need the cache in the write path's critical section.
- *Write-through* would double write latency to keep an entry that may never be
  read.
- *Write-behind* would risk losing writes — unacceptable for the mapping
  itself.

Cache-aside keeps PostgreSQL authoritative and lets the cache be lossy. A total
Redis failure degrades latency, not correctness.

Specifics:

| | |
| --- | --- |
| Key | `url:v1:{short_code}` — the `v1` allows a format change without a flush |
| Value | JSON: `id`, `short_code`, `original_url`, `expires_at`, `created_at` |
| TTL | 300 s + 0–60 s of per-key jitter |
| Negative TTL | 30 s for tombstones |
| Invalidation | explicit `DEL` on delete, plus TTL expiry |

`click_count` is deliberately **excluded** from the cached value. It changes on
every redirect, so caching it would serve stale counts or force an invalidation
per click, defeating the cache.

**Stampede.** The jitter matters: without it, entries created together expire
together, and a burst of traffic on popular codes all misses at once. Jitter
spreads those expiries. A full single-flight lock is not used — a brief
duplicate query on one code costs one indexed lookup (~1 ms, 4 buffers), so the
extra machinery is not justified at this scale.

**Penetration.** Requests for codes that do not exist would otherwise reach the
database every time, which is a trivial denial-of-service vector. Negative
tombstones with a short TTL absorb that; the TTL is short so a code created
moments after being probed becomes visible quickly.

### Why a token bucket?

Compared with the alternatives:

| Algorithm | Why not |
| --- | --- |
| Fixed window | Boundary bursts: 2× the limit across two adjacent windows |
| Sliding window log | Stores a timestamp per request — unbounded memory per client |
| Leaky bucket | Equivalent smoothing, but shapes rather than admits; more state |
| **Token bucket** | O(1) state (two fields), allows a deliberate burst, refills smoothly |

Two properties made it the choice here:

1. **Bursts are a feature.** Loading a page fires several requests at once.
   A token bucket admits a short burst and then throttles to the sustained
   rate, which matches how clients actually behave.
2. **The state is two numbers**, so the whole update fits in one small Lua
   script and one Redis key.

The clock comes from Redis's own `TIME` command, not from the API replicas.
Otherwise clock skew between containers would make the effective limit depend
on which replica answered.

Configured policies (all environment-driven):

| Policy | Default | Applies to |
| --- | --- | --- |
| write | 20 / 60 s | `POST`, `DELETE` |
| read | 100 / 60 s | `GET /api/v1/*` |
| redirect | 1000 / 60 s | `GET /{code}` |
| exempt | — | `/health`, `/ready`, `/metrics`, `/docs` |

Health checks are exempt because rate-limiting the load balancer's probes would
let a traffic spike trigger a false unhealthy verdict and remove capacity
exactly when it is needed.

**Redis unavailable — the failure-mode decision.** Reads and redirects
**fail open**; writes **fail closed**.

The reasoning is asymmetric because the risks are. A redirect is cheap and
availability is the whole product, so serving it unlimited during a Redis
outage is better than returning 429 to everyone. Writes are the abusable path —
they consume durable storage and permanent short codes — so an unlimited write
path during an outage is a real hazard. Both are configurable
(`RATE_LIMIT_FAIL_OPEN`, `RATE_LIMIT_WRITE_FAIL_OPEN`) because the right answer
depends on whether abuse or availability is the greater concern for a given
deployment.

This is implemented as a per-policy `fail_open` attribute rather than a global
flag, so the distinction is explicit in code rather than a comment.

### Why these database indexes?

Covered in detail in [DATABASE_AND_SHARDING.md](DATABASE_AND_SHARDING.md),
including the measured `EXPLAIN ANALYZE`. In brief:

| Index | Query it serves | Why it earns its place |
| --- | --- | --- |
| `uq_urls_short_code` (UNIQUE) | every redirect and metadata read | Correctness *and* performance: enforces uniqueness under concurrency, and turns a 4731-buffer scan into a 4-buffer probe |
| `ix_urls_created_at_id` (DESC, DESC) | newest-first listing, keyset paginated | Matches the `ORDER BY` exactly, so no sort node; `id` makes the keyset stable when timestamps tie |
| `ix_urls_expires_at_active` (partial) | the expiry sweeper | `WHERE expires_at IS NOT NULL AND deleted_at IS NULL` keeps it small when most links are permanent |

No index was added on `original_url`: nothing queries by it, and it would cost
write throughput and space for nothing.

### Why stateless API replicas?

Statelessness is what makes every other scaling property available. Because no
request depends on which replica serves it:

- Nginx can use plain round-robin — no sticky sessions.
- A replica can be killed or added without draining state.
- The rate limit stays globally correct, since the budget lives in Redis.

Scenario D verifies the claim rather than asserting it: a code created via one
replica resolved via the other 4569 times in a 45-second run.

### Why Nginx?

It terminates client connections, spreads load with passive health checks, and
serves the React bundle — all without adding a language runtime. It also does
the one genuinely tricky piece of routing in this system: `/{code}` must not
shadow the SPA's client-side routes. A regex `location` matching only valid
short-code shapes, ordered after the exact and prefix matches for `/api/`,
`/docs`, `/health` and friends, resolves that ambiguity.

`/metrics` is restricted to private address ranges. It is not secret, but it is
operational detail with no reason to be on the public internet.

### Why Prometheus and Grafana?

Pull-based scraping suits replicas that come and go, and a histogram is the
only honest way to talk about latency — the benchmarks show a p50 of 2 ms
alongside a p99 of 99 ms, and an average would hide that entirely.

Labels are deliberately bounded: the **route template** (`/{code}`), never the
raw path. Using raw paths would let any client mint unbounded label
combinations and exhaust Prometheus's memory — a denial-of-service through the
monitoring system. Client IPs and URLs are likewise never labels.

Error counters are seeded to zero at startup so a healthy dashboard shows a
truthful `0` rather than an ambiguous "No data".

### Why k6?

Scenarios are JavaScript, so the assertions live with the load definition —
Scenario C checks the rate-limit *headers* while generating the burst, rather
than measuring throughput and inspecting behaviour separately. It also runs as
a container, so reproducing the benchmark needs no host installation.

---

## 4. Scaling

### To 10× traffic (~13,000 req/s redirects)

Mostly a matter of replicas, since the bottleneck is API CPU:

1. **Scale the API to ~16 replicas.** `docker compose up -d --scale api=16`
   with the Nginx upstream generated, or an ECS/Cloud Run service with the same
   image. No application change: the state is already external.
2. **Add PostgreSQL read replicas.** Cache misses and metadata reads go to
   replicas; writes stay on the primary. Needs care — a read replica can serve
   a stale row just after creation.
3. **Give Redis a replica and failover.** At 10× it is on the path for every
   request; a single node is both the hottest dependency and a single point of
   failure.
4. **Raise the cache hit ratio** with a longer TTL. The mapping is immutable
   once created, so the TTL only bounds staleness for deletes and expiry.

### To 100× traffic (~130,000 req/s redirects)

The shape of the system has to change:

1. **Shard PostgreSQL by `short_code` hash.** Every hot query is an exact-match
   lookup on the shard key, so almost all traffic stays single-shard. The full
   analysis, including why `user_id` is wrong here, is in
   [DATABASE_AND_SHARDING.md](DATABASE_AND_SHARDING.md).
2. **Redis Cluster**, sharded on the same key, so a cache lookup and a database
   lookup for one code land on one shard each.
3. **A CDN in front of redirects.** A 307 with a short `Cache-Control` would
   let edge nodes serve repeat traffic entirely, at the cost of exact click
   counts and instant deletion — a trade to make explicitly.
4. **Reconsider the runtime.** At 100× the per-request Python cost dominates
   the bill. Moving just the redirect path to a compiled service, keeping
   FastAPI for the write and admin paths, is the surgical version.
5. **Regional deployments**, since at that volume the dominant latency is the
   speed of light rather than anything in this diagram.

### Current bottlenecks

Measured, in the order they bind — see [PERFORMANCE.md](PERFORMANCE.md) §5:

1. **API CPU.** Two single-worker replicas cap redirects at ~1326 req/s.
2. **PostgreSQL writes.** One primary; creation is a synchronous `INSERT`.
3. **Redis round-trips.** Two per request, so it sets the latency floor.
4. **Nginx.** Not saturated at these rates, but a single container.

### Single points of failure

Honestly, in the current single-host Compose deployment, several:

| Component | Fails how | Blast radius |
| --- | --- | --- |
| **PostgreSQL** | single container, no replica | **Total for writes.** Cached redirects keep working; misses 503 |
| **Redis** | single container, no replica | Degraded: reads and redirects fail open and fall through to PostgreSQL; writes fail closed |
| **Nginx** | single container | Total — it is the only ingress |
| **Docker host** | single machine | Total |
| API replica | 1 of 2 | None: Nginx routes around it |

Only the API layer is genuinely redundant today. That is a deliberate scope
boundary rather than an oversight, and it is recorded as such in
[PRODUCTION_READINESS.md](PRODUCTION_READINESS.md).

---

## 5. Code layout

```text
backend/app/
  main.py               app factory, lifespan, exception handlers, middleware order
  core/                 config, structured logging, errors, metrics, request context
  api/                  routes only — no business logic
    deps.py             FastAPI dependencies reading from app.state
    routes_system.py    /health, /ready, /metrics
    routes_redirect.py  GET /{code}, registered last so it shadows nothing
    v1/routes_urls.py   the CRUD surface
  schemas/              Pydantic request/response models (the API contract)
  services/             business logic: url_service, short_code, url_validation, click_tracker
  repositories/         SQL, and nothing else
  cache/                Redis client wrapper and the cache-aside implementation
  ratelimit/            token-bucket limiter, the Lua script, and the policies
  middleware/           pure ASGI: request context, security headers, body limit, rate limit
  db/                   declarative base with a naming convention, models, session
```

The dependency direction is one-way: `api → services → repositories → db`.
Routes never touch SQL and repositories never construct HTTP responses. The one
deliberate deviation from textbook FastAPI dependency injection is that
`UrlService` holds a `Database` and opens sessions per operation, rather than
receiving a request-scoped session — that is what lets a cache-hit redirect
complete without ever opening a database connection, which is the difference
between a ~1 ms and a ~3 ms hot path.

Middleware order (outermost first) is chosen, not incidental:

```text
RequestContext   ─▶ request ID and access log wrap everything, including 429s
SecurityHeaders  ─▶ applied to every response, error responses included
CORS             ─▶ outside the limiter, so a browser can *read* a 429 body
RateLimit        ─▶ sheds load before routing or body parsing
BodySizeLimit    ─▶ rejects oversized payloads before Pydantic sees them
```

CORS has to sit outside the rate limiter: without CORS headers on a 429, the
browser blocks the response and the React app cannot show the user a rate-limit
message. Starlette also short-circuits preflight `OPTIONS` requests in the CORS
layer, so they never consume tokens.
