# Engineering decisions

Decisions where a reasonable engineer could have chosen differently, recorded
with what was actually at stake in *this* application. Several were changed
part-way through because testing or measurement contradicted the first choice;
those are marked and the reason kept.

---

## 1. FastAPI and Python for the backend

**Problem.** Serve a read-heavy HTTP API where each request performs two Redis
round-trips and at most one PostgreSQL query.

**Options.** FastAPI + uvicorn (async); Django REST Framework; Flask +
Gunicorn (sync workers); Litestar.

**Chosen.** FastAPI on uvicorn, one worker per container.

**Reason.** The workload is I/O-bound with tiny payloads, which is the shape
`asyncio` handles well: a worker awaiting Redis can serve other requests
instead of occupying a thread. Sync Flask would need a worker per concurrent
request to reach comparable concurrency. Django brings an ORM, admin and
migration stack whose value is mostly in the parts this project does not have
(users, sessions, admin views). The decisive practical factor is that Pydantic
models serve three purposes from one definition — runtime validation, the
OpenAPI schema, and something mypy can check — so the API contract cannot drift
from the documentation.

**Trade-offs.** Real and measured. At saturation the bottleneck is Python CPU,
not the datastores: ~1326 req/s for redirects across two replicas, with
PostgreSQL near-idle. A compiled runtime would raise per-replica throughput
several-fold. Async also makes blocking mistakes expensive — one synchronous
call in a handler stalls the whole event loop for that replica, a failure mode
that does not exist with sync workers.

**Future alternatives.** If per-request CPU became the cost driver, move only
the redirect path to a compiled service and keep FastAPI for writes and admin.
The split is clean because the redirect path shares nothing but Redis and the
`urls` table.

---

## 2. One uvicorn worker per container, not several

**Problem.** Eight cores are available; two single-worker containers use two.

**Options.** (a) One worker per container, scale by adding containers. (b)
Several uvicorn workers per container. (c) Gunicorn managing uvicorn workers.

**Chosen.** (a).

**Reason.** `prometheus_client` keeps counters in process memory. Multiple
workers behind one `/metrics` endpoint means several disagreeing registries and
whichever worker answers the scrape reports only its own slice. Fixing that
needs the multiprocess collector plus a shared writable scratch directory —
which also conflicts with the read-only root filesystem the containers run
with. Beyond metrics, one worker per container makes "a container" and "a unit
of capacity" the same thing, which is what keeps `X-Served-By` and per-replica
metrics meaningful, and is the model a container orchestrator expects.

**Trade-offs.** Six of eight cores sit unused in the local deployment, and the
measured ceiling is lower than the hardware could deliver. Scaling needs
container orchestration rather than a config change.

**Future alternatives.** `--scale api=N` behind a generated Nginx upstream is
the immediate answer, which is how the design intends capacity to be added.

---

## 3. React with no state-management library

**Problem.** Manage server data and UI state across a create page and a details
page.

**Options.** Redux Toolkit; Zustand; TanStack Query; React hooks only.

**Chosen.** Hooks only, with one centralized `apiClient`.

**Reason.** There is almost no *client* state — no cross-page shared mutable
data, no optimistic updates, no offline queue. What looks like state is server
data fetched per route and owned by one page component. A store would create a
second source of truth for data the backend already owns, and cache
invalidation bugs in the frontend for a system whose whole point is that the
backend is authoritative.

TanStack Query was the closest call; it would genuinely help with retries and
caching. It was rejected because with two data-fetching call sites its
abstraction exceeds the problem.

**Trade-offs.** No request deduplication or background refetching. Loading and
error state is handled per page, which would become repetitive at ten pages.

**Future alternatives.** TanStack Query is the right answer the moment a third
page fetches server data, or a list needs background refresh.

---

## 4. PostgreSQL as the source of truth

**Problem.** Store link mappings with a hard uniqueness guarantee under
concurrent creation.

**Options.** PostgreSQL; MySQL; DynamoDB; MongoDB; Redis alone.

**Chosen.** PostgreSQL 17.

**Reason.** A UNIQUE index is the cleanest concurrency primitive available for
this problem: it makes collision handling correct without application locking
or a compare-and-set loop (see §7). PostgreSQL also supplies the two features
that materially shaped the schema — partial indexes, which make the expiry
sweeper's index 8 KB instead of 11 MB, and CHECK constraints as a backstop
against service-layer bugs.

