# Production readiness

An honest assessment of what this system does today and what it would still
need before it carried real traffic for a real business.

The distinction used throughout:

- **IMPLEMENTED** — built, running, and covered by a test or a command in
  [TESTING.md](./TESTING.md) that a reviewer can run.
- **PARTIAL** — works, but with a limitation stated explicitly.
- **FUTURE** — deliberately not built. The reasoning is given, because "we
  didn't get to it" and "we decided against it for now" are different things.

Nothing here is marked implemented unless `./scripts/verify.sh` exercises it or
a named test covers it. The last recorded run is in
`docs/performance/last-verification.tsv`.

---

## Summary

| Area | Status |
| --- | --- |
| API validation | IMPLEMENTED |
| Frontend validation | IMPLEMENTED |
| Authentication | FUTURE — deliberate, see below |
| Rate limiting | IMPLEMENTED |
| Database indexes | IMPLEMENTED |
| Caching | IMPLEMENTED |
| Cache invalidation | IMPLEMENTED |
| Concurrency correctness | IMPLEMENTED |
| Graceful shutdown | IMPLEMENTED |
| Health checks | IMPLEMENTED |
| Readiness checks | IMPLEMENTED |
| Structured logging | IMPLEMENTED |
| Metrics | IMPLEMENTED |
| Dashboards | IMPLEMENTED |
| Error handling | IMPLEMENTED |
| Docker security | IMPLEMENTED |
| Secrets management | PARTIAL |
| HTTPS | PARTIAL — terminated by the platform, not by this repo |
| Horizontal scaling | IMPLEMENTED |
| Dependency failure behaviour | IMPLEMENTED |
| Alerting | PARTIAL — rules exist, no receiver |
| Frontend accessibility | PARTIAL |
| Frontend security | IMPLEMENTED |
| Backups | FUTURE |
| Disaster recovery | FUTURE |

---

## Request correctness

### API validation — IMPLEMENTED

Every request body is a Pydantic model, so malformed input is rejected before
it reaches a service. URL validation is the substantial part: scheme allow-list
(`http`/`https` only), length ceiling, and rejection of credentials in the
authority. `javascript:` and `data:` never reach the database.

SSRF is treated as a first-class concern rather than a footnote, because the
whole product accepts a URL from an anonymous caller and later makes it
clickable. Hostnames resolving to loopback, link-local, private, or reserved
ranges are refused, including the shorthand forms that string matching misses —
`http://127.1/admin` is caught, as is `http://169.254.169.254/`, the cloud
metadata endpoint that turns a URL shortener into a credential leak.

The decision to **block private destinations** is documented with its
trade-offs in [ENGINEERING_DECISIONS.md](./ENGINEERING_DECISIONS.md); an
internal deployment shortening intranet links would want the opposite default,
which is why it is a setting rather than a constant.

Verified by: `blocks SSRF to link-local`, `blocks permissive IPv4 loopback`,
`rejects javascript: scheme`, `rejects a malformed URL` in the gate, plus the
validation unit tests.

### Frontend validation — IMPLEMENTED

The form checks shape before spending a request, and surfaces the server's
field-level errors when it disagrees. The backend stays authoritative: the
client never decides whether a URL is acceptable, only whether it is worth
sending. Covered by the Vitest suite.

### Error handling — IMPLEMENTED

One error envelope for every failure, produced by exception handlers rather
than assembled at each call site, so a new endpoint cannot invent its own
shape. Distinct conditions get distinct codes — `404` for an unknown code,
`410` for one that existed and expired, `422` for a rejected URL, `429` for the
limiter, `503` for a dependency outage. `410` versus `404` is the one worth
calling out: a client can tell "never existed" from "no longer valid", which is
the difference between a typo and an expiry.

Tracebacks never reach a response. `debug=False` and custom handlers mean the
default HTML error page is unreachable.

---

## Authentication — FUTURE

There is no authentication, and this is a decision rather than an omission.

The product as specified is an anonymous public shortener, so the unit of abuse
control is the IP address, which is what the rate limiter keys on. Adding
accounts would change the data model (an owner column, a users table), the
authorization rules (who may delete which code), and the limiter key (per
principal rather than per IP) all at once.

