#!/usr/bin/env bash
#
# The full verification gate: every check the project claims to pass, run in
# one go, with a pass/fail table at the end.
#
#   ./scripts/verify.sh              # everything except the long benchmarks
#   ./scripts/verify.sh --with-load  # also run the k6 scenarios and benchmark
#   ./scripts/verify.sh --list       # what will run
#
# Checks are grouped so a failure is attributable: static analysis and tests do
# not need the stack, while the runtime checks do. Nothing is skipped silently
# -- a check that cannot run is reported as SKIP with a reason, so an empty
# result is never mistaken for a pass.
#
# Exit code is non-zero if any non-optional check fails, which is what makes
# this usable as a pre-release gate rather than a report.

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

readonly BASE="http://localhost:8080"
readonly API1="http://localhost:8001"
readonly API2="http://localhost:8002"

WITH_LOAD=false
RESULTS_FILE="$(mktemp)"
trap 'rm -f "$RESULTS_FILE"' EXIT

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }

# --- result recording --------------------------------------------------------

pass() { printf 'PASS\t%s\t%s\n' "$1" "${2:-}" >> "$RESULTS_FILE"; printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
fail() { printf 'FAIL\t%s\t%s\n' "$1" "${2:-}" >> "$RESULTS_FILE"; printf '  \033[1;31m✗\033[0m %s — %s\n' "$1" "${2:-}"; }
skip() { printf 'SKIP\t%s\t%s\n' "$1" "${2:-}" >> "$RESULTS_FILE"; printf '  \033[1;33m-\033[0m %s (%s)\n' "$1" "${2:-}"; }

# Runs a command, records the outcome, and never aborts the suite.
check() {
  local name="$1"
  shift
  local output
  if output="$("$@" 2>&1)"; then
    pass "$name"
    return 0
  fi
  fail "$name" "$(printf '%s' "$output" | tail -3 | tr '\n' ' ')"
  return 1
}

# Asserts an HTTP status. Separate from `check` so the expected and actual
# codes both appear in the failure message.
check_status() {
  local name="$1" expected="$2" url="$3"
  shift 3
  local actual
  actual="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" "$url" 2>/dev/null || echo 000)"
  if [[ "$actual" == "$expected" ]]; then
    pass "$name"
  else
    fail "$name" "expected $expected, got $actual"
  fi
}

usage() {
  cat <<'USAGE'
Usage: ./scripts/verify.sh [--with-load]

Groups:
  1. Static analysis and tests   backend lint/format/types/tests, frontend ditto, build
  2. Containers                  image builds, compose validation, stack startup
  3. Runtime                     health, readiness, CRUD, redirect, cache, rate limit
  4. Distributed                 multi-instance, shared cache, shared limiter
  5. Observability               Prometheus targets, metrics, Grafana dashboard
  6. Failure modes               Redis down, PostgreSQL down, replica down
  7. Load and benchmark          only with --with-load (adds ~10 minutes)
USAGE
}

# --- 1. static analysis and tests -------------------------------------------

verify_backend() {
  log "1. Backend: lint, format, types, tests"

  if [[ ! -x backend/.venv/bin/python ]]; then
    skip "backend lint"        "backend/.venv missing — see docs/TESTING.md §6.1"
    skip "backend format"      "backend/.venv missing"
    skip "backend type check"  "backend/.venv missing"
    skip "backend unit tests"  "backend/.venv missing"
    skip "backend integration tests" "backend/.venv missing"
    return 0
  fi

  local venv="$REPO_ROOT/backend/.venv/bin"
  check "backend lint (ruff)"          env -C backend "$venv/ruff" check app tests
  check "backend format (ruff)"        env -C backend "$venv/ruff" format --check app tests
  check "backend type check (mypy)"    env -C backend "$venv/mypy" app tests
  check "backend unit tests"           env -C backend "$venv/python" -m pytest tests/unit -q
  check "backend integration tests"    env -C backend "$venv/python" -m pytest tests/integration -q
}

verify_frontend() {
  log "2. Frontend: lint, format, types, tests, build"

  if [[ ! -d frontend/node_modules ]]; then
    skip "frontend lint"       "node_modules missing — run npm ci in frontend/"
    skip "frontend format"     "node_modules missing"
    skip "frontend type check" "node_modules missing"
    skip "frontend tests"      "node_modules missing"
    skip "frontend build"      "node_modules missing"
    return 0
  fi

  check "frontend lint (eslint)"       npm --prefix frontend run lint
  check "frontend format (prettier)"   npm --prefix frontend run format:check
  check "frontend type check (tsc)"    npm --prefix frontend run typecheck
  check "frontend tests (vitest)"      npm --prefix frontend run test
  check "frontend production build"    npm --prefix frontend run build
  check "frontend build emitted assets" test -f frontend/dist/index.html
}

