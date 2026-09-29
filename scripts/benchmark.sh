#!/usr/bin/env bash
#
# Baseline vs optimized benchmark for the redirect hot path.
#
#   ./scripts/benchmark.sh
#
# Both arms serve identical offered load (a fixed k6 arrival rate) against an
# identical corpus, so the only difference is the configuration under test:
#
#   baseline    CACHE_ENABLED=false, the short_code index dropped, and click
#               counts written straight to PostgreSQL on every redirect.
#               Every request becomes a sequential scan plus a write.
#
#   optimized   Redis cache-aside, the UNIQUE index on short_code present, and
#               click counts buffered in Redis and flushed in the background.
#
# The index is dropped and recreated around the baseline arm rather than being
# left out of the migration, so the schema a reviewer inspects afterwards is the
# real one. The teardown runs from a trap, so an interrupted run still restores
# it.
#
# Results, including the raw k6 output for both arms, land in
# docs/performance/benchmarks/<timestamp>/.

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

readonly NETWORK="url-shortener_backplane"
readonly TARGET="http://nginx:80"
readonly K6_IMAGE="grafana/k6:latest"

# Identical workload for both arms. Modest enough that the optimized arm stays
# comfortably inside its capacity, so the comparison reflects the cost of a
# request rather than how each configuration degrades once queueing dominates.
RPS="${RPS:-100}"
DURATION="${DURATION:-45s}"
CORPUS_SIZE="${CORPUS_SIZE:-50}"
MAX_VUS="${MAX_VUS:-100}"

# The arms are measured alternately, several times each, and the reported figure
# is the median across repeats.
#
# This matters more than it might appear. The load generator shares one machine
# with the stack it measures, and on a developer workstation that machine also
# has a browser, an editor and whatever else running. Back-to-back repeats of an
# identical configuration were observed to differ by 3-7x purely from host
# contention. Measuring baseline once and optimized once would therefore compare
# two different levels of background load as much as two configurations.
# Alternating the arms spreads any contention drift across both, and the median
# discards the single worst excursion.
REPEATS="${REPEATS:-3}"

# Table volume matters more than any other knob here. On a few hundred rows
# PostgreSQL sequential-scans even when an index exists, because the whole table
# is one or two pages -- so an "index vs no index" comparison on a small table
# measures nothing and would invite the conclusion that the index is worthless.
# At this size the table is tens of megabytes and the difference between an
# index probe and a scan is real.
SEED_ROWS="${SEED_ROWS:-250000}"

RUN_ID="$(date +%Y%m%d-%H%M%S)"
RESULTS_DIR="docs/performance/benchmarks/$RUN_ID"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

psql_run() {
  docker compose exec -T postgres psql -U urlshort -d urlshort -v ON_ERROR_STOP=1 "$@"
}

# Compose with both files, so the benchmark overrides always apply.
bench_compose() {
  docker compose -f docker-compose.yml -f docker-compose.benchmark.yml "$@"
}

# --- preflight ---------------------------------------------------------------

check_dependencies() {
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running"
  [[ -f .env ]] || die ".env is missing -- copy .env.example to .env first"
}

wait_for_ready() {
  local attempt
  for attempt in $(seq 1 45); do
    if curl -fsS --max-time 2 http://localhost:8080/ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  die "/ready did not respond in time"
}