What that means concretely today: **anyone who knows a short code can delete
it.** `DELETE /api/v1/urls/{code}` is unauthenticated. For a public demo that
is acceptable and honest; for anything real it is not.

The smallest credible step is a creation token returned at create time and
required for delete, which adds ownership without a full identity system.

---

## Rate limiting — IMPLEMENTED

A token bucket in Redis, evaluated by a Lua script so check-and-consume is
atomic under concurrency. State lives in Redis, not in a process, which is the
property that makes it correct across replicas — the gate drains the budget
through the load balancer and then confirms **both** replicas refuse, which a
per-process limiter would fail.

Responses carry `X-RateLimit-Limit`, `-Remaining`, `-Reset`, `-Policy` and, on
a rejection, `Retry-After`. The frontend reads `Retry-After` and tells the user
how long to wait instead of showing a bare failure.

Client identity is derived from `X-Forwarded-For` counted from the right by
`TRUSTED_PROXY_HOPS`, so a client cannot forge a fresh identity by sending its
own header. This is the detail that makes IP-based limiting meaningful behind a
proxy; getting it wrong makes the limiter trivially bypassable.

**Redis unavailable:** reads fail open, writes fail closed. Rationale and the
reasoning behind splitting the two are in
[ENGINEERING_DECISIONS.md](./ENGINEERING_DECISIONS.md). Verified by the gate.

**Limitation.** The bucket is per IP, so NAT'd users share a budget and a
distributed source is limited per address rather than in aggregate. Real abuse
control needs a second layer keyed on something harder to rotate.

---

## Data layer

### Indexes — IMPLEMENTED

Three indexes, each tied to a query that exists in the code, with the reasoning
and the `EXPLAIN ANALYZE` output in
[DATABASE_AND_SHARDING.md](./DATABASE_AND_SHARDING.md). The index that matters
is the unique one on `short_code`: it serves the redirect lookup and enforces
collision safety, so correctness and performance come from the same object.

Indexes that were considered and *not* created are listed with the reason,
since an unused index costs write throughput for nothing.

### Concurrency — IMPLEMENTED

Short-code collisions are resolved by the database, not by a pre-check: insert
and retry on unique violation. A read-then-write check would be a race no
matter how the read is written. Click counts are buffered in Redis and flushed
periodically, so concurrent redirects to one code do not serialise on a row
lock. `tests/integration/test_concurrency.py` exercises simultaneous creates
and concurrent redirects rather than asserting the design in prose.

### Cache invalidation — IMPLEMENTED

Deletion invalidates the entry, and the gate proves it end to end: after a
delete, the code stops redirecting on a subsequent request. Negative lookups
are cached too, with a shorter TTL, so a scanner hammering nonexistent codes
does not become database load. Cache-key format, TTL choice, serialisation, and
stampede behaviour are documented in [ARCHITECTURE.md](./ARCHITECTURE.md).

### Backups and disaster recovery — FUTURE

Not implemented, and worth being blunt: the local stack keeps PostgreSQL in a
Docker volume with no backup, no point-in-time recovery, and no restore drill.
If that volume is lost, every short link is gone.

This is the largest gap between this project and a production service, and it
is not a coding task — it is `pg_dump` on a schedule plus WAL archiving plus,
most importantly, a *tested* restore. An untested backup is a guess. The
deployment path in [README.md](../README.md) uses managed PostgreSQL
specifically so automated backups and PITR come from the platform rather than
being reinvented here.

---

## Operations

### Health and readiness — IMPLEMENTED

`/health` answers from the process alone; `/ready` checks Redis and PostgreSQL.
The split is what makes rollout behaviour correct — liveness failing means
restart me, readiness failing means stop sending traffic. Compose and the
platform health checks both use them.

The interesting case is verified explicitly: with Redis down, `/ready` stays
`200`, because reads still work and pulling every replica out of service would
convert a degraded cache into a total outage. With PostgreSQL down it returns
`503`. That asymmetry is deliberate and is asserted in the gate.