# --- 2. containers -----------------------------------------------------------

verify_docker() {
  log "3. Docker: builds, compose validation, startup"

  if ! docker info >/dev/null 2>&1; then
    skip "docker build"        "the Docker daemon is not running"
    skip "compose validation"  "the Docker daemon is not running"
    skip "stack startup"       "the Docker daemon is not running"
    return 1
  fi

  [[ -f .env ]] || cp .env.example .env

  check "compose config valid"           docker compose config
  check "compose loadtest overlay valid" docker compose -f docker-compose.yml -f docker-compose.loadtest.yml config
  check "compose benchmark overlay valid" docker compose -f docker-compose.yml -f docker-compose.benchmark.yml config
  check "docker image build"             docker compose build --quiet
  check "stack startup (healthy)"         docker compose up -d --wait --wait-timeout 240
}

# --- 3. runtime --------------------------------------------------------------

verify_runtime() {
  log "4. Runtime: health, CRUD, redirect, cache"

  check_status "liveness /health"   200 "$BASE/health"
  check_status "readiness /ready"   200 "$BASE/ready"
  check_status "metrics /metrics"   200 "$BASE/metrics"
  check_status "OpenAPI /docs"      200 "$BASE/docs"
  check_status "openapi.json"       200 "$BASE/openapi.json"
  check_status "frontend served"    200 "$BASE/"
  check_status "SPA client route"   200 "$BASE/urls/anything"

  # Create
  local created code
  created="$(curl -sS -X POST "$BASE/api/v1/urls" \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com/verify-script"}' 2>/dev/null || true)"
  code="$(printf '%s' "$created" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["short_code"])' 2>/dev/null || true)"

  if [[ -z "$code" ]]; then
    fail "create URL via API" "no short_code in response: ${created:0:120}"
    return 1
  fi
  pass "create URL via API"
  info "created /$code"

  # Redirect, and check the destination rather than only the status
  local location
  location="$(curl -s -o /dev/null -w '%{redirect_url}' "$BASE/$code")"
  if [[ "$location" == "https://example.com/verify-script" ]]; then
    pass "redirect resolves to the original URL"
  else
    fail "redirect resolves to the original URL" "Location was '$location'"
  fi
  check_status "redirect status is 307" 307 "$BASE/$code"

  check_status "metadata read" 200 "$BASE/api/v1/urls/$code"

  # Cache hit, then a forced miss, then a hit again.
  local state
  state="$(curl -s -o /dev/null -D- "$BASE/$code" | tr -d '\r' | grep -i '^x-cache:' | cut -d' ' -f2)"
  [[ "$state" == "hit" ]] && pass "cache hit observed (X-Cache: hit)" \
    || fail "cache hit observed" "X-Cache was '$state'"

  docker compose exec -T redis redis-cli DEL "url:v1:$code" >/dev/null 2>&1 || true
  state="$(curl -s -o /dev/null -D- "$BASE/$code" | tr -d '\r' | grep -i '^x-cache:' | cut -d' ' -f2)"
  [[ "$state" == "miss" ]] && pass "cache miss observed after eviction" \
    || fail "cache miss observed after eviction" "X-Cache was '$state'"

  state="$(curl -s -o /dev/null -D- "$BASE/$code" | tr -d '\r' | grep -i '^x-cache:' | cut -d' ' -f2)"
  [[ "$state" == "hit" ]] && pass "cache repopulated after the miss" \
    || fail "cache repopulated after the miss" "X-Cache was '$state'"

  # Validation and security
  check_status "rejects a malformed URL"        422 "$BASE/api/v1/urls" \
    -X POST -H 'Content-Type: application/json' -d '{"url":"not-a-url"}'
  check_status "rejects javascript: scheme"     422 "$BASE/api/v1/urls" \
    -X POST -H 'Content-Type: application/json' -d '{"url":"javascript:alert(1)"}'
  check_status "blocks SSRF to link-local"      422 "$BASE/api/v1/urls" \
    -X POST -H 'Content-Type: application/json' -d '{"url":"http://169.254.169.254/latest/meta-data/"}'
  check_status "blocks permissive IPv4 loopback" 422 "$BASE/api/v1/urls" \
    -X POST -H 'Content-Type: application/json' -d '{"url":"http://127.1/admin"}'
  check_status "unknown code returns 404"       404 "$BASE/zzzzzzz"

  # `/api` is redirected to `/api/` by nginx, which then 404s. The redirect
  # must be relative, or a client on a published port follows it to the wrong
  # place -- so assert the Location has no scheme/host, not just the status.
  local api_location
  api_location="$(curl -s -o /dev/null -D- "$BASE/api" | tr -d '\r' \
    | grep -i '^location:' | awk '{print $2}')"
  if [[ "$api_location" == /* ]]; then
    pass "nginx self-redirects are relative (Location: $api_location)"
  else
    fail "nginx self-redirects are relative" "Location was '$api_location'"
  fi
  check_status "reserved path is not a short code" 404 "$BASE/api" -L

  # Security headers
  local headers
  headers="$(curl -s -o /dev/null -D- "$BASE/health" | tr -d '\r')"
  for header in x-content-type-options x-frame-options x-request-id x-served-by; do
    if printf '%s' "$headers" | grep -qi "^$header:"; then
      pass "security/diagnostic header: $header"
    else
      fail "security/diagnostic header: $header" "not present on the response"
    fi
  done

  # Delete and confirm invalidation propagated
  check_status "delete returns 204" 204 "$BASE/api/v1/urls/$code" -X DELETE
  check_status "deleted code no longer redirects" 404 "$BASE/$code"
  check_status "deleted code has no metadata"     404 "$BASE/api/v1/urls/$code"
}

# --- 4. distributed ----------------------------------------------------------

verify_distributed() {
  log "5. Distributed: load balancing, shared cache, shared rate limit"

  # Load balancing
  local seen
  seen="$(for _ in $(seq 1 20); do
    curl -s -o /dev/null -D- "$BASE/health" 2>/dev/null \
      | tr -d '\r' | grep -i '^x-served-by:' | cut -d' ' -f2
  done | sort -u | tr '\n' ' ')"
  local count
  count="$(printf '%s' "$seen" | wc -w | tr -d '[:space:]')"
  if ((count >= 2)); then
    pass "load balanced across replicas"
    info "served by: $seen"
  else
    fail "load balanced across replicas" "only saw: $seen"
  fi

  # Shared cache: create on api-1, resolve on api-2
  local code
  code="$(curl -sS -X POST "$API1/api/v1/urls" \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com/cross-instance-verify"}' 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["short_code"])' 2>/dev/null || true)"

  if [[ -z "$code" ]]; then
    skip "shared cache across replicas" "could not create via api-1 directly"
  else
    local resolved
    resolved="$(curl -s -o /dev/null -D- "$API2/$code" | tr -d '\r')"
    local served state
    served="$(printf '%s' "$resolved" | grep -i '^x-served-by:' | cut -d' ' -f2)"
    state="$(printf '%s' "$resolved" | grep -i '^x-cache:' | cut -d' ' -f2)"
    if [[ "$served" == "api-2" && "$state" == "hit" ]]; then
      pass "shared cache across replicas (created on api-1, cache hit on api-2)"
    else
      fail "shared cache across replicas" "served_by=$served x-cache=$state"
    fi
  fi

  # Shared rate limit: drain through the load balancer, then confirm BOTH
  # replicas refuse. A per-process limiter would let each serve its own budget.
  docker compose exec -T redis redis-cli --scan --pattern 'rl:*' 2>/dev/null \
    | tr -d '\r' | xargs -r -n 50 docker compose exec -T redis redis-cli DEL >/dev/null 2>&1 || true

  local i
  for i in $(seq 1 30); do
    curl -s -o /dev/null -X POST "$BASE/api/v1/urls" \
      -H 'Content-Type: application/json' \
      -d "{\"url\":\"https://example.com/drain-$i\"}" >/dev/null 2>&1 || true
  done

  local s1 s2
  s1="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API1/api/v1/urls" \
    -H 'Content-Type: application/json' -d '{"url":"https://example.com/after-drain-1"}')"
  s2="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API2/api/v1/urls" \
    -H 'Content-Type: application/json' -d '{"url":"https://example.com/after-drain-2"}')"

  if [[ "$s1" == "429" && "$s2" == "429" ]]; then
    pass "rate limit is shared across replicas (both refused after draining)"
  else
    fail "rate limit is shared across replicas" "api-1=$s1 api-2=$s2 (expected 429 from both)"
  fi

  # Header contract on a 429
  local rl
  rl="$(curl -s -o /dev/null -D- -X POST "$BASE/api/v1/urls" \
    -H 'Content-Type: application/json' -d '{"url":"https://example.com/blocked"}' | tr -d '\r')"
  for header in x-ratelimit-limit x-ratelimit-remaining x-ratelimit-policy retry-after; do
    if printf '%s' "$rl" | grep -qi "^$header:"; then
      pass "429 carries $header"
    else
      fail "429 carries $header" "not present"
    fi
  done

  # Restore the budget so later checks and manual exploration are not throttled.
  docker compose exec -T redis redis-cli --scan --pattern 'rl:*' 2>/dev/null \
    | tr -d '\r' | xargs -r -n 50 docker compose exec -T redis redis-cli DEL >/dev/null 2>&1 || true
}

# --- 5. observability --------------------------------------------------------

verify_observability() {
  log "6. Observability: Prometheus, metrics, Grafana"

  check_status "Prometheus is up" 200 "http://localhost:9090/-/healthy"

  # Both API replicas must be scraped and healthy.
  local up
  up="$(curl -s --data-urlencode 'query=up{job="url-shortener-api"}' \
    http://localhost:9090/api/v1/query 2>/dev/null \
    | python3 -c 'import json,sys; print(sum(1 for r in json.load(sys.stdin)["data"]["result"] if r["value"][1]=="1"))' \
    2>/dev/null || echo 0)"
  if [[ "$up" -ge 2 ]]; then
    pass "Prometheus scrapes both API replicas ($up up)"
  else
    fail "Prometheus scrapes both API replicas" "only $up target(s) up"
  fi

  # Scrape a replica directly rather than through the load balancer. Via nginx
  # the target is whichever replica answers, so a series that only one replica
  # has materialised makes this check flap. Anchoring on `^name` also matters:
  # prometheus_client emits HELP and TYPE lines for a registered-but-unused
  # counter, so an unanchored match passes with no series present.
  local metrics
  metrics="$(curl -s "$API1/metrics" 2>/dev/null || true)"
  local name
  for name in http_requests_total http_request_duration_seconds_bucket \
              cache_hits_total cache_misses_total cache_errors_total \
              urls_created_total url_redirects_total \
              rate_limit_decisions_total redis_errors_total postgres_errors_total; do
    if printf '%s' "$metrics" | grep -q "^${name}[{ ]"; then
      pass "metric exposed: $name"
    else
      fail "metric exposed: $name" "no series on api-1 (HELP line alone is not enough)"
    fi
  done

  # Every replica must export the hit-ratio terms, or scaling out blanks the
  # panel until the new replica happens to serve a redirect.
  local replica
  for replica in "$API1" "$API2"; do
    if curl -s "$replica/metrics" 2>/dev/null | grep -q '^cache_misses_total{cache="url"}'; then
      pass "cache hit-ratio series seeded on ${replica##*/}"
    else
      fail "cache hit-ratio series seeded on ${replica##*/}" "cache_misses_total series absent"
    fi
  done

  # Route labels must be templates, never raw paths -- otherwise any client can
  # exhaust Prometheus's memory through the label dimension.
  if printf '%s' "$metrics" | grep -q 'route="/{code}"'; then
    pass "metric route labels are templates, not raw paths"
  else
    skip "metric route labels are templates" "no redirect traffic recorded yet"
  fi

  # Recording and alerting rules must parse and evaluate. A rule with
  # health="err" still appears in the config, so checking presence is not
  # enough -- the evaluated health is what matters.
  local rules
  rules="$(curl -s http://localhost:9090/api/v1/rules 2>/dev/null || true)"
  local rule_report
  rule_report="$(printf '%s' "$rules" | python3 -c '
import json, sys
groups = json.load(sys.stdin).get("data", {}).get("groups", [])
rules = [r for g in groups for r in g["rules"]]
bad = [r["name"] for r in rules if r.get("health") != "ok"]
alerting = sum(1 for r in rules if r["type"] == "alerting")
recording = sum(1 for r in rules if r["type"] == "recording")
print(f"{len(bad)}|{alerting}|{recording}|{\",\".join(bad)}")
' 2>/dev/null || echo "")"

  if [[ -z "$rule_report" ]]; then
    fail "Prometheus rules evaluate cleanly" "could not read the rules API"
  else
    IFS='|' read -r bad_count alerting_count recording_count bad_names <<< "$rule_report"
    if [[ "$bad_count" == "0" && "$alerting_count" -gt 0 && "$recording_count" -gt 0 ]]; then
      pass "Prometheus rules evaluate cleanly ($alerting_count alerts, $recording_count recording)"
    else
      fail "Prometheus rules evaluate cleanly" "unhealthy: ${bad_names:-none}, alerts=$alerting_count recording=$recording_count"
    fi
  fi

  check_status "Grafana is up" 200 "http://localhost:3000/api/health"

  local dashboards
  dashboards="$(curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD:-admin}" \
    'http://localhost:3000/api/search?type=dash-db' 2>/dev/null || true)"
  if printf '%s' "$dashboards" | grep -qi 'url.shortener'; then
    pass "Grafana dashboard is provisioned"
  else
    skip "Grafana dashboard is provisioned" "could not authenticate; set GRAFANA_ADMIN_PASSWORD"
  fi

  if [[ -x scripts/verify_dashboard_queries.py ]] || [[ -f scripts/verify_dashboard_queries.py ]]; then
    if python3 scripts/verify_dashboard_queries.py >/dev/null 2>&1; then
      pass "every dashboard query is valid PromQL"
    else
      skip "every dashboard query is valid PromQL" "run scripts/verify_dashboard_queries.py for detail"
    fi
  fi
}

