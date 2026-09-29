# Performance

Every number in this document was measured on the stack in this repository. Raw
k6 output, the recorded host conditions for each repeat, and the query plans are
committed under [`docs/performance/`](performance/) so the figures can be
checked rather than taken on trust.

Reproduce with:

```bash
./scripts/benchmark.sh          # baseline vs optimized
./scripts/load-test.sh all      # the four k6 scenarios
```

---

## 1. Test environment

Measurements were taken on a developer workstation, not on dedicated hardware.
This matters, and section 6 quantifies how much.

| | |
| --- | --- |
| Host | Apple Silicon, macOS (Darwin 25.6.0, arm64), 8 cores |
| Container runtime | Docker Desktop, Linux VM with 8 vCPU and 3.83 GiB RAM |
| Load generator | k6 v2.2.0, in a container on the same Docker network |
| API | 2 containers, **1 uvicorn worker each**, Python 3.12 |
| PostgreSQL | 17-alpine, `max_connections=100`, `shared_buffers` default |
| Redis | 7-alpine, `maxmemory=256mb`, `allkeys-lru`, persistence off |
| Load balancer | Nginx 1.27-alpine, round-robin over the two API containers |
| Connection pool | `pool_size=10`, `max_overflow=10` per API container |
| Frontend | Not in the request path for these tests; served separately by Nginx |

The load generator shares the Docker VM with the system under test. That is a
real limitation of single-machine benchmarking and is accounted for in the
methodology rather than ignored.

### Why only two CPUs ever serve requests

Each API container runs a single uvicorn worker, so exactly two Python
processes handle all traffic regardless of the eight cores available. This is
deliberate:

- `prometheus_client` in its default mode keeps counters in process memory. Two
  workers per container would mean two disagreeing registries behind one
  `/metrics` endpoint, requiring the multiprocess collector and a shared
  scratch directory — meaningful complexity for a service designed to scale by
  adding replicas.
- One worker per container makes a container and a unit of capacity the same
  thing, which is what keeps `X-Served-By` and per-replica metrics honest.

The consequence is that vertical headroom is unused by design; capacity is
added by running more replicas. Section 5 covers what that implies.

---

## 2. Workload

Both benchmark arms use the same k6 script
([`load-tests/redirects.js`](../load-tests/redirects.js)) against the same
corpus, through Nginx.

| | |
| --- | --- |
| Path under test | `GET /{code}` — the redirect hot path |
| Executor | `constant-arrival-rate`, so both arms receive **identical offered load** |
| Corpus | 50 short codes, chosen uniformly at random per request |
| Table size | **250,000 rows** (~44 MB with indexes) |
| Repeats | 3 per arm at 100 req/s; 2 per arm at 800 req/s |
| Duration | 45 s per repeat |

The table size is the single most important choice here. On a few hundred rows
PostgreSQL sequential-scans even when an index exists, because the entire table
is a couple of pages — an "index versus no index" comparison at that size
measures nothing and would wrongly suggest the index is pointless. At 250,000
rows the difference between an index probe and a scan is real.

### What "baseline" and "optimized" mean

| | Baseline | Optimized |
| --- | --- | --- |
| Redis cache | disabled (`CACHE_ENABLED=false`) | cache-aside, jittered TTL |
| `short_code` index | UNIQUE constraint dropped | UNIQUE btree |
| Click counting | synchronous `UPDATE` per redirect | buffered in Redis, flushed in batches |
| Rate limiter | **enabled**, budget raised | **enabled**, budget raised |

The rate limiter stays on in both arms so its Redis round-trip sits inside the
measurement either way; only its budget is raised, so it never becomes the
bottleneck under test. The index is dropped and restored in SQL around the
baseline arm rather than being left out of the migration, so the schema a
reviewer inspects afterwards is the real one.

---

## 3. Baseline vs optimized

### 3a. Controlled load — 100 req/s