Redis alone was rejected outright: the mapping is durable data, and an
`allkeys-lru` cache is explicitly allowed to discard it. DynamoDB would fit the
key-value access pattern well and shard without effort, but conditional writes
are a weaker and more awkward uniqueness guarantee than a UNIQUE index, and
adopting it would trade an operational dependency for a problem this project
does not have yet.

**Trade-offs.** A single primary is the write ceiling and the largest single
point of failure. Sharding is real work, planned but not done — see
[DATABASE_AND_SHARDING.md](DATABASE_AND_SHARDING.md).

---

## 5. Redis for both caching and rate limiting

**Problem.** Two needs: keep the database off the hot path, and hold rate-limit
state shared by all replicas with atomic updates.

**Options.** Redis for both; Memcached for cache + Redis for limits; PostgreSQL
for limits; in-memory per-replica limits.

**Chosen.** One Redis for both.

**Reason.** In-memory limits are disqualified by the requirement: with two
replicas behind round-robin, a per-process limiter allows roughly twice the
configured budget. Scenario C demonstrates the difference concretely — 60
requests against a limit of 20 yielded exactly 20 allowed while both replicas
served traffic. A per-process limiter would have allowed ~40.

PostgreSQL could hold the counters but would put a serialised write on every
request, which is precisely what the cache exists to prevent. Memcached is a
fine cache but has no scripting, so it cannot do the atomic multi-step token
bucket. Running one datastore rather than two is then simply less to operate.

**Trade-offs.** Redis is now on the path for every request and is a single
point of failure. Cache pressure and limiter state share one memory budget, so
an `allkeys-lru` eviction storm could evict limiter keys — which fails open for
reads by design, but is worth knowing.

**Future alternatives.** Separate instances (or logical databases) once either
workload is large enough that their failure domains should not be shared.

---

## 6. Token bucket, with Redis's clock

**Problem.** Distributed rate limiting that behaves correctly with concurrent
requests across replicas.

**Options.** Fixed window; sliding window log; sliding window counter; leaky
bucket; token bucket.

**Chosen.** Token bucket, in a Lua script, using Redis `TIME`.

**Reason.** Fixed windows permit a 2× burst across a window boundary. A sliding
window log is exact but stores a timestamp per request, so memory grows with
traffic — an attacker can inflate it deliberately. A token bucket keeps two
numbers per client, admits a deliberate short burst (which matches how browsers
behave: several requests on page load, then quiet), and refills smoothly.

Two implementation details carry most of the weight:

- **Lua, not `MULTI`/`WATCH`.** Read tokens, compute refill, decide, write
  back must be atomic. A `WATCH`/retry loop would livelock under contention on
  a hot key; a Lua script runs atomically on the server in one round-trip.
- **Redis's `TIME`, not the replica's clock.** Otherwise clock skew between
  containers makes the effective limit depend on which replica answered. Using
  the shared datastore's clock removes an entire class of bug that is very
  hard to diagnose in production.

A subtlety worth recording: Lua truncates numbers to integers, so
`retry_after` is returned as a string to preserve its fractional part while the
remaining token count is floored.

**Trade-offs.** A burst allowance means short spikes above the sustained rate
are permitted by design — correct here, wrong for protecting a hard downstream
quota. The bucket is keyed by client IP, so clients behind one NAT share a
budget.

**Future alternatives.** Per-API-key buckets once authentication exists, which
also removes the NAT problem.

---

## 7. Collisions arbitrated by the UNIQUE index, not a pre-flight check

**Problem.** Generate short codes that are unique under concurrent creation.

**Options.** (a) `SELECT` to check availability, then `INSERT`. (b) `INSERT`
and handle `IntegrityError`. (c) Pre-generate a pool of unused codes. (d)
Encode a monotonic counter.

**Chosen.** (b), retrying up to 5 times and widening the code on later
attempts.

**Reason.** Option (a) is a time-of-check-to-time-of-use race — two workers can
both see a code as free and both attempt it — and it adds a query to every
creation to defend against something that happens roughly once in 35,000
inserts at 100 million links. Option (b) is correct at any concurrency because
the database arbitrates, and it costs nothing on the overwhelmingly common
success path. Option (d) was rejected on security grounds, not performance:
sequential codes are enumerable, and the code is the only access control a link
has.

**Trade-offs.** A collision costs a wasted round-trip. Correct handling depends
on distinguishing a `short_code` uniqueness violation from any other
`IntegrityError`, which couples the service slightly to database error
semantics.

**Verification.** An integration test creates URLs concurrently and asserts
every short code is distinct; a unit test pins the documented collision table
so the numbers in the docs cannot drift from the implementation.