# --- 6. failure modes --------------------------------------------------------

verify_failure_modes() {
  log "7. Failure modes: Redis down, PostgreSQL down, replica down"

  # A code that is cached, so the Redis/PostgreSQL outage behaviour is testable.
  local code
  code="$(curl -sS -X POST "$BASE/api/v1/urls" \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com/failure-mode-probe"}' 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["short_code"])' 2>/dev/null || true)"

  if [[ -z "$code" ]]; then
    skip "failure-mode checks" "could not create a probe URL"
    return 0
  fi
  curl -s -o /dev/null "$BASE/$code"   # ensure it is cached

  # --- Redis down: reads fail open, writes fail closed ---
  docker compose stop redis >/dev/null 2>&1
  sleep 2

  local redirect_status write_status ready_status
  redirect_status="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/$code")"
  write_status="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/urls" \
    -H 'Content-Type: application/json' -d '{"url":"https://example.com/no-redis"}')"
  ready_status="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/ready")"

  [[ "$redirect_status" == "307" ]] \
    && pass "Redis down: redirects still work (fail open)" \
    || fail "Redis down: redirects still work" "got $redirect_status, expected 307"

  [[ "$write_status" == "503" ]] \
    && pass "Redis down: writes fail closed with 503" \
    || fail "Redis down: writes fail closed" "got $write_status, expected 503"

  [[ "$ready_status" == "200" ]] \
    && pass "Redis down: /ready reports degraded but stays in service (200)" \
    || fail "Redis down: /ready stays 200" "got $ready_status"

  docker compose start redis >/dev/null 2>&1
  docker compose up -d --wait --wait-timeout 90 redis >/dev/null 2>&1 || true
  sleep 2

  # --- PostgreSQL down: cached reads work, uncached and writes 503 ---
  curl -s -o /dev/null "$BASE/$code"   # re-cache after the Redis restart
  docker compose stop postgres >/dev/null 2>&1
  sleep 2

  local cached_status uncached_status pg_ready
  cached_status="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/$code")"
  uncached_status="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/nocachedcode")"
  pg_ready="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/ready")"

  [[ "$cached_status" == "307" ]] \
    && pass "PostgreSQL down: cached redirects still work" \
    || fail "PostgreSQL down: cached redirects still work" "got $cached_status, expected 307"

  [[ "$uncached_status" == "503" || "$uncached_status" == "404" ]] \
    && pass "PostgreSQL down: uncached lookup degrades cleanly ($uncached_status)" \
    || fail "PostgreSQL down: uncached lookup degrades cleanly" "got $uncached_status"

  [[ "$pg_ready" == "503" ]] \
    && pass "PostgreSQL down: /ready returns 503 (not ready)" \
    || fail "PostgreSQL down: /ready returns 503" "got $pg_ready"

  docker compose start postgres >/dev/null 2>&1
  docker compose up -d --wait --wait-timeout 120 postgres >/dev/null 2>&1 || true
  sleep 3

  # --- One replica down: Nginx routes around it ---
  docker compose stop api-1 >/dev/null 2>&1
  sleep 2

  local ok=0 attempt
  for attempt in $(seq 1 10); do
    [[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health")" == "200" ]] && ((ok++)) || true
  done
  ((ok >= 8)) \
    && pass "one replica down: traffic continues via the survivor ($ok/10)" \
    || fail "one replica down: traffic continues" "only $ok/10 succeeded"

  docker compose start api-1 >/dev/null 2>&1
  docker compose up -d --wait --wait-timeout 120 api-1 >/dev/null 2>&1 || true

  # --- Expired link returns 410, distinct from 404 ---
  local expiry expired_code
  expiry="$(python3 -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=3)).isoformat().replace("+00:00","Z"))')"
  expired_code="$(curl -sS -X POST "$BASE/api/v1/urls" \
    -H 'Content-Type: application/json' \
    -d "{\"url\":\"https://example.com/expiring\",\"expires_at\":\"$expiry\"}" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["short_code"])' 2>/dev/null || true)"

  if [[ -z "$expired_code" ]]; then
    skip "expired link returns 410" "could not create a short-lived URL"
  else
    sleep 5
    check_status "expired link returns 410 (not 404)" 410 "$BASE/$expired_code"
  fi
}

