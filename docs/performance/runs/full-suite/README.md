# Load-test run 20260904-030550

- Target: `http://nginx:80` (Nginx, 2 API instances)
- Host: Darwin 25.6.0 arm64
- k6: k6 v2.2.0 (commit/00a9a1b7f5, go1.26.5, linux/arm64)

| Scenario | Requests | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
| --- | --: | --: | --: | --: | --: | --: |
| A -- URL creation | 54,522 | 778.9 | 12.89 | 104.82 | 225.18 | 0.0000 |
| B -- redirects | 11,714 | 194.4 | 1.65 | 9.78 | 199.00 | 0.0000 |
| C -- distributed rate limiter | n/a | n/a | n/a | n/a | n/a | n/a |
| D -- concurrent distributed traffic | 68,183 | 1,509.2 | 20.49 | 74.09 | 130.32 | 0.0000 |

## Scenario notes

### B -- cache

- Hits: 11,712  Misses: 0
- Hit ratio: 1.0000
- Served by: {'api-1': 6832, 'api-2': 4882}

### C -- rate limiter

- Sent: 60  Allowed: 20  Throttled: 40
- Configured limit (from `X-RateLimit-Limit`): 20
- Allowed within a single shared budget: True
- Instances that served traffic: ['api-1', 'api-2']
- Distinct rate-limit buckets: None

### D -- distributed traffic

- Share of traffic: {'api-1': 0.5, 'api-2': 0.5}
- Load balanced: True
- Cross-instance resolves: 4,569