---

## 8. Click counts buffered in Redis, not written per redirect

**Problem.** Count clicks without putting a write on the hottest read path.

**Options.** (a) `UPDATE … SET click_count = click_count + 1` per redirect. (b)
Redis counter, flushed periodically. (c) Append to a log, aggregate offline.

**Chosen.** (b): `INCR` a pending counter plus `SADD` to a dirty set, flushed
every 5 s by a background task in each replica.

**Reason.** Option (a) is the obvious implementation and measurably the reason
the unoptimized baseline collapses: a row lock and a WAL write on every
redirect turn a read into a read-plus-write, and under concurrency the row
locks serialise on popular links — precisely the links with the most traffic.
The buffered version keeps redirects read-only against PostgreSQL and turns
thousands of increments into one bulk `UPDATE`.

The distributed-safety details are the substance:

- **`SPOP` to claim a batch.** With several replicas flushing, `SPOP` gives a
  code to exactly one of them. Reading the set and then deleting would let two
  replicas double-apply the same delta.
- **`GETDEL` to read-and-reset.** Atomic, so increments arriving during the
  flush are not silently discarded between a `GET` and a `DEL`.
- **Restore on failure.** If the database write fails, codes go back into the
  dirty set and the deltas are re-added with `INCRBY`.

**Trade-offs.** Counts are approximate. A replica crashing between claiming a
batch and persisting it loses up to one interval of counts for those codes.
This is accepted deliberately: click counts are analytics, and the alternative
costs far more than the accuracy is worth. To keep the *reported* number honest
rather than up to five seconds stale, `GET /api/v1/urls/{code}` adds any
pending Redis delta to the stored count.

**Future alternatives.** Option (c) — Kafka or Kinesis into a warehouse — once
per-click analytics (referrer, geography, device) are wanted. At that point the
event stream is the product and the counter is a by-product.

---

## 9. Cache-aside, and what is deliberately not cached

**Problem.** Serve redirects without touching PostgreSQL on every request.

**Options.** Cache-aside; read-through; write-through; write-behind.

**Chosen.** Cache-aside, TTL 300 s + 0–60 s jitter, with negative caching.

**Reason.** PostgreSQL must stay authoritative and the cache must be allowed to
be lossy, so a total Redis failure degrades latency rather than correctness.
Write-behind is disqualified for the mapping itself — losing a write means
losing a link. Write-through would double write latency to populate entries
that may never be read.

Three specifics matter more than the pattern name:

- **`click_count` is excluded from the cached value.** It changes on every
  redirect, so caching it would either serve stale counts or force an
  invalidation per click, which defeats the cache entirely.
- **Jittered TTL.** Without jitter, entries created together expire together
  and a burst on popular codes all misses at once. Jitter spreads the
  expiries. A full single-flight lock was considered and rejected: a duplicate
  query costs one indexed lookup (~1 ms, 4 buffers), which is cheaper than the
  machinery to prevent it.
- **Negative caching (30 s).** Without it, requests for codes that do not
  exist reach the database every time — a trivial denial-of-service vector. The
  TTL is short so a code created moments after being probed becomes visible
  quickly.
- **A `v1` prefix in the key.** Changing the serialized shape later means
  bumping to `v2` rather than flushing the cache or, worse, deserialising an
  old shape into a new struct.

**Trade-offs.** A deleted link can be served from cache for up to the TTL — so
delete invalidates explicitly rather than waiting. Cache and database can
diverge briefly; acceptable because entries are immutable apart from deletion.

---

## 10. Soft delete, accepting unbounded growth

**Problem.** `DELETE /api/v1/urls/{code}` must stop a link resolving.

**Options.** (a) Hard delete. (b) Soft delete via `deleted_at`.

**Chosen.** (b).

**Reason.** A hard delete returns the code to the generatable keyspace. Because
codes are random, a later creation could be issued that same code, and every
link previously shared, bookmarked or printed would then silently point at
somebody else's destination. That is a link-hijacking vector that arrives
through no fault of whoever published the original link.

This decision was revisited mid-project when considering whether the expiry
sweeper should hard-delete, and the same reasoning applied — so the sweeper
soft-deletes too, and its method is named `soft_delete_expired` to make that
unambiguous at the call site.

**Trade-offs.** Rows are never reclaimed, so the table grows without bound.
That is the same trade every URL shortener makes by promising links keep
working. Every read path must remember `deleted_at IS NULL`, which is a
correctness footgun mitigated by keeping all SQL in `repositories/`.

