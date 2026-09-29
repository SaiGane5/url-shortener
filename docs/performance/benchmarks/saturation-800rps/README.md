# Benchmark 20260904-041914

## Workload

- Offered load: 800 req/s for 45s per repeat, using k6 `constant-arrival-rate` so both arms receive identical load
- Repeats: 2 per arm, run alternately; reported figures are medians
- Corpus: 50 short codes, selected uniformly at random
- Table size: ~250,000 rows, so an index probe and a sequential scan are meaningfully different
- Path under test: `GET /{code}` through Nginx across 2 API instances

## Configurations

| | Baseline | Optimized |
| --- | --- | --- |
| Redis cache | disabled | cache-aside, jittered TTL |
| `short_code` index | dropped | UNIQUE btree |
| Click counting | synchronous `UPDATE` per redirect | buffered in Redis, flushed in batches |
| Rate limiter | enabled (budget raised) | enabled (budget raised) |

## Results (median of repeats)

| Version | RPS | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Error rate |
| --- | --: | --: | --: | --: | --: | --: |
| Baseline (no cache, no index) | 89.6 | 1662.43 | 1563.58 | 3499.72 | 5256.91 | 0.0004 |
| Optimized (Redis + index) | 777.3 | 11.86 | 1.09 | 49.38 | 264.60 | 0.0000 |

### Change

| Metric | Baseline | Optimized | Change |
| --- | --: | --: | --: |
| Throughput (RPS) | 89.6 | 777.3 | +767.8% (better) |
| Average latency (ms) | 1,662.43 | 11.86 | -99.3% (better) |
| p50 latency (ms) | 1,563.58 | 1.09 | -99.9% (better) |
| p95 latency (ms) | 3,499.72 | 49.38 | -98.6% (better) |
| p99 latency (ms) | 5,256.91 | 264.60 | -95.0% (better) |
| Error rate | 0.0004 | 0.0000 | -100.0% (better) |

> **Reading the throughput row.** k6 offers a fixed arrival rate, so
> whenever both arms can absorb the offered load their throughput is
> necessarily near-identical and the difference shows up in latency
> instead. Throughput only separates the two once the offered rate
> exceeds what the baseline can serve. To see that, re-run with a
> higher rate (`RPS=800 ./scripts/benchmark.sh`) and compare the
> achieved RPS against the offered rate for each arm.

## Per-repeat detail

| Arm | Repeat | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Errors | Load average |
| --- | --: | --: | --: | --: | --: | --: | --- |
| baseline | 1 | 110.1 | 1240.41 | 2525.32 | 3186.55 | 0.0000 | 8.76 10.94 13.60 |
| baseline | 2 | 69.1 | 1886.76 | 4474.12 | 7327.26 | 0.0009 | 11.58 12.18 13.81 |
| optimized | 1 | 763.9 | 1.20 | 84.52 | 453.88 | 0.0000 | 14.78 12.52 14.03 |
| optimized | 2 | 790.7 | 0.99 | 14.24 | 75.32 | 0.0000 | 15.86 13.26 14.07 |

Optimized throughput ranged 763.9-790.7 req/s across repeats. The load average column shows how busy the host was; this machine was shared with unrelated work, which is why the arms were interleaved and medians reported.

## Cache behaviour (optimized arm, final repeat)

- Hits: 35,785
- Misses: 0
- Hit ratio: 1.0000

## Load distribution

- Baseline (final repeat): {'api-1': 1698, 'api-2': 1696}
- Optimized (final repeat): {'api-1': 17892, 'api-2': 17893}

Raw k6 output, the recorded host conditions for each repeat, and the
`EXPLAIN (ANALYZE, BUFFERS)` plans for both configurations are
alongside this file.