Offered load sits inside both arms' capacity, so this isolates the **cost of a
single request**. Median of 3 interleaved repeats.

| Version | RPS | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Error rate |
| --- | --: | --: | --: | --: | --: | --: |
| Baseline (no cache, no index) | 98.5 | 173.35 | 100.99 | 530.74 | 1083.96 | 0.0000 |
| Optimized (Redis + index) | 99.5 | 4.72 | 2.28 | 5.39 | 99.11 | 0.0000 |

| Metric | Baseline | Optimized | Change |
| --- | --: | --: | --- |
| Throughput (RPS) | 98.5 | 99.5 | +1.0% |
| Average latency | 173.35 ms | 4.72 ms | **−97.3%** |
| p50 latency | 100.99 ms | 2.28 ms | **−97.7%** (44× faster) |
| p95 latency | 530.74 ms | 5.39 ms | **−99.0%** |
| p99 latency | 1083.96 ms | 99.11 ms | **−90.9%** |
| Error rate | 0.0000 | 0.0000 | — |

**Throughput is near-identical here, and that is expected.** k6 offers a fixed
arrival rate; while both arms can absorb it, they must both report roughly the
offered rate, and the difference can only appear in latency. Reporting "+1.0%
throughput" as an achievement would be misleading. To separate throughput, the
offered load has to exceed what the baseline can serve — which is what the next
section does.

### 3b. Saturation — 800 req/s

Median of 2 interleaved repeats.

| Version | RPS | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Error rate |
| --- | --: | --: | --: | --: | --: | --: |
| Baseline (no cache, no index) | 89.6 | 1662.43 | 1563.58 | 3499.72 | 5256.91 | 0.0004 |
| Optimized (Redis + index) | 777.3 | 11.86 | 1.09 | 49.38 | 264.60 | 0.0000 |

| Metric | Baseline | Optimized | Change |
| --- | --: | --: | --- |
| Throughput (RPS) | 89.6 | 777.3 | **+767.8%** (8.7×) |
| Average latency | 1662.43 ms | 11.86 ms | **−99.3%** |
| p50 latency | 1563.58 ms | 1.09 ms | **−99.9%** |
| p95 latency | 3499.72 ms | 49.38 ms | **−98.6%** |
| p99 latency | 5256.91 ms | 264.60 ms | **−95.0%** |
| Error rate | 0.0004 | 0.0000 | −100% |

At 800 req/s offered, the baseline serves 89.6 — it is saturated at roughly an
eighth of the load and its latency has collapsed into multiple seconds. The
optimized configuration absorbs 777.3 of the 800 offered while holding a p50 of
1.09 ms.

Both arms were served evenly by the two API containers throughout
(2251/2250 in the controlled run), so neither result is an artefact of traffic
landing on one replica.

---

## 4. Where the time goes

### The query plan

Captured on the same 250,000-row table, for the exact lookup the redirect path
performs. Full output in
[`docs/performance/benchmarks/controlled-100rps/`](performance/benchmarks/controlled-100rps/).

Baseline — the UNIQUE index dropped:

```text
Gather  (cost=1000.00..7033.18 rows=1 width=112) (actual time=12.850..14.761 rows=1 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=4731
  ->  Parallel Seq Scan on urls  (actual time=10.067..10.068 rows=0 loops=3)
        Filter: ((deleted_at IS NULL) AND ((short_code)::text = 'af7d1130fd45'::text))
        Rows Removed by Filter: 83333
Execution Time: 14.888 ms
```

Optimized — the UNIQUE index present:

```text
Index Scan using uq_urls_short_code on urls  (cost=0.42..8.44 rows=1 width=112)
                                             (actual time=1.129..1.130 rows=1 loops=1)
  Index Cond: ((short_code)::text = '298Np3p'::text)
  Filter: (deleted_at IS NULL)
  Buffers: shared hit=1 read=3
Execution Time: 1.194 ms
```