# Brings up both API instances with the given cache/click configuration. Rate
# limits are raised in both arms so the limiter never becomes the bottleneck,
# and it stays enabled in both so its Redis round-trip is inside the measurement
# either way.
start_api() {
  local cache_enabled="$1" click_flush="$2"

  log "starting API with CACHE_ENABLED=$cache_enabled CLICK_FLUSH_ENABLED=$click_flush"

  # Exported for the remainder of this arm so that *every* Compose invocation
  # resolves the same values. Setting them as a one-shot prefix on a single
  # command is not enough: a later `up nginx` would pull in its `depends_on`
  # API containers, resolve `${CACHE_ENABLED:-true}` from .env instead, and
  # silently recreate them with the wrong configuration -- turning the whole
  # benchmark into a comparison of two identical arms.
  export CACHE_ENABLED="$cache_enabled"
  export CLICK_FLUSH_ENABLED="$click_flush"
  export RATE_LIMIT_REQUESTS=2000000
  export RATE_LIMIT_WRITE_REQUESTS=2000000
  export RATE_LIMIT_REDIRECT_REQUESTS=2000000

  # Dependencies first, without forcing recreation. Only the API containers'
  # configuration changes between arms, and restarting PostgreSQL each time
  # would add a minute per arm and discard its buffer cache -- making the two
  # arms differ by cache warmth as well as by the setting under test.
  bench_compose up -d --wait --wait-timeout 180 postgres redis \
    || die "PostgreSQL and Redis did not become healthy"

  # `--force-recreate` on the API containers: the environment change must
  # actually take effect, and Compose will not always recreate for an env-only
  # difference.
  bench_compose up -d --wait --wait-timeout 180 --force-recreate --no-deps api-1 api-2 \
    || die "the API containers did not become healthy"

  bench_compose up -d --wait --wait-timeout 120 --no-deps nginx \
    || die "Nginx did not become healthy"

  wait_for_ready

  # Confirm the setting actually reached the process rather than assuming the
  # recreation worked -- a silently ignored override would quietly turn this
  # into a comparison of two identical configurations.
  local reported
  reported="$(docker compose exec -T api-1 printenv CACHE_ENABLED 2>/dev/null | tr -d '[:space:]')"
  if [[ "$reported" != "$cache_enabled" ]]; then
    die "api-1 reports CACHE_ENABLED=$reported, expected $cache_enabled"
  fi
}

index_present() {
  psql_run -tAc \
    "SELECT count(*) FROM pg_indexes WHERE tablename='urls' AND indexname='uq_urls_short_code';" \
    2>/dev/null | tr -d '[:space:]'
}

drop_short_code_index() {
  # The UNIQUE constraint owns the index, so the constraint has to go with it.
  # Restored by restore_short_code_index(), which the trap also calls.
  log "dropping the UNIQUE index on short_code (baseline)"
  psql_run -c "ALTER TABLE urls DROP CONSTRAINT IF EXISTS uq_urls_short_code;" >/dev/null
  [[ "$(index_present)" == "0" ]] || die "the index was not dropped"
}

restore_short_code_index() {
  if [[ "$(index_present)" == "1" ]]; then
    return 0
  fi
  log "restoring the UNIQUE index on short_code"
  psql_run -c "ALTER TABLE urls ADD CONSTRAINT uq_urls_short_code UNIQUE (short_code);" \
    >/dev/null || warn "could not restore the index -- run migrations to repair"
}

# Bulk-loads filler rows straight through SQL rather than the API, so seeding
# takes seconds instead of hours and does not itself depend on the code path
# under test. Idempotent: tops the table up to SEED_ROWS and does nothing if it
# is already there, so repeat runs are cheap and comparable.
seed_rows() {
  local existing
  existing="$(psql_run -tAc 'SELECT count(*) FROM urls;' | tr -d '[:space:]')"

  if ((existing >= SEED_ROWS)); then
    log "table already holds $existing rows (target $SEED_ROWS) -- skipping seed"
    return 0
  fi

  local needed=$((SEED_ROWS - existing))
  log "seeding $needed filler rows (have $existing, want $SEED_ROWS)"

  # 12 hex characters satisfies the short_code CHECK constraint and gives ample
  # headroom against collisions at this row count; ON CONFLICT covers the rest.
  psql_run -c "
    INSERT INTO urls (short_code, original_url, created_at, updated_at, click_count)
    SELECT
      substr(md5(random()::text || n::text), 1, 12),
      'https://example.com/seed/' || n || '?utm_source=benchmark',
      now() - (random() * interval '90 days'),
      now(),
      floor(random() * 100)::bigint
    FROM generate_series(1, $needed) AS n
    ON CONFLICT (short_code) DO NOTHING;
  " >/dev/null || die "seeding failed"

  local total
  total="$(psql_run -tAc 'SELECT count(*) FROM urls;' | tr -d '[:space:]')"
  log "table now holds $total rows"
  psql_run -c "VACUUM ANALYZE urls;" >/dev/null
}

