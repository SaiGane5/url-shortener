#!/usr/bin/env python3
"""Print server-side latency percentiles for a route from Prometheus.

Used to separate application latency from everything the client also measures
(Nginx, the Docker network, k6's own scheduling). When k6 reports a long tail
but this reports a short one, the time is being spent outside the application
and no amount of application tuning will move it.

    ./scripts/query_latency.py --route '/{code}' --window 10m
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request

QUANTILES = (0.50, 0.95, 0.99)


def query(prometheus: str, expression: str) -> float | None:
    """Run an instant query and return the single scalar it produced."""
    url = f"{prometheus}/api/v1/query?" + urllib.parse.urlencode({"query": expression})
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - fixed scheme
        payload = json.load(response)

    if payload.get("status") != "success":
        raise RuntimeError(f"query failed: {payload}")

    result = payload["data"]["result"]
    if not result:
        return None

    value = float(result[0]["value"][1])
    # A histogram_quantile over a bucket set with no recent observations yields
    # NaN rather than an empty result; treat that as "no data" too.
    return None if value != value else value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", default="http://localhost:9090")
    parser.add_argument("--route", default="/{code}", help="route label to filter on")
    parser.add_argument("--window", default="10m", help="range for the rate() window")
    parser.add_argument(
        "--by-instance",
        action="store_true",
        help="break the percentiles down per API replica",
    )
    args = parser.parse_args()

    selector = f'http_request_duration_seconds_bucket{{route="{args.route}"}}'
    grouping = "le, replica" if args.by_instance else "le"

    print(f"route={args.route} window={args.window}")

    for quantile in QUANTILES:
        expression = (
            f"histogram_quantile({quantile}, "
            f"sum(rate({selector}[{args.window}])) by ({grouping}))"
        )
        try:
            value = query(args.prometheus, expression)
        except (OSError, RuntimeError) as error:
            print(f"  p{int(quantile * 100)}: error: {error}")
            continue

        label = f"p{int(quantile * 100)}"
        print(f"  {label}: {value * 1000:.2f} ms" if value is not None else f"  {label}: no data")

    throughput = query(
        args.prometheus,
        f'sum(rate(http_requests_total{{route="{args.route}"}}[{args.window}]))',
    )
    if throughput is not None:
        print(f"  throughput: {throughput:.1f} req/s (averaged over {args.window})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