| | Baseline | Optimized | Ratio |
| --- | --: | --: | --: |
| Execution time | 14.888 ms | 1.194 ms | **12.5× faster** |
| Buffers touched | 4731 | 4 | **1183× fewer** |
| Rows examined | 250,000 | 1 | — |
| Parallel workers | 2 | 0 | — |

The buffer count is the more telling figure. The baseline reads roughly 37 MB
of pages **per lookup**, and PostgreSQL recruits two parallel workers to do it,
so a single redirect consumes three cores briefly. Concurrent requests then
contend for CPU and buffer pool, which is why baseline latency degrades so
sharply under load rather than staying flat at ~15 ms.

### The three optimizations, separated

The measured gain comes from three changes made together. Their contributions
differ in kind:

1. **The UNIQUE index** removes the 250,000-row scan. This is what turns a
   ~15 ms query into a ~1 ms one, and it matters on *every* cache miss. Without
   it, the cache is only papering over a query that stays expensive.
2. **The Redis cache** removes the database from the hot path entirely. With a
   1.0 hit ratio the redirect never reaches PostgreSQL, which is why the
   optimized p50 (1.09–2.28 ms) is below even the indexed query time.
3. **Buffered click counting** removes a synchronous `UPDATE` from every
   redirect. In the baseline, each redirect is a read *and* a write, so it also
   takes a row lock and generates WAL — this is a large part of why the
   baseline collapses under concurrency rather than merely being slow.

Because the cache absorbs nearly all reads, the index looks redundant at a 1.0
hit ratio — but it is what bounds the cost of a cold cache, an evicted key, or
a Redis outage. Keeping both is defence in depth, not redundancy.

### Cache hit ratio

The optimized arm recorded a **1.0000** hit ratio (4501 hits, 0 misses in the
final repeat). That is genuine but flattered by the workload: 50 codes read
repeatedly for 45 seconds, and `POST /api/v1/urls` primes the cache on
creation, so even a code's first read is a hit. Production traffic against a
long tail of rarely-used links would see a lower ratio, and the indexed query
in section 4 is what sets the floor when that happens.

---

## 5. Capacity and bottlenecks

### Measured ceilings

| Path | Achieved | Conditions |
| --- | --: | --- |
| Redirects (`GET /{code}`) | **~1326 req/s** | 1500 req/s offered, cache hits, p50 8.6 ms, 0 errors |
| Redirect (sustainable) | **~780 req/s** | p50 1.09 ms, p95 49 ms |
| URL creation (`POST`) | **~779 req/s** | 30 VUs, p50 12.89 ms, p95 104.82 ms, 0 errors |
| Mixed traffic (Scenario D) | **~1509 req/s** | 40 VUs, 85% reads, p50 20.49 ms, 0 errors |

At the ~1326 req/s ceiling, p95 rose to 774 ms and p99 to 1704 ms while the
error rate stayed at 0 — the system queues rather than failing, which is the
preferable failure mode, but it is past the point of a usable SLO.

### Current bottleneck: Python CPU

At saturation the two uvicorn workers are the constraint, not PostgreSQL or
Redis. During the saturation run PostgreSQL sat near-idle and Redis used
1.91 MB with no evictions, while the API containers were the busiest processes.

Each cached redirect still costs two Redis round-trips (one rate-limiter Lua
call, one cache read) plus a click-buffer write, all inside a single-threaded
event loop. At ~660 req/s per container that is roughly 1.5 ms of Python per
request, which is consistent with the measured p50.

The implication is that this system scales **horizontally, cheaply** — the
constrained resource is the one that replicates trivially, because the API
holds no state. Scenario D confirms the mechanism works: a code created on one
container resolved on the other 4569 times in a 45-second run.

### Bottlenecks in the order they would appear

1. **API CPU** — now. Fix: more replicas.
2. **PostgreSQL writes** — creation is a synchronous `INSERT`; a single primary
   is the write ceiling. Fix: read replicas first, then the sharding plan in
   [DATABASE_AND_SHARDING.md](DATABASE_AND_SHARDING.md).