cleanup() {
  local status=$?
  # Always leave the schema and configuration in their documented state, even
  # if the run was interrupted part way through the baseline arm.
  restore_short_code_index >/dev/null 2>&1 || true
  if ((status != 0)); then
    warn "benchmark exited with status $status; schema restored"
  fi
}
trap cleanup EXIT INT TERM

# --- measurement -------------------------------------------------------------

# Records what else the machine was doing, so a reader can judge how much to
# trust a given repeat rather than having to take the numbers on faith.
record_conditions() {
  local outdir="$1"
  {
    echo "timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "host: $(uname -srm)"
    echo "load_average: $(uptime | sed 's/.*load averages*: //')"
    echo "docker_cpus: $(docker info --format '{{.NCPU}}' 2>/dev/null)"
    echo "docker_memory_bytes: $(docker info --format '{{.MemTotal}}' 2>/dev/null)"
  } > "$outdir/conditions.txt"
}

run_arm() {
  local arm="$1" expect_cache="$2"
  local outdir="$RESULTS_DIR/$arm"
  mkdir -p "$outdir"
  record_conditions "$outdir"

  log "measuring the $arm arm: ${RPS} req/s for $DURATION"

  docker run --rm \
    --network "$NETWORK" \
    -v "$REPO_ROOT/load-tests:/scripts:ro" \
    -v "$REPO_ROOT/$outdir:/results" \
    -w /results \
    "$K6_IMAGE" run \
    -e "BASE_URL=$TARGET" \
    -e "RPS=$RPS" \
    -e "DURATION=$DURATION" \
    -e "CORPUS_SIZE=$CORPUS_SIZE" \
    -e "MAX_VUS=$MAX_VUS" \
    -e "EXPECT_CACHE=$expect_cache" \
    /scripts/redirects.js \
    2>&1 | tee "$outdir/k6-output.txt" || true

  [[ -f "$outdir/summary.json" ]] || die "the $arm arm produced no summary.json"
}

capture_query_plan() {
  # Two statements, not one: within a single `local`, the names are declared
  # before any initialiser runs, so referring to `$label` on the same line reads
  # the still-unset local and trips `set -u`.
  local label="$1"
  local outfile="$RESULTS_DIR/explain-$label.txt"
  local code
  code="$(psql_run -tAc \
    "SELECT short_code FROM urls WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 1;" \
    2>/dev/null | tr -d '[:space:]')"

  if [[ -z "$code" ]]; then
    warn "no rows to explain for $label"
    return 0
  fi

  {
    echo "-- $label: the exact lookup the redirect path performs"
    echo "-- rows in table: $(psql_run -tAc 'SELECT count(*) FROM urls;' | tr -d '[:space:]')"
    echo
    psql_run -c "EXPLAIN (ANALYZE, BUFFERS) SELECT id, short_code, original_url, expires_at, created_at, click_count FROM urls WHERE short_code = '$code' AND deleted_at IS NULL;"
  } > "$outfile" 2>&1

  log "saved the $label query plan to $outfile"
}

# --- report ------------------------------------------------------------------

