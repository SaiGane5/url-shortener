#!/usr/bin/env bash
#
# Runs the k6 load-test suite against the local Docker stack and saves the
# results under docs/performance/.
#
#   ./scripts/load-test.sh              # all four scenarios
#   ./scripts/load-test.sh redirects    # one scenario
#   ./scripts/load-test.sh --list       # what is available
#
# k6 runs in a container joined to the compose network and targets Nginx
# directly at http://nginx:80. That keeps the host's published-port hop out of
# the measurement and means no k6 installation is required on the host.
#
# Scenarios A, B and D run with the load-test profile (raised rate limits, so
# the limiter does not shed the traffic under test). Scenario C runs with the
# production profile, because throttling is the behaviour it verifies. The
# script switches profiles automatically.

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

readonly COMPOSE_BASE="docker-compose.yml"
readonly COMPOSE_LOADTEST="docker-compose.loadtest.yml"
readonly NETWORK="url-shortener_backplane"
readonly TARGET="http://nginx:80"
readonly K6_IMAGE="grafana/k6:latest"

RUN_ID="$(date +%Y%m%d-%H%M%S)"
RESULTS_DIR="docs/performance/runs/$RUN_ID"
RESTORE_PROFILE=false

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: ./scripts/load-test.sh [scenario ...]

Scenarios:
  create        A -- URL creation throughput (write path)
  redirects     B -- redirect throughput and cache hit ratio (hot path)
  rate-limit    C -- distributed rate limiter (production limits)
  distributed   D -- mixed traffic across both API instances
  all           every scenario, in order (default)

Options:
  --restore     afterwards, restore the production rate-limit policy. This
                recreates the API containers and so resets their in-process
                Prometheus metrics; omit it if you want to inspect this run's
                metrics in Grafana first.
  --list        print the scenarios and exit
  --help        this message

Environment overrides (forwarded to k6):
  DURATION, VUS, RPS, CORPUS_SIZE, REQUESTS
USAGE
}

# --- preflight ---------------------------------------------------------------

check_dependencies() {
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running"
  docker compose version >/dev/null 2>&1 || die "docker compose v2 is required"
  [[ -f .env ]] || die ".env is missing -- copy .env.example to .env first"
}

# Brings the stack up under the requested profile and waits for readiness.
# Idempotent: re-running with the same profile is a no-op beyond the health wait.
ensure_stack() {
  local profile="$1"   # "production" or "loadtest"
  local -a compose_args=(-f "$COMPOSE_BASE")

  if [[ "$profile" == "loadtest" ]]; then
    compose_args+=(-f "$COMPOSE_LOADTEST")
  fi

  log "starting the stack (profile: $profile)"
  docker compose "${compose_args[@]}" up -d --wait --wait-timeout 180 \
    postgres redis api-1 api-2 nginx prometheus grafana \
    || die "the stack did not become healthy -- try: docker compose logs"

  wait_for_ready
}

wait_for_ready() {
  local attempt
  for attempt in $(seq 1 30); do
    if curl -fsS --max-time 2 http://localhost:8080/ready >/dev/null 2>&1; then
      log "stack is ready"
      return 0
    fi
    sleep 1
  done
  die "/ready did not respond after 30s"
}