### Graceful shutdown — IMPLEMENTED

Lifespan teardown drains the click buffer, closes the connection pool, and
releases Redis. Signals reach the process directly rather than through a shell
wrapper, so `SIGTERM` is not swallowed and the container stops in its grace
period instead of being killed. Without the buffer flush, a deploy would
silently discard the most recent clicks.

### Structured logging — IMPLEMENTED

JSON lines with timestamp, level, request ID, method, templated route, status,
and duration. Request IDs come from the edge (`X-Request-ID`, generated by
Nginx when absent) and are returned to the client, so a user-reported error can
be traced to its log line.

Full URLs are deliberately **not** logged — a shortened URL can carry a session
token or a password-reset link in its query string, and logs outlive the
request. The code is logged instead, which is enough to find the row.

### Metrics — IMPLEMENTED

HTTP counters and a duration histogram; application counters for creates,
redirects, cache outcomes, and limiter decisions; dependency error counters and
latency.

Cardinality is treated as a correctness property, not a style preference. Route
labels are templates (`/{code}`, never `/aB3xY9z`) and paths that match no
route collapse to one label, so scanning traffic cannot grow the series count.
There is a test asserting no metric ever contains a short code or a URL,
because this is the failure that takes Prometheus down rather than merely
looking untidy.

Known-value series are seeded at zero at startup. A labelled counter that has
never been incremented exports nothing, which on a dashboard is
indistinguishable from a mistyped metric name — both render "No data". This
mattered in practice: the hit-ratio panel divides by `hits + misses`, so a
freshly started replica blanked the panel entirely until it happened to serve a
redirect.

### Dashboards — IMPLEMENTED

One Grafana dashboard, provisioned from disk with its datasource, so it exists
on first boot with no clicking. Panels cover throughput, latency percentiles,
error rate, 429 rate, cache hit ratio, creates, redirects, and dependency
errors. `scripts/verify_dashboard_queries.py` checks every panel query against
the live Prometheus, so a renamed metric fails a check instead of quietly
producing an empty graph months later.

### Alerting — PARTIAL

Six alert rules and six recording rules are provisioned and evaluate cleanly:
`ApiInstanceDown`, `HighServerErrorRate`, `RedisUnavailable`,
`PostgresUnavailable`, `CacheHitRatioCollapsed`, `RedirectLatencyHigh`.

They genuinely fire — during a verification run that stops PostgreSQL,
`PostgresUnavailable` transitions to `firing`, which is visible in
`http://localhost:9090/alerts`.

**What is missing is the half that matters at 3am:** there is no Alertmanager
and no receiver, so a firing alert is visible in a UI nobody is watching rather
than delivered to a person. Wiring a receiver is straightforward; the
deliberate omission is that routing, grouping, inhibition, and on-call
escalation are a real design exercise that a demo cannot honestly simulate.

The thresholds are also unvalidated. They were chosen by judgement, not derived
from observed behaviour, so in production they would need tuning against real
traffic before anyone trusted a page.

---

## Infrastructure

### Docker security — IMPLEMENTED

Multi-stage builds, non-root users, `no-new-privileges`, dropped capabilities,
pinned base images, and no build toolchain in the runtime layer. The gate
asserts the containers actually run as non-root rather than trusting the
Dockerfile — a `USER` line is easy to add and easy to negate later.

CI runs `pip-audit`, `npm audit`, and a Trivy image scan, so a vulnerable
transitive dependency surfaces on a pull request.

### Secrets management — PARTIAL

No secrets are committed. Configuration is environment-based and typed, with
`.env.example` files carrying placeholders. `.env` is git-ignored, and the
compose files reference variables rather than inlining values.

**The limitation:** in the local stack, secrets are environment variables in a
compose file, which is fine for local work and not fine for production.
Environment variables are visible to anyone who can inspect the container, they
land in process listings and crash dumps, and rotating one means a redeploy.
Production wants a secret manager with rotation and audit. The default
PostgreSQL and Grafana passwords in `.env.example` are development placeholders
and are labelled as such.