3. **Redis as a single node** — every request touches it twice, so it is both
   the hottest dependency and a single point of failure. Fix: Redis Cluster or
   a replicated managed instance.
4. **Nginx** — one container, far from saturated at these rates, but a single
   point of failure. Fix: multiple instances behind DNS or a cloud LB.

---

## 6. Measurement honesty

### The host was busy, and it shows

The workstation was running unrelated work throughout (a browser, an editor, a
Rust compile, a macOS wallpaper extension). Host load average ranged from 6.4
to 20.4 during the runs — on 8 cores, up to 2.5× oversubscribed.

Two identical back-to-back runs of the same configuration were observed to
differ by **3–7×** purely from this contention. Concretely, two runs at the
same 400 req/s offered rate produced p50 values of 17.37 ms and 4.75 ms.

Reporting a single baseline run against a single optimized run would therefore
have compared two levels of background load as much as two configurations. Two
countermeasures were applied:

1. **Interleaving.** The arms alternate (baseline, optimized, baseline, …) so
   contention drift affects both roughly equally, rather than landing entirely
   on whichever arm ran during a busy stretch.
2. **Medians over repeats.** The reported figure is the median, which discards
   a single bad excursion.

Every repeat is recorded individually with the host load average at the time,
in each run's `README.md` and `conditions.txt`, so the spread is visible instead
of hidden behind an average.

### One outlier, kept on the record

In the controlled run, optimized repeat 2 recorded a **0.2284 error rate** while
repeats 1 and 3 recorded zero:

| Arm | Repeat | RPS | p50 (ms) | p95 (ms) | Errors | Load average |
| --- | --: | --: | --: | --: | --: | --- |
| optimized | 1 | 99.5 | 2.28 | 5.39 | 0.0000 | 20.43 |
| optimized | 2 | 99.5 | 2.05 | 12.28 | **0.2284** | 14.79 |
| optimized | 3 | 99.5 | 2.28 | 4.74 | 0.0000 | 9.99 |

The median discards it, which is the point of taking one, but it should not go
unmentioned. Latency in that repeat was normal (p50 2.05 ms), so this was not
gradual overload; it looks like a burst of failures during container churn on a
contended host rather than a load-dependent property of the application. It is
not fully explained, and on dedicated hardware it would be worth chasing before
publishing an SLO.

### What these numbers are not

- **Not a production capacity estimate.** Everything shares one laptop: load
  generator, database, cache, both API replicas, Prometheus and Grafana.
- **Not tuned.** Default `shared_buffers`, no `pgbouncer`, no HTTP keep-alive
  tuning, no uvloop/httptools benchmarking. There is headroom left deliberately
  — the goal was to demonstrate the effect of the architectural choices, not to
  reach a maximum number.
- **Not a claim about the 1.0 cache hit ratio generalising.** See section 4.

---

## 7. Conclusions

1. **The optimizations work, and the effect is large.** 44× lower p50 latency
   under controlled load; 8.7× higher throughput at saturation. Both measured,
   both reproducible via `./scripts/benchmark.sh`.
2. **The index matters more than it appears.** At a 1.0 hit ratio the cache
   hides it, but it is what bounds the cost of a cold cache or a Redis outage:
   4 buffers instead of 4731.
3. **The write on the read path was as costly as the missing index.** Removing
   the synchronous per-redirect `UPDATE` is a large part of why the baseline
   collapses under concurrency instead of degrading gently.
4. **The bottleneck is the resource that scales most easily.** Python CPU, on
   stateless replicas, behind a load balancer, with all shared state in Redis
   and PostgreSQL.
5. **Single-machine benchmarking needed defending.** Interleaved arms, repeats
   and medians were not ceremony here; without them the run-to-run noise
   exceeded the effect being measured on at least one occasion.