# The redirect scenarios read from a corpus they create in setup(), so no
# separate seeding step is needed. This just proves the write path works before
# committing to a multi-minute run.
verify_write_path() {
  local response
  response="$(curl -fsS --max-time 5 -X POST http://localhost:8080/api/v1/urls \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com/load-test-preflight"}' 2>/dev/null)" \
    || die "could not create a URL -- the write path is broken or rate limited"

  local code
  code="$(printf '%s' "$response" | sed -n 's/.*"short_code":"\([^"]*\)".*/\1/p')"
  [[ -n "$code" ]] || die "URL creation returned no short_code: $response"
  log "write path verified (created /$code)"
}

# --- k6 ----------------------------------------------------------------------

run_k6() {
  local script="$1" outdir="$2"
  shift 2

  mkdir -p "$outdir"

  local -a env_args=(-e "BASE_URL=$TARGET")
  local name
  for name in DURATION VUS MAX_VUS RPS CORPUS_SIZE REQUESTS POLICY EXPECT_CACHE; do
    if [[ -n "${!name:-}" ]]; then
      env_args+=(-e "$name=${!name}")
    fi
  done
  # Per-scenario overrides passed by the caller win over the environment.
  while (($#)); do
    env_args+=(-e "$1")
    shift
  done

  # k6 writes summary.json into its working directory, which is the mounted
  # results dir, so the summary lands on the host.
  #
  # `|| status=$?` is required, not defensive: k6 exits non-zero when a
  # threshold is crossed, and under `set -e` with `pipefail` that would abort
  # the whole suite before the result could be recorded. A crossed threshold is
  # a finding to report, not a reason to throw the run away.
  local status=0
  docker run --rm \
    --network "$NETWORK" \
    -v "$REPO_ROOT/load-tests:/scripts:ro" \
    -v "$REPO_ROOT/$outdir:/results" \
    -w /results \
    "$K6_IMAGE" run "${env_args[@]}" \
    --summary-export=/results/k6-metrics.json \
    "/scripts/$script" \
    2>&1 | tee "$outdir/k6-output.txt" || status=$?

  if ((status != 0)); then
    # A non-zero exit means a threshold was breached. That is a real result, not
    # a broken run, so record it and keep going rather than aborting the suite.
    warn "k6 exited $status for $script (a threshold was not met) -- results kept"
  fi
  echo "$status" > "$outdir/k6-exit-code.txt"
  return 0
}

scenario_create() {
  log "Scenario A -- URL creation"
  ensure_stack loadtest
  verify_write_path
  run_k6 create-urls.js "$RESULTS_DIR/a-create-urls"
}

scenario_redirects() {
  log "Scenario B -- redirects and cache hit ratio"
  ensure_stack loadtest
  verify_write_path
  run_k6 redirects.js "$RESULTS_DIR/b-redirects"
}

scenario_rate_limit() {
  log "Scenario C -- distributed rate limiter (production limits)"
  # Deliberately the production profile: throttling is the behaviour under test.
  ensure_stack production
  # The bucket must start full, otherwise the allowed count reflects whatever
  # earlier scenarios consumed rather than the configured burst.
  log "clearing rate-limit buckets so the burst starts from a full budget"
  docker compose exec -T redis redis-cli --scan --pattern 'rl:*' \
    | xargs -r -n 100 docker compose exec -T redis redis-cli DEL >/dev/null 2>&1 || true
  run_k6 rate-limit.js "$RESULTS_DIR/c-rate-limit"
}

scenario_distributed() {
  log "Scenario D -- concurrent distributed traffic"
  ensure_stack loadtest
  verify_write_path
  run_k6 distributed.js "$RESULTS_DIR/d-distributed"
}

# --- reporting ---------------------------------------------------------------

print_summary() {
  local index="$RESULTS_DIR/README.md"

  {
    echo "# Load-test run $RUN_ID"
    echo
    echo "- Target: \`$TARGET\` (Nginx, 2 API instances)"
    echo "- Host: $(uname -srm)"
    echo "- k6: $(docker run --rm "$K6_IMAGE" version 2>/dev/null | head -1)"
    echo
    echo "| Scenario | Requests | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Errors |"
    echo "| --- | --: | --: | --: | --: | --: | --: |"
  } > "$index"

  local dir
  for dir in "$RESULTS_DIR"/*/; do
    [[ -f "$dir/summary.json" ]] || continue
    python3 - "$dir/summary.json" >> "$index" <<'PY'
import json, sys

with open(sys.argv[1]) as handle:
    data = json.load(handle)


def fmt(value, digits=1):
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}"


latency = data.get("latency_ms") or {}
print(
    "| {name} | {reqs} | {rps} | {p50} | {p95} | {p99} | {errors} |".format(
        name=data.get("scenario", "?"),
        reqs=fmt(data.get("requests"), 0),
        rps=fmt(data.get("rps")),
        p50=fmt(latency.get("p50"), 2),
        p95=fmt(latency.get("p95"), 2),
        p99=fmt(latency.get("p99"), 2),
        errors=fmt(data.get("error_rate"), 4) if data.get("error_rate") is not None else "n/a",
    )
)
PY
  done

  # Scenario-specific findings that a latency table cannot express.
  {
    echo
    echo "## Scenario notes"
    echo
  } >> "$index"

  if [[ -f "$RESULTS_DIR/b-redirects/summary.json" ]]; then
    python3 - "$RESULTS_DIR/b-redirects/summary.json" >> "$index" <<'PY'
import json, sys
with open(sys.argv[1]) as handle:
    data = json.load(handle)
cache = data.get("cache", {})
ratio = cache.get("hit_ratio")
print("### B -- cache")
print()
print(f"- Hits: {cache.get('hits', 0):,}  Misses: {cache.get('misses', 0):,}")
if ratio is not None:
    print(f"- Hit ratio: {ratio:.4f}")
print(f"- Served by: {data.get('served_by', {})}")
print()
PY
  fi

  if [[ -f "$RESULTS_DIR/c-rate-limit/summary.json" ]]; then
    python3 - "$RESULTS_DIR/c-rate-limit/summary.json" >> "$index" <<'PY'
import json, sys
with open(sys.argv[1]) as handle:
    data = json.load(handle)
print("### C -- rate limiter")
print()
print(f"- Sent: {data.get('requests_sent')}  Allowed: {data.get('allowed')}  "
      f"Throttled: {data.get('throttled')}")
print(f"- Configured limit (from `X-RateLimit-Limit`): {data.get('configured_limit')}")
print(f"- Allowed within a single shared budget: "
      f"{data.get('allowed_within_one_shared_budget')}")
print(f"- Instances that served traffic: {data.get('instances_that_served')}")
print(f"- Bucket identity stable across instances: "
      f"{data.get('bucket_identity_stable')}")
print(f"- First 429 at request index: {data.get('first_throttled_at')}")
print(f"- Header checks passed/failed: {data.get('checks_passed')}/"
      f"{data.get('checks_failed')}")
print()
PY
  fi

  if [[ -f "$RESULTS_DIR/d-distributed/summary.json" ]]; then
    python3 - "$RESULTS_DIR/d-distributed/summary.json" >> "$index" <<'PY'
import json, sys
with open(sys.argv[1]) as handle:
    data = json.load(handle)
print("### D -- distributed traffic")
print()
print(f"- Share of traffic: {data.get('share_of_traffic')}")
print(f"- Load balanced: {data.get('load_balanced')}")
print(f"- Cross-instance resolves: {data.get('cross_instance_resolves'):,}")
print()
PY
  fi

  echo
  log "results saved to $RESULTS_DIR"
  cat "$index"
}

# --- main --------------------------------------------------------------------

main() {
  local -a requested=()

  while (($#)); do
    case "$1" in
      --help|-h) usage; exit 0 ;;
      --list) usage; exit 0 ;;
      --restore) RESTORE_PROFILE=true ;;
      create|redirects|rate-limit|distributed|all) requested+=("$1") ;;
      *) die "unknown scenario '$1' (try --list)" ;;
    esac
    shift
  done

  ((${#requested[@]})) || requested=(all)
  if [[ " ${requested[*]} " == *" all "* ]]; then
    requested=(create redirects rate-limit distributed)
  fi

  check_dependencies
  mkdir -p "$RESULTS_DIR"

  local scenario
  for scenario in "${requested[@]}"; do
    case "$scenario" in
      create)      scenario_create ;;
      redirects)   scenario_redirects ;;
      rate-limit)  scenario_rate_limit ;;
      distributed) scenario_distributed ;;
    esac
  done

  print_summary

  # Restoring the production profile means recreating the API containers, which
  # resets their in-process Prometheus registries and erases the very metrics
  # the run just produced. Since the usual next step is to open Grafana and look
  # at them, that is left to the operator rather than done automatically.
  if [[ "$RESTORE_PROFILE" == "true" ]]; then
    log "restoring the production rate-limit profile (this resets API metrics)"
    docker compose -f "$COMPOSE_BASE" up -d --wait --wait-timeout 120 api-1 api-2 >/dev/null
  else
    cat <<'NOTE'

Note: the API containers are still running with raised rate limits, and their
metrics from this run are intact -- open Grafana at http://localhost:3000 to
inspect them.

To restore the production rate-limit policy afterwards (this resets the
in-process metrics):

    docker compose up -d --force-recreate api-1 api-2

NOTE
  fi
  log "done"
}

main "$@"
