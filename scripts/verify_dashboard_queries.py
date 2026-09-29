#!/usr/bin/env python3
"""Validate every Grafana panel query against a live Prometheus.

A dashboard that provisions successfully can still be useless: Grafana does not
check that a panel's PromQL is valid or that the metric it names actually
exists. This walks the committed dashboard JSON, executes each panel
expression against Prometheus, and reports three distinct outcomes:

  ERROR  - Prometheus rejected the query (bad PromQL). Always a real bug.
  EMPTY  - Valid query, no series. Either a metric name typo or simply an
           event that has not happened yet (e.g. no Redis errors). Needs a
           human to decide, so it is reported but does not fail the run
           unless --strict is passed.
  OK     - Valid query returning data.

Usage:
    python3 scripts/verify_dashboard_queries.py
    python3 scripts/verify_dashboard_queries.py --strict
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DASHBOARD = REPO_ROOT / "grafana" / "dashboards" / "url-shortener.json"
DEFAULT_PROMETHEUS = "http://localhost:9090"

# Grafana expands template variables client-side; Prometheus would treat them
# as literals and silently match nothing. Substitute values that keep each
# query semantically identical for validation purposes.
VARIABLE_SUBSTITUTIONS = {
    "$__rate_interval": "5m",
    "$__interval": "5m",
    "${replica:regex}": ".*",
    "$replica": ".*",
    "${route:regex}": ".*",
    "$route": ".*",
}


def iter_panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten panels, descending into rows which nest their children."""
    flattened: list[dict[str, Any]] = []
    queue = list(dashboard.get("panels", []))
    while queue:
        panel = queue.pop(0)
        nested = panel.get("panels")
        if nested:
            queue.extend(nested)
        flattened.append(panel)
    return flattened


def resolve_variables(expr: str) -> str:
    for placeholder, value in VARIABLE_SUBSTITUTIONS.items():
        expr = expr.replace(placeholder, value)
    return expr


def query_prometheus(base_url: str, expr: str) -> tuple[bool, str, int]:
    """Return (accepted, message, series_count) for a single instant query."""
    url = f"{base_url}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    try:
        with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        # Prometheus returns 400 with a JSON body explaining bad PromQL.
        try:
            detail = json.load(exc).get("error", str(exc))
        except (json.JSONDecodeError, ValueError):
            detail = str(exc)
        return False, detail, 0
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"cannot reach Prometheus at {base_url}: {exc}", 0

    if payload.get("status") != "success":
        return False, payload.get("error", "unknown error"), 0
    return True, "", len(payload.get("data", {}).get("result", []))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD)
    parser.add_argument("--prometheus", default=DEFAULT_PROMETHEUS)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail when a valid query returns no series.",
    )
    args = parser.parse_args()

    dashboard = json.loads(args.dashboard.read_text())
    panels = iter_panels(dashboard)

    errors: list[str] = []
    empties: list[str] = []
    checked = 0

    for panel in panels:
        title = panel.get("title") or f"<untitled {panel.get('type')}>"
        for target in panel.get("targets", []):
            raw_expr = target.get("expr")
            if not raw_expr:
                continue
            checked += 1
            expr = resolve_variables(raw_expr)
            if "$" in expr:
                # An unknown template variable would be sent to Prometheus as a
                # literal and match nothing, which would masquerade as EMPTY.
                # Fail loudly instead so the substitution table stays in sync
                # with the dashboard.
                errors.append(
                    f"{title}: unresolved template variable; "
                    f"add it to VARIABLE_SUBSTITUTIONS\n    query: {expr}"
                )
                print(f"ERROR  {title}\n       unresolved template variable in: {expr}")
                continue
            accepted, message, series = query_prometheus(args.prometheus, expr)
            if not accepted:
                errors.append(f"{title}: {message}\n    query: {expr}")
                print(f"ERROR  {title}\n       {message}")
            elif series == 0:
                empties.append(f"{title}\n    query: {expr}")
                print(f"EMPTY  {title}")
            else:
                print(f"OK     {title}  ({series} series)")

    print(f"\n{checked} queries checked across {len(panels)} panels")
    print(f"  ok:     {checked - len(errors) - len(empties)}")
    print(f"  empty:  {len(empties)}")
    print(f"  errors: {len(errors)}")

    if empties:
        print("\nQueries returning no series:")
        for item in empties:
            print(f"  - {item}")

    if errors:
        print("\nInvalid queries (must fix):")
        for item in errors:
            print(f"  - {item}")
        return 1
    if args.strict and empties:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