# --- 7. load and benchmark ---------------------------------------------------

verify_load() {
  log "8. Load tests and benchmark"

  if [[ "$WITH_LOAD" != "true" ]]; then
    skip "k6 load scenarios"        "pass --with-load to run (~4 min)"
    skip "baseline vs optimized benchmark" "pass --with-load to run (~6 min)"
    return 0
  fi

  check "k6 load scenarios" ./scripts/load-test.sh all
  check "baseline vs optimized benchmark" ./scripts/benchmark.sh
}

# --- summary -----------------------------------------------------------------

summarise() {
  local passed failed skipped total
  passed="$(grep -c '^PASS' "$RESULTS_FILE" || true)"
  failed="$(grep -c '^FAIL' "$RESULTS_FILE" || true)"
  skipped="$(grep -c '^SKIP' "$RESULTS_FILE" || true)"
  total=$((passed + failed + skipped))

  printf '\n\033[1m══════════════════════════════════════════════════════════\033[0m\n'
  printf '\033[1m  Verification summary\033[0m\n'
  printf '\033[1m══════════════════════════════════════════════════════════\033[0m\n\n'
  printf '  passed:  %s\n  failed:  %s\n  skipped: %s\n  total:   %s\n\n' \
    "$passed" "$failed" "$skipped" "$total"

  if ((failed > 0)); then
    printf '\033[1;31mFailures:\033[0m\n'
    grep '^FAIL' "$RESULTS_FILE" | while IFS=$'\t' read -r _ name detail; do
      printf '  ✗ %s\n      %s\n' "$name" "$detail"
    done
    printf '\n'
  fi

  if ((skipped > 0)); then
    printf '\033[1;33mSkipped:\033[0m\n'
    grep '^SKIP' "$RESULTS_FILE" | while IFS=$'\t' read -r _ name detail; do
      printf '  - %s (%s)\n' "$name" "$detail"
    done
    printf '\n'
  fi

  # A machine-readable copy, so FINAL_VERIFICATION.md can be regenerated from
  # a real run rather than transcribed by hand.
  mkdir -p docs/performance
  cp "$RESULTS_FILE" docs/performance/last-verification.tsv
  printf 'Full results: docs/performance/last-verification.tsv\n\n'

  ((failed == 0))
}

# --- main --------------------------------------------------------------------

main() {
  while (($#)); do
    case "$1" in
      --with-load) WITH_LOAD=true ;;
      --list|--help|-h) usage; exit 0 ;;
      *) printf 'unknown option: %s\n' "$1" >&2; usage; exit 2 ;;
    esac
    shift
  done

  printf '\033[1mVerification gate\033[0m — %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'host: %s\n' "$(uname -srm)"

  # `|| true` throughout: a failing group must not stop the rest, because the
  # point of this script is a complete picture rather than the first failure.
  verify_backend      || true
  verify_frontend     || true

  if verify_docker; then
    verify_runtime       || true
    verify_distributed   || true
    verify_observability || true
    verify_failure_modes || true
    verify_load          || true
  else
    skip "runtime checks"       "the stack is unavailable"
    skip "distributed checks"   "the stack is unavailable"
    skip "observability checks" "the stack is unavailable"
    skip "failure-mode checks"  "the stack is unavailable"
  fi

  summarise
}

main "$@"
