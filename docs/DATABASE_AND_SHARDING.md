# Database design and sharding strategy

**This application is not sharded.** It runs a single PostgreSQL instance. This
document explains the schema and indexes as they exist, then sets out what
sharding would look like and what would trigger it. Nothing here describes
behaviour that is already implemented unless it says so.

---

## 1. Schema

```sql
CREATE TABLE urls (
    id           BIGSERIAL    PRIMARY KEY,
    short_code   VARCHAR(16)  NOT NULL,
    original_url TEXT         NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ  NULL,
    deleted_at   TIMESTAMPTZ  NULL,
    click_count  BIGINT       NOT NULL DEFAULT 0,

    CONSTRAINT uq_urls_short_code UNIQUE (short_code),
    CONSTRAINT ck_urls_click_count_non_negative CHECK (click_count >= 0),
    CONSTRAINT ck_urls_original_url_length
        CHECK (char_length(original_url) BETWEEN 1 AND 2048),
    CONSTRAINT ck_urls_short_code_charset
        CHECK (short_code ~ '^[A-Za-z0-9_-]{4,16}$'),
    CONSTRAINT ck_urls_expires_after_created
        CHECK (expires_at IS NULL OR expires_at > created_at)
);
```

Owned by Alembic ([`backend/alembic/versions/`](../backend/alembic/versions/)).
`db/init/01-extensions.sql` only enables `pg_stat_statements`, so there is
exactly one migration path.

### Type choices

| Column | Type | Reasoning |
| --- | --- | --- |
| `id` | `BIGSERIAL` | Internal surrogate key; never exposed. `INT` would cap at 2.1 billion links, which is a plausible ceiling for a URL shortener. |
| `short_code` | `VARCHAR(16)` | The public identifier. Bounded so the index stays compact and the CHECK constraint is enforceable. |
| `original_url` | `TEXT` + CHECK | `TEXT` and `VARCHAR(n)` are stored identically in PostgreSQL, so the limit is expressed as a CHECK — the same 2048 bound the API validates, restated where it cannot be bypassed. |
| timestamps | `TIMESTAMPTZ` | Instants, not wall-clock readings. `TIMESTAMP` would silently reinterpret values by server timezone. |
| `click_count` | `BIGINT` | A single viral link can exceed 2.1 billion clicks. |
| `deleted_at` | `TIMESTAMPTZ` NULL | Soft delete — see below. |

### Why the CHECK constraints exist at all

The service layer already validates all of this. The constraints are a backstop
for the case the validation is wrong: a bug that writes a negative click count
or an `expires_at` before `created_at` should fail loudly at write time rather
than become a permanent bad row that every reader has to defend against.

`ck_urls_expires_after_created` earned its place during testing — a test tried
to simulate expiry with `UPDATE … SET expires_at = <past>` and the constraint
rejected it. The test was wrong, and the constraint caught it.

### Why soft delete

`DELETE /api/v1/urls/{code}` sets `deleted_at` instead of removing the row.

A hard delete would return the short code to the generatable keyspace. Since
codes are random, a future creation could eventually be issued the same code —
and every link previously shared, bookmarked or printed would then silently
point at somebody else's destination. That is a link-hijacking vector, and it
arrives through no fault of the person who published the original link.

The cost is unbounded growth: rows are never reclaimed. That is the same trade
every URL shortener makes by promising links keep working, and it is the right
side of the trade here. Archiving cold rows to cheaper storage while retaining
the codes is the natural future step.

Every read path therefore filters `deleted_at IS NULL`, and delete also
invalidates the cache entry so the change takes effect immediately across all
replicas.

---

## 2. Indexes

Measured on the live 250,000-row table (37 MB of table, 23 MB of indexes):

| Index | Size | Definition |
| --- | --: | --- |
| `uq_urls_short_code` | 7.0 MB | `UNIQUE (short_code)` |
| `pk_urls` | 5.6 MB | `PRIMARY KEY (id)` |
| `ix_urls_created_at_id` | 11 MB | `(created_at DESC, id DESC)` |
| `ix_urls_expires_at_active` | **8 KB** | `(expires_at) WHERE expires_at IS NOT NULL AND deleted_at IS NULL` |

### `uq_urls_short_code` — the one that matters

**Query it optimizes**

```sql
SELECT id, short_code, original_url, expires_at, created_at, click_count
FROM urls
WHERE short_code = $1 AND deleted_at IS NULL;
```

Every redirect and every metadata read. On a cache miss this is the query.

**Why it is useful.** Measured on 250,000 rows — full plans in
[`docs/performance/benchmarks/controlled-100rps/`](performance/benchmarks/controlled-100rps/):

Without it:

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

With it:

```text
Index Scan using uq_urls_short_code on urls  (cost=0.42..8.44 rows=1 width=112)
                                             (actual time=1.129..1.130 rows=1 loops=1)
  Index Cond: ((short_code)::text = '298Np3p'::text)
  Filter: (deleted_at IS NULL)
  Buffers: shared hit=1 read=3
Execution Time: 1.194 ms
```

| | Scan | Index | Ratio |
| --- | --: | --: | --: |
| Execution time | 14.888 ms | 1.194 ms | 12.5× |
| Buffers | 4731 | 4 | 1183× |
| Rows examined | 250,000 | 1 | — |

**Why it beats a table scan.** The buffer count is the real story. The scan
reads ~37 MB of pages *per lookup* and recruits two parallel workers, so one
redirect briefly occupies three cores. Under concurrency those requests contend
for CPU and buffer pool, which is why baseline latency degrades to seconds
rather than staying near 15 ms. The index probe touches four pages and is
effectively constant-time at this scale — B-tree depth grows logarithmically,
so ten times the rows adds roughly one page read.

**Trade-offs.** ~7 MB at 250k rows (about 19% of table size); an extra B-tree
insert per write, and page splits under load. Both are accepted without
hesitation, because this index is **not optional**: it also enforces the
uniqueness that makes concurrent code generation safe. Even if it were never
read, it would have to exist.

### `ix_urls_created_at_id` — listing

**Query it optimizes**

```sql
SELECT … FROM urls
WHERE deleted_at IS NULL AND (created_at, id) < ($1, $2)
ORDER BY created_at DESC, id DESC
LIMIT $3;
```

**Why it is useful.** The column order and direction match the `ORDER BY`
exactly, so PostgreSQL walks the index and stops at `LIMIT` — no sort node, and
cost independent of table size. `id` is the tiebreaker: `created_at` alone is
not unique, and without a deterministic tiebreaker keyset pagination can skip
or repeat rows when timestamps collide.

An integration test asserts the plan contains no `Sort Key`, so a regression
here fails the build rather than quietly becoming slow.

**Trade-offs.** The largest index at 11 MB — larger than the table's primary
key — for an endpoint far less hot than redirects. `pg_stat_user_indexes`
currently reports **0 scans** against it, versus 538 for `uq_urls_short_code`.

That is worth stating plainly: on current usage this index does not pay for
itself. It is retained because the alternative is a full scan plus a sort of
every row on a listing endpoint, which degrades without bound as the table
grows — a cliff rather than a slope. If listing were dropped from the API, this
index should be dropped with it.

### `ix_urls_expires_at_active` — the expiry sweeper, and why partial

**Query it optimizes**

```sql
SELECT id, short_code FROM urls
WHERE expires_at IS NOT NULL AND expires_at < now() AND deleted_at IS NULL
LIMIT $1;
```

**Why it is useful, and why partial.** The `WHERE` clause on the index means
only rows that can *ever* match are stored. The measurement makes the case
better than any argument:

| Index | Size at 250k rows |
| --- | --: |
| `ix_urls_created_at_id` (full) | 11 MB |
| `ix_urls_expires_at_active` (partial) | **8 KB** |

Three orders of magnitude smaller, because almost every seeded row has
`expires_at IS NULL`. A full index on `expires_at` would store a quarter of a
million NULL entries that the sweeper can never match. The partial index stays
resident in memory, and — the part that is easy to miss — writes only pay for
it when the row actually qualifies, so creating a permanent link incurs no
index maintenance here at all.

**Trade-offs.** The planner can only use it when the query's predicate implies
the index's, so the sweeper query must keep the `expires_at IS NOT NULL AND
deleted_at IS NULL` clauses even though they look redundant next to
`expires_at < now()`. That is a real maintenance hazard — a future refactor
that "simplifies" the query would silently lose the index. Hence the comment on
the index definition in the model.

### Indexes deliberately not created

| Candidate | Why not |
| --- | --- |
| `original_url` | Nothing queries by destination. It would cost write throughput and space for no reader. A dedup feature would change this. |
| `click_count` | No "most popular links" endpoint. It would also be the worst possible index to maintain, since the column changes on every redirect. |
| `(short_code, original_url)` covering | The lookup already reads one heap page after the index probe; a covering index would enlarge the hottest index to save that one page. Measured first, then rejected. |
| `deleted_at` | Low cardinality (two meaningful states) and almost always used together with another predicate, where it is better as a partial-index condition. |

---

## 3. Short-code generation and collisions

The alphabet is base62 (`A-Za-z0-9`); the default length is 7, giving
62⁷ ≈ **3.52 × 10¹²** codes. Custom aliases additionally permit `-` and `_`.

Codes come from `secrets.choice`, not `random`. A predictable generator would
let an attacker enumerate codes that are about to be issued, or guess
unlisted ones — the codes are the only access control on a link.

### Collision handling

Collisions are treated as certain to happen eventually, not as improbable
enough to ignore:

```text
for attempt in 1..SHORT_CODE_MAX_ATTEMPTS (5):
    code = generate(length)
    try:  INSERT …            # UNIQUE(short_code) is the arbiter
    except IntegrityError:
        record a collision metric
        widen the length on later attempts
        retry