write_report() {
  python3 - "$RESULTS_DIR" "$RPS" "$DURATION" "$CORPUS_SIZE" "$REPEATS" "$SEED_ROWS" <<'PY'
import json
import pathlib
import statistics
import sys

results = pathlib.Path(sys.argv[1])
offered_rps, duration, corpus, repeats, seed_rows = sys.argv[2:7]


def load_arm(arm):
    """Every repeat of one arm, in order."""
    runs = []
    for path in sorted(results.glob(f"{arm}-*/summary.json")):
        runs.append(json.loads(path.read_text()))
    return runs


baseline_runs, optimized_runs = load_arm("baseline"), load_arm("optimized")
if not baseline_runs or not optimized_runs:
    sys.exit("both arms must have produced at least one summary")


def metric(run, name):
    if name == "rps":
        return run.get("rps") or 0.0
    if name == "errors":
        return run.get("error_rate") or 0.0
    return (run.get("latency_ms") or {}).get(name) or 0.0


NAMES = ("rps", "avg", "p50", "p95", "p99", "errors")


def median(runs, name):
    return statistics.median(metric(run, name) for run in runs)


def spread(runs, name):
    """Min-max range, so the reader can see how noisy the measurement was."""
    values = [metric(run, name) for run in runs]
    return min(values), max(values)


def change(old, new, *, lower_is_better):
    if not old:
        return "n/a"
    delta = (new - old) / old * 100
    arrow = ""
    if lower_is_better and delta < 0:
        arrow = " (better)"
    elif lower_is_better and delta > 0:
        arrow = " (worse)"
    elif not lower_is_better and delta > 0:
        arrow = " (better)"
    elif not lower_is_better and delta < 0:
        arrow = " (worse)"
    return f"{delta:+.1f}%{arrow}"


headline = [
    "| Version | RPS | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Error rate |",
    "| --- | --: | --: | --: | --: | --: | --: |",
]
for label, runs in (
    ("Baseline (no cache, no index)", baseline_runs),
    ("Optimized (Redis + index)", optimized_runs),
):
    headline.append(
        f"| {label} "
        f"| {median(runs, 'rps'):,.1f} "
        f"| {median(runs, 'avg'):.2f} "
        f"| {median(runs, 'p50'):.2f} "
        f"| {median(runs, 'p95'):.2f} "
        f"| {median(runs, 'p99'):.2f} "
        f"| {median(runs, 'errors'):.4f} |"
    )

comparison = [
    "| Metric | Baseline | Optimized | Change |",
    "| --- | --: | --: | --: |",
]
for name, label, lower_better in (
    ("rps", "Throughput (RPS)", False),
    ("avg", "Average latency (ms)", True),
    ("p50", "p50 latency (ms)", True),
    ("p95", "p95 latency (ms)", True),
    ("p99", "p99 latency (ms)", True),
    ("errors", "Error rate", True),
):
    old, new = median(baseline_runs, name), median(optimized_runs, name)
    digits = 1 if name == "rps" else (4 if name == "errors" else 2)
    comparison.append(
        f"| {label} | {old:,.{digits}f} | {new:,.{digits}f} "
        f"| {change(old, new, lower_is_better=lower_better)} |"
    )

# Per-repeat detail, so the medians above can be checked rather than trusted.
detail = [
    "| Arm | Repeat | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Errors | Load average |",
    "| --- | --: | --: | --: | --: | --: | --: | --- |",
]
for arm, runs in (("baseline", baseline_runs), ("optimized", optimized_runs)):
    for index, run in enumerate(runs, start=1):
        conditions = results / f"{arm}-{index}" / "conditions.txt"
        load_avg = "n/a"
        if conditions.exists():
            for line in conditions.read_text().splitlines():
                if line.startswith("load_average:"):
                    load_avg = line.split(":", 1)[1].strip()
        detail.append(
            f"| {arm} | {index} | {metric(run, 'rps'):,.1f} "
            f"| {metric(run, 'p50'):.2f} | {metric(run, 'p95'):.2f} "
            f"| {metric(run, 'p99'):.2f} | {metric(run, 'errors'):.4f} "
            f"| {load_avg} |"
        )

rps_low, rps_high = spread(optimized_runs, "rps")
cache = optimized_runs[-1].get("cache", {})
hit_ratio = cache.get("hit_ratio")

report = results / "README.md"
report.write_text(
    "\n".join(
        [
            f"# Benchmark {results.name}",
            "",
            "## Workload",
            "",
            f"- Offered load: {offered_rps} req/s for {duration} per repeat, "
            "using k6 `constant-arrival-rate` so both arms receive identical load",
            f"- Repeats: {repeats} per arm, run alternately; reported figures are medians",
            f"- Corpus: {corpus} short codes, selected uniformly at random",
            f"- Table size: ~{int(seed_rows):,} rows, so an index probe and a "
            "sequential scan are meaningfully different",
            "- Path under test: `GET /{code}` through Nginx across 2 API instances",
            "",
            "## Configurations",
            "",
            "| | Baseline | Optimized |",
            "| --- | --- | --- |",
            "| Redis cache | disabled | cache-aside, jittered TTL |",
            "| `short_code` index | dropped | UNIQUE btree |",
            "| Click counting | synchronous `UPDATE` per redirect | buffered in Redis, flushed in batches |",
            "| Rate limiter | enabled (budget raised) | enabled (budget raised) |",
            "",
            "## Results (median of repeats)",
            "",
            *headline,
            "",
            "### Change",
            "",
            *comparison,
            "",
            "> **Reading the throughput row.** k6 offers a fixed arrival rate, so",
            "> whenever both arms can absorb the offered load their throughput is",
            "> necessarily near-identical and the difference shows up in latency",
            "> instead. Throughput only separates the two once the offered rate",
            "> exceeds what the baseline can serve. To see that, re-run with a",
            "> higher rate (`RPS=800 ./scripts/benchmark.sh`) and compare the",
            "> achieved RPS against the offered rate for each arm.",
            "",
            "## Per-repeat detail",
            "",
            *detail,
            "",
            f"Optimized throughput ranged {rps_low:,.1f}-{rps_high:,.1f} req/s across "
            "repeats. The load average column shows how busy the host was; this "
            "machine was shared with unrelated work, which is why the arms were "
            "interleaved and medians reported.",
            "",
            "## Cache behaviour (optimized arm, final repeat)",
            "",
            f"- Hits: {cache.get('hits', 0):,}",
            f"- Misses: {cache.get('misses', 0):,}",
            f"- Hit ratio: {hit_ratio:.4f}" if hit_ratio is not None else "- Hit ratio: n/a",
            "",
            "## Load distribution",
            "",
            f"- Baseline (final repeat): {baseline_runs[-1].get('served_by', {})}",
            f"- Optimized (final repeat): {optimized_runs[-1].get('served_by', {})}",
            "",
            "Raw k6 output, the recorded host conditions for each repeat, and the",
            "`EXPLAIN (ANALYZE, BUFFERS)` plans for both configurations are",
            "alongside this file.",
            "",
        ]
    )
)

print("\n".join(headline))
print()
print("\n".join(comparison))
print(f"\nreport: {report}")
PY
}