**Future alternatives.** Archive cold rows to cheaper storage while retaining
the short codes, so the keyspace stays reserved without the rows staying hot.

---

## 11. Rate limiting as pure ASGI middleware

**Problem.** Where should the limiter run?

**Options.** (a) FastAPI dependency per route. (b) `BaseHTTPMiddleware`. (c)
Pure ASGI middleware.

**Chosen.** (c).

**Reason.** Middleware sheds load before routing, validation or body parsing,
which is the point of a limiter — a dependency runs after FastAPI has already
done that work, and has to be remembered on every new route. `BaseHTTPMiddleware`
was rejected for overhead: it wraps each request in an anyio task group and
buffers the body through a stream, which is measurable on a path where the
whole request budget is ~1 ms.

The consequence is that the limiter runs *before* routing, so it cannot know
the matched route. Policy selection is therefore keyed on HTTP method and path
prefix — write, read, redirect, exempt — which is deterministic and needs no
route resolution.

**Trade-offs.** Path-prefix classification must be kept in step with the URL
structure; a new `/api/v2/` prefix would need a rule. Being outside the routing
layer also means a 429 is returned before FastAPI's own error handling, so the
envelope had to be constructed in the middleware to stay consistent with every
other error response.

---

## 12. Nginx for load balancing and static serving

**Problem.** Distribute traffic across replicas and serve the React bundle.

**Options.** Nginx; Traefik; Caddy; HAProxy; an application-level balancer.

**Chosen.** Nginx 1.27, round-robin with passive health checks.

**Reason.** It does both jobs — reverse proxy and static file server — without
adding a runtime, and it handles the one genuinely tricky piece of routing
here: `/{code}` must not shadow the SPA's client-side routes. Nginx's location
precedence (exact, then prefix, then ordered regex) lets `/api/`, `/docs`,
`/health` win before a regex that matches only valid short-code shapes.

Round-robin was chosen over `least_conn` deliberately: with two identical
stateless replicas and uniformly cheap requests, it distributes evenly and
makes the load-balancing *demonstrable* — the near-exact 2251/2250 split in the
benchmark is easier to reason about than `least_conn`'s adaptive behaviour.

**Trade-offs.** One Nginx container is a single point of failure and the sole
ingress. Passive health checks only notice a dead replica after a request has
already failed against it; active health checks are an Nginx Plus feature.
Scaling replicas needs the upstream block regenerated, since the open-source
build has no dynamic service discovery.

**Future alternatives.** A cloud load balancer (ALB, Cloud Run) removes the
single point of failure and adds active health checks; Traefik reads container
labels if dynamic scaling matters more than familiarity.

---

## 13. Docker Compose rather than Kubernetes

**Problem.** Run eight services locally and make the stack reproducible.

**Options.** Docker Compose; Kubernetes (kind/minikube); Podman; scripts.

**Chosen.** Compose, with `docker compose up --build` as the single entry point.

**Reason.** The requirement is a reviewer running one command and getting a
working stack. Compose expresses everything needed — service dependencies with
health conditions, an internal network, per-service environment — in one
readable file. Kubernetes would add manifests, an ingress controller and a
local cluster runtime to demonstrate exactly the same distributed-systems
properties, obscuring them behind YAML that is mostly about Kubernetes.

Two override files keep the benchmark honest without duplicating the stack:
`docker-compose.loadtest.yml` raises the rate limits so throughput scenarios
measure the application rather than the limiter, and
`docker-compose.benchmark.yml` parameterises the cache and click-write settings
so the two benchmark arms differ only in the setting under test.

**Trade-offs.** No self-healing, no rolling deploys, no autoscaling, and
single-host. `--scale` conflicts with the fixed host port bindings used to make
individual replicas directly inspectable.

**Future alternatives.** ECS or Cloud Run for a managed deployment from the
same images. Kubernetes when there is a reason beyond appearances.

---

## 14. Fail open for reads, fail closed for writes

**Problem.** What should happen when Redis is unavailable and the limiter
cannot make a decision?

**Options.** Fail open everywhere; fail closed everywhere; per-policy.

**Chosen.** Per-policy: reads and redirects fail open, writes fail closed.

**Reason.** The risks are asymmetric, so one global answer must be wrong for
half the endpoints. A redirect is cheap and availability is the entire product —
returning 429 to every visitor because a cache is down converts a degradation
into an outage. Writes are the abusable path: they consume durable storage and
permanently allocate short codes, so an unlimited write path during an outage
is a genuine hazard.