else: fail loudly
```

Two points of substance:

1. **The database decides, not the application.** There is no
   `SELECT … WHERE short_code = ?` before inserting. That check would be a
   TOCTOU race: two workers could both observe a code as free and both attempt
   it. Catching `IntegrityError` is correct at any concurrency, and an
   integration test drives concurrent creations to confirm it.
2. **Retries widen the code.** Repeated collisions are evidence the keyspace at
   the current length is crowded, so later attempts lengthen rather than
   re-rolling the same odds.

### Expected characteristics

The per-insert collision probability is occupancy, `n/N`:

| Stored links | Occupancy | Probability one insert collides |
| --: | --: | --: |
| 1 million | 2.8 × 10⁻⁷ | 1 in 3.5 million |
| 100 million | 2.8 × 10⁻⁵ | 1 in 35,000 |
| 1 billion | 2.8 × 10⁻⁴ | 1 in 3,500 |

At 100 million links, five consecutive collisions has probability
(2.8 × 10⁻⁵)⁵ ≈ **1.7 × 10⁻²³** — so exhausting the retry budget is not a
realistic failure mode, and the code raising there is a genuine "something is
badly wrong" signal.

Note this is deliberately *not* the birthday bound. The birthday problem
answers "will any two of n codes collide", which is the wrong question when
each insert is independently checked by a UNIQUE index; the relevant quantity
is the chance that *this* insert hits an occupied slot. A unit test pins the
table above so the documentation cannot drift from the implementation.

---

## 4. Sharding

### What would trigger it

Sharding would be a response to a measured limit, not a milestone. In
likelihood order:

1. **Write throughput on a single primary.** The nearest real trigger. Creation
   is a synchronous `INSERT` plus index maintenance, and read replicas do not
   help writes at all.
2. **Working set outstripping RAM.** While the hot index fits in memory, reads
   are page-cache hits. Once it does not, every cache miss becomes disk I/O and
   p99 degrades sharply. At ~7 MB per 250k rows, the `short_code` index reaches
   ~28 GB around 1 billion links — beyond comfortable single-instance memory.
3. **Vacuum and maintenance windows.** Index maintenance on a multi-terabyte
   table starts to constrain deploys and schema changes.
4. **Blast radius.** One primary means one failure takes everything. Shards
   reduce a total outage to a partial one.

Cheaper measures come first, and in this order: raise the cache hit ratio, add
read replicas, partition by time within one instance
(`PARTITION BY RANGE (created_at)`). Native partitioning gets much of the
maintenance benefit with none of the cross-shard complexity, and is the step
this schema is closest to.

### Candidate shard keys

#### `short_code` hash — **chosen**

```text
shard = hash(short_code) mod SHARD_COUNT
```

**Why.** The workload decides this almost entirely. The dominant query, by
orders of magnitude, is:

```sql
WHERE short_code = $1
```

An exact-match predicate on the shard key means the router knows the target
shard before issuing anything, so a redirect touches exactly one shard. That is
the property that makes sharding worth doing:

| Operation | Frequency | Shards touched |
| --- | --- | --: |
| Redirect | dominant | 1 |
| Metadata read | common | 1 |
| Create | moderate | 1 |
| Delete | rare | 1 |
| Newest-first listing | rare | **all** |

**Distribution.** Codes come from a CSPRNG, so hashing them is uniform by
construction — no skew correction needed, and no possibility of a "celebrity"
key, because each key addresses exactly one row.

**Hot partitions.** Per-key hotspots are impossible: a shard cannot be hot
because of one popular row, only because it holds more traffic in aggregate,
which uniform hashing already prevents. A viral link concentrates load on one
*cache* key, which Redis absorbs — and that is a cache concern, not a shard
concern.

#### `user_id` — **rejected**

Rejected for two independent reasons, either sufficient:

1. **There are no users.** The service has no authentication, so the column
   does not exist. Sharding on a field the schema lacks is not an option.
2. **Even with users, it would be wrong here.** The hot path resolves a
   `short_code` and has no user context — the whole point of a short link is
   that anyone can follow it without identifying themselves. Every redirect
   would need a `short_code → user_id` lookup first, which is either a global
   index (the bottleneck reintroduced) or a scatter-gather across shards. It
   would also invite skew: one enterprise customer creating 40% of links would
   place 40% of the data on one shard.

`user_id` is the right key for systems whose queries are naturally
user-scoped — a mailbox, a feed, an order history. This system's queries are
link-scoped, and the shard key should follow the access pattern.

#### `created_at` range — **rejected as the shard key**

Every new link lands on the newest shard, making it a write hotspot while older
shards go idle. Redirect traffic also skews recent, so the newest shard takes
disproportionate reads too. It is a reasonable *partitioning* key inside one
instance (archival, cheap bulk drops of expired data) but a poor sharding key.

#### `id` range — **rejected**

Same monotonic hotspot as `created_at`, and worse: the hot path does not know
`id`. Resolving `short_code → id` before routing puts a global lookup in front
of every redirect, which defeats the exercise.

### How traffic would be distributed

With 4 shards and uniform hashing, each shard holds ~25% of links and serves
~25% of redirects. Cache misses distribute the same way, since Redis Cluster
would hash the same `short_code`, so one code maps to one cache slot and one
database shard.

Creation distributes uniformly too, because the code is generated *before*
routing — so shard selection inherits the CSPRNG's uniformity. This is a
concrete advantage over auto-increment identifiers, where the newest shard
always absorbs the writes.

### Rebalancing

Naive `hash mod N` is the trap: changing `N` remaps almost every key. Growing
from 4 shards to 5 would relocate ~80% of rows, and — far worse for this
system — every existing short link would resolve to the wrong shard during the
migration.

Two workable approaches:

1. **Consistent hashing with virtual nodes.** Map codes onto a ring of, say,
   1024 virtual nodes and assign those to physical shards. Adding a shard moves
   only `1/(N+1)` of the virtual nodes. Rebalancing becomes reassigning vnodes
   and copying their rows.
2. **Pre-split into many logical shards.** Start with 1024 logical shards
   mapped onto 4 physical instances. Growth reassigns logical shards without
   ever rehashing a key. Simpler to reason about, and the mapping is small
   enough to keep in a config service. **This is what I would choose** — the
   operational simplicity of never rehashing outweighs the ring's elegance.

Either way the migration is: dual-write, backfill, verify, cut reads over,
stop dual-writing. The soft-delete design helps here, since rows are immutable
after creation apart from `click_count` and `deleted_at` — a backfill does not
have to chase a moving target for the fields that matter.

### Cross-shard implications

What breaks, and what to do about it:

| Capability | Impact | Mitigation |
| --- | --- | --- |
| `short_code` uniqueness | A UNIQUE index is per-shard, so it no longer guarantees global uniqueness | Uniqueness is only needed *within* the shard a code routes to: `hash(code)` sends a given code to exactly one shard, and its local UNIQUE index is authoritative for it. **Global uniqueness follows from the routing function.** This is the single most elegant consequence of choosing `short_code`. |
| Newest-first listing | Becomes scatter-gather across all shards, then a merge | Accept it (rare, admin-facing), or maintain the listing in a separate store fed by CDC. Not worth sharding-aware pagination. |
| `COUNT(*)`, aggregate click totals | Fan-out and sum | Precompute into a metrics store rather than querying shards. |
| Transactions across links | No cross-shard transactions without 2PC | Never needed: each operation touches one link, hence one shard. Another consequence of the key choice. |
| Expiry sweeper | Must run per shard | Fine, and better — it parallelises naturally. |
| Migrations | Must be applied to every shard | Alembic against each shard in turn; the naming convention on the metadata already keeps them identical. |

The recurring theme is that most of the usual sharding pain does not arise
here, because the shard key is also the natural identifier for every hot
operation. The pain is concentrated in exactly one place — listing — which is
the rarest operation in the system.

### Evolution path

```text
Today          1 PostgreSQL, 1 Redis, 2 stateless API replicas
               ↓ trigger: read latency, cache misses growing
Step 1         + read replicas; misses and metadata reads go to replicas
               ↓ trigger: maintenance windows, table size
Step 2         + native range partitioning on created_at within one instance
               ↓ trigger: single-primary write throughput
Step 3         + logical shards (1024) on hash(short_code), 4 physical instances
               + Redis Cluster on the same key
               + a routing layer in the repository (the only code that changes)
               ↓ trigger: sustained write growth
Step 4         rebalance by reassigning logical shards; never rehash a key
```

The repository layer is where routing would live, which is why SQL is confined
to `repositories/` today. Services call
`repository.get_by_short_code(code)`; that method choosing a connection from a
shard map instead of a single engine is a change in one file, and no service,
route or schema has to know sharding exists. That boundary is the practical
preparation for sharding, and it is real today even though the sharding is not.