### HTTPS — PARTIAL

The local stack is HTTP on `localhost`, where TLS would add ceremony without
adding security. The application is written to sit behind a terminator: it
honours `X-Forwarded-Proto`, builds short URLs from the forwarded host, and
counts proxy hops when identifying clients. The port-preservation fix
(`$http_host` rather than `$host`) exists precisely so generated URLs stay
correct behind a proxy.

TLS is terminated by the platform in the documented deployment path rather than
by Nginx here, because certificate issuance and renewal are better handled by
infrastructure than by a container in this repo. HSTS is therefore also a
platform concern; setting it locally over HTTP would be wrong.

### Horizontal scaling — IMPLEMENTED

Two API replicas behind Nginx, stateless by construction: no session state, no
in-process cache of shared data, no local counters. Every piece of shared state
— cache, limiter buckets, click buffer — is in Redis, which is what makes a
third replica a configuration change rather than a rewrite.

Proven rather than asserted: the gate confirms traffic reaches both replicas,
that a URL created on `api-1` is served from cache by `api-2`, and that the
limiter budget is shared across both. Killing one replica leaves traffic
flowing through the survivor (10/10 requests succeeded).

**Limitation.** Nginx itself is a single instance and therefore a single point
of failure, as are the single Redis and single PostgreSQL. See
[ARCHITECTURE.md](./ARCHITECTURE.md) for the full SPOF list and what each would
take to remove.

### Dependency failure behaviour — IMPLEMENTED

Each dependency has a defined, tested degradation rather than an assumption:

| Failure | Behaviour |
| --- | --- |
| Redis down | Redirects work (fail open); creates return `503` (fail closed); `/ready` stays `200` |
| PostgreSQL down | Cached redirects work; uncached return `503`; `/ready` returns `503` |
| One API replica down | Nginx routes to the survivor; no client-visible errors |
| Frontend down | SPA route returns `502`; the entire API is unaffected |

These are the checks in section 7 of the gate. They are the part of the project
most worth running, because a documented failure mode that was never executed
is a hypothesis.

---

## Frontend

### Security — IMPLEMENTED

No `dangerouslySetInnerHTML` anywhere; user-provided URLs are rendered as text
and as `href` values that React escapes. Outbound short links carry
`rel="noopener noreferrer"` and the app sends no referrer, so visiting a
shortened link does not leak the shortener's URL. Security headers
(`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
`Permissions-Policy`) are set at the edge and asserted by the gate. No secret
is present in the bundle — the API base URL is the only build-time value, and
it defaults to same-origin so no environment ever hard-codes a production host.

Open redirect deserves a note, since a URL shortener *is* an open redirect by
definition. The mitigations are scheme restriction, SSRF filtering, and `307`
rather than a meta-refresh or JavaScript hop, so the browser's own protections
apply.

### Accessibility — PARTIAL

Labelled controls, `aria-invalid` and `aria-describedby` on invalid fields,
live regions for async results so a screen reader hears them, visible focus,
full keyboard operation, and semantic landmarks. `eslint-plugin-jsx-a11y`
runs in CI.

**What has not been done is the part that requires a human:** no screen-reader
pass with NVDA or VoiceOver, no automated axe audit, no verified contrast
ratios. The basics are right by construction; calling it accessible without a
real assistive-technology pass would be overclaiming.

---

## The five things I would do first

In order, if this were about to take real traffic:

1. **Backups with a tested restore.** The only item on this list where the
   failure is unrecoverable rather than merely painful.
2. **Ownership tokens on delete.** Closes the one genuine security hole:
   anyone with a code can currently delete it.
3. **Alertmanager with a real receiver, and thresholds tuned against observed
   traffic.** Rules that fire into an unwatched UI are not alerting.
4. **Remove the single points of failure** — Redis with replication and
   failover, PostgreSQL with a standby, more than one load balancer.
5. **A secret manager with rotation**, replacing environment-variable secrets.

Items 1 and 2 are small and high value. Items 3 through 5 are infrastructure
work whose difficulty lies in operating them, not in configuring them.