An earlier draft used one global toggle. It was replaced with a per-policy
`fail_open` attribute because the single flag made a security-relevant
distinction implicit — the code read as though one answer fit everything.

**Trade-offs.** During a Redis outage, link creation returns 503 even though
PostgreSQL is healthy. That is a deliberate choice of safety over availability
on the write path, and it is configurable
(`RATE_LIMIT_WRITE_FAIL_OPEN=true`) for demo deployments where the opposite
trade is preferred.

**Verification.** Integration tests point the app at a dead Redis port and
assert redirects still resolve while creation returns 503.

---

## 15. 404 for malformed short codes, not 422

**Problem.** What should `GET /{code}` return for a code that is too long,
contains illegal characters, or is a reserved word like `docs`?

**Options.** (a) 422 with details. (b) 404 for everything unresolvable.

**Chosen.** (b) on the lookup path; 422 is retained for *custom alias
creation*, where the caller genuinely needs to know what was wrong.

**Reason.** This started as (a) and was changed because of two concrete
problems found in testing. Distinguishing "malformed" from "not found" leaks
which paths are reserved, letting a client enumerate internal route names. And
with the docs disabled in production, `/docs` fell through to the catch-all
redirect route and returned 422 — advertising that `docs` is special, when
404 is exactly right.

The rule is now: on the lookup path every unresolvable path is 404. On the
creation path, where the user is choosing an alias and a helpful message is the
whole point, malformed input is 422 with a specific reason.

A related fix: FastAPI's `Path(max_length=16)` was intercepting long codes and
returning 422 before the validator ran, so the constraint was removed from the
route signature and length is enforced in `validate_short_code` alongside every
other rule — one place, one consistent answer.

**Trade-offs.** A developer mistyping a code gets no explanation. Acceptable:
the audience for `GET /{code}` is a browser following a link, not a developer
debugging a payload.

---

## 16. A naming convention on the SQLAlchemy metadata

**Problem.** The test suite builds its schema from ORM metadata for speed;
production builds it with Alembic. The two disagreed on constraint names.

**Options.** (a) Name every constraint explicitly in the models. (b) Set a
`naming_convention` on the `MetaData`. (c) Have tests run migrations instead.

**Chosen.** (b), plus a test that applies the real migration to a throwaway
database and diffs it against the metadata.

**Reason.** This was found by a test asserting the expected set of indexes.
`unique=True` on a column let PostgreSQL name it `urls_short_code_key`, while
the migration created `uq_urls_short_code`; the primary key was `urls_pkey`
versus `pk_urls`. Two concrete costs follow: `alembic revision --autogenerate`
emits spurious drop/create pairs for functionally identical constraints, and
any future migration referring to a constraint by name works in one environment
and fails in the other.

A convention fixes it at the source, so both construction paths produce
identical names by default rather than by discipline. Option (c) would also
work but would make every test run pay for migrations.

**Trade-offs.** The `ck` convention composes with the given name, so check
constraints in models *and* migrations must be written as suffixes
(`click_count_non_negative`, not `ck_urls_click_count_non_negative`) or they
become `ck_urls_ck_urls_…`. That is a genuine trap — and the migration-diff
test caught it immediately when the migration still used full names, which is
why the test exists rather than just the convention.

---

## 17. `UrlService` owns a `Database`, not a request-scoped session

**Problem.** A cache-hit redirect should not open a database connection at all,
but FastAPI's idiomatic pattern injects a session per request.

**Options.** (a) Inject a request-scoped `AsyncSession`. (b) Give the service a
`Database` and open sessions per operation.

**Chosen.** (b).

**Reason.** With (a), every redirect acquires a pooled connection whether or
not it needs one — and at a 1.0 cache hit ratio, almost none of them do. That
wastes pool capacity precisely on the highest-volume path, and pool capacity is
the resource that has to be shared across replicas
(`pool_size × replicas < max_connections`).

**Trade-offs.** A deliberate deviation from the documented FastAPI pattern,
which is a cost in familiarity. Transaction scope becomes the service's
responsibility rather than the framework's.

---

## 18. Testing against real PostgreSQL and Redis

**Problem.** Integration tests need datastores.

**Options.** Mock the clients; use fakes (`fakeredis`, SQLite); use real
containers.

**Chosen.** Real containers, with unit tests kept dependency-free.