# --- main --------------------------------------------------------------------

main() {
  check_dependencies
  mkdir -p "$RESULTS_DIR"

  log "benchmark $RUN_ID -- ${RPS} req/s for $DURATION, corpus $CORPUS_SIZE, ${REPEATS} repeats per arm"

  # Bring the stack up in its normal configuration first: seeding uses
  # ON CONFLICT, which needs the UNIQUE index still in place.
  start_api true true
  seed_rows

  local repeat
  for repeat in $(seq 1 "$REPEATS"); do
    log "--- repeat $repeat of $REPEATS ---"

    # --- baseline: no cache, no index, synchronous click writes ---
    start_api false false
    drop_short_code_index
    # Re-analyze after the index is gone so the planner is not working from
    # stale statistics that still reference it.
    psql_run -c "ANALYZE urls;" >/dev/null
    [[ "$repeat" == "1" ]] && capture_query_plan baseline
    # The corpus is created by the k6 setup() inside this arm, so it is created
    # under the degraded configuration too. That is intentional: the baseline
    # represents how the system would actually behave without these
    # optimizations, not a hybrid of the two.
    run_arm "baseline-$repeat" false

    # --- optimized: cache on, index present, buffered clicks ---
    restore_short_code_index
    psql_run -c "ANALYZE urls;" >/dev/null
    start_api true true
    [[ "$repeat" == "1" ]] && capture_query_plan optimized
    run_arm "optimized-$repeat" true
  done

  echo
  log "comparison"
  write_report
}

main "$@"
