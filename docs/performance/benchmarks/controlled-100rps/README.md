# Benchmark 20260904-041256

## Workload

- Offered load: 100 req/s for 45s per repeat, using k6 `constant-arrival-rate` so both arms receive identical load
- Repeats: 3 per arm, run alternately; reported figures are medians
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
| Baseline (no cache, no index) | 98.5 | 173.35 | 100.99 | 530.74 | 1083.96 | 0.0000 |
| Optimized (Redis + index) | 99.5 | 4.72 | 2.28 | 5.39 | 99.11 | 0.0000 |

### Change

| Metric | Baseline | Optimized | Change |
| --- | --: | --: | --: |
| Throughput (RPS) | 98.5 | 99.5 | +1.0% (better) |
| Average latency (ms) | 173.35 | 4.72 | -97.3% (better) |
| p50 latency (ms) | 100.99 | 2.28 | -97.7% (better) |
| p95 latency (ms) | 530.74 | 5.39 | -99.0% (better) |
| p99 latency (ms) | 1,083.96 | 99.11 | -90.9% (better) |
| Error rate | 0.0000 | 0.0000 | n/a |

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
| baseline | 1 | 96.9 | 160.24 | 996.77 | 1350.65 | 0.0000 | 6.39 11.07 14.61 |
| baseline | 2 | 98.5 | 100.99 | 530.74 | 1083.96 | 0.0000 | 15.49 13.51 15.16 |
| baseline | 3 | 99.0 | 48.67 | 508.68 | 796.12 | 0.0000 | 9.19 12.33 14.52 |
| optimized | 1 | 99.5 | 2.28 | 5.39 | 99.11 | 0.0000 | 20.43 13.86 15.39 |
| optimized | 2 | 99.5 | 2.05 | 12.28 | 153.83 | 0.2284 | 14.79 13.72 15.13 |
| optimized | 3 | 99.5 | 2.28 | 4.74 | 51.62 | 0.0000 | 9.99 12.01 14.25 |

Optimized throughput ranged 99.5-99.5 req/s across repeats. The load average column shows how busy the host was; this machine was shared with unrelated work, which is why the arms were interleaved and medians reported.

## Cache behaviour (optimized arm, final repeat)

- Hits: 4,501
- Misses: 0
- Hit ratio: 1.0000

## Load distribution

- Baseline (final repeat): {'api-1': 2242, 'api-2': 2242}
- Optimized (final repeat): {'api-1': 2251, 'api-2': 2250}

Raw k6 output, the recorded host conditions for each repeat, and the
`EXPLAIN (ANALYZE, BUFFERS)` plans for both configurations are
alongside this file.