**Reason.** The behaviour most worth testing here only exists in the real
implementations: `IntegrityError` on a UNIQUE violation, `SPOP`/`GETDEL`
atomicity, Lua script execution, `EXPLAIN` plans. SQLite has no partial
indexes and different constraint semantics; `fakeredis` does not execute Lua
the way Redis does. A test suite passing against fakes would prove very little
about the parts of this system that are actually hard.

**Trade-offs.** Tests need Docker running, are slower, and needed real care
around fixtures — `pytest-asyncio` assigns a function-scoped event loop, so
session-scoped async fixtures become bound to a dead loop. The resolution was
function-scoped fixtures guarded by module-level flags so expensive DDL runs
once per session without crossing loops.

---

## 19. Structured JSON logging, hand-rolled

**Problem.** Emit machine-parseable logs with request correlation, without
leaking sensitive data.

**Options.** `python-json-logger`; `structlog`; a custom formatter.

**Chosen.** A custom `logging.Formatter`, roughly 60 lines.

**Reason.** The requirement that drove this is redaction, not JSON. URLs are
user-submitted and may carry tokens or personal data in query strings, so they
must never be logged whole. Owning the formatter means redaction is not
optional or bolted on: `safe_url()` reduces a URL to scheme, host and a
truncated hash, and it is the only way a URL reaches a log line. `structlog` is
excellent but is a larger dependency and a different logging idiom for what is
ultimately one formatter and a `contextvar`.

**Trade-offs.** Code to maintain that a library would have provided. No
processor pipeline or log-level-based sampling.

**A bug worth recording.** The first `safe_url()` used `urlsplit().netloc`,
which *includes userinfo* — so `https://user:secret@example.com/` would have
logged the password. It was rewritten to compose the output from `scheme`,
`hostname` and `port` explicitly, so credentials cannot survive by
construction rather than by remembering to strip them. A test asserts it.

---

## 20. SSRF validation without DNS resolution

**Problem.** The API accepts arbitrary URLs. Should it block private and
internal destinations?

**Options.** Allow anything; block literal private addresses and internal
hostnames; additionally resolve DNS and check the resolved address.

**Chosen.** Block literal private/loopback/link-local addresses and internal
hostname suffixes. **No DNS resolution at validation time.**

**Reason.** The threat model matters here, and it is narrower than classic
SSRF. This service never fetches the target itself — it only issues a 307. So
the risk is not our server being tricked into reaching internal
infrastructure; it is a *victim's browser* being redirected to an internal
address, which makes a shortened link a convenient wrapper for an attack on
someone's private network. Blocking literal private addresses and names like
`localhost` addresses that.

DNS resolution was rejected on two grounds: it adds a network call, with its
latency and failure modes, to every write; and it does not actually close the
hole, because DNS rebinding means the answer at validation time need not match
the answer at click time. Adding cost without closing the gap is worse than
not adding it, so the boundary is documented instead of half-defended.

**A bypass found and fixed.** The first implementation used
`ipaddress.ip_address()`, which only parses strict dotted-quad form. That let
`http://127.1/admin`, `http://0x7f000001/` and other permissive IPv4 forms slip
through as "hostnames" and reach the blocklist unexamined — while browsers
resolve them to loopback perfectly well. The fix normalises through
`socket.inet_aton()` before the private-range check, and tests cover the legacy
forms explicitly. It is a good illustration of why a blocklist keyed on parsing
is fragile: the parser must agree with the *consumer's* parser, not with the
specification.

**Trade-offs.** A URL whose hostname resolves to a private address still
passes. Legitimate uses on private networks are blocked unless explicitly
allowed via configuration.

---

## 21. Frontend/backend separation with no shared schema tooling

**Problem.** Keep TypeScript types honest against the Python API contract.

**Options.** Generate a client from the OpenAPI schema; share types via a
generator; hand-write TypeScript interfaces.

**Chosen.** Hand-written interfaces in one `types.ts`, consumed by one
`apiClient`.

**Reason.** With five endpoints and a small stable payload shape, a generation
step adds a build dependency and a regeneration ritual for types that fit on
one screen. Concentrating all `fetch` calls in one client means the types have
exactly one place to be wrong, and the component tests exercise the client
against mocked responses shaped like the real ones.

**Trade-offs.** The types can drift from the backend and nothing will fail the
build. That is the honest weakness of this choice; the mitigating factor is
that the CI stack smoke test exercises the real endpoints, so a contract change
that matters breaks something visible.

**Future alternatives.** `openapi-typescript` against the live schema in CI is
the correct answer as soon as the surface grows or more than one client
consumes it.
