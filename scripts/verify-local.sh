#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

API_GATEWAY_PORT="${API_GATEWAY_PORT:-8080}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
ALERTMANAGER_PORT="${ALERTMANAGER_PORT:-9093}"
PING_AGENT_PORT="${PING_AGENT_PORT:-18080}"

pass() {
  printf 'PASS %s\n' "$1"
}

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

wait_for_http() {
  local name="$1"
  local url="$2"
  local attempts="${3:-30}"
  local delay="${4:-2}"

  for ((i = 0; i < attempts; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      pass "$name"
      return 0
    fi
    sleep "$delay"
  done

  fail "$name"
}

wait_for_body() {
  local name="$1"
  local url="$2"
  local needle="$3"
  local attempts="${4:-30}"
  local delay="${5:-2}"

  local body
  for ((i = 0; i < attempts; i++)); do
    body="$(curl -fsS "$url" 2>/dev/null)" || body=""
    if [[ "$body" == *"$needle"* ]]; then
      pass "$name"
      return 0
    fi
    sleep "$delay"
  done

  fail "$name (no $needle at $url)"
}

assert_contains() {
  local name="$1"
  local body="$2"
  local needle="$3"

  [[ "$body" == *"$needle"* ]] || fail "$name"
}

has_service() {
  local name="$1"
  printf '%s\n' "$COMPOSE_SERVICES" | grep -qx "$name"
}

require_cmd docker
require_cmd curl

COMPOSE_SERVICES="$(docker compose config --services)"
docker compose config >/dev/null
pass "docker compose config"

if ! up_output="$(docker compose up -d --build 2>&1)"; then
  printf '%s\n' "$up_output" | tail -5 >&2
  fail "docker compose up (a busy host port can be moved with e.g. GRAFANA_PORT=3001 in .env)"
fi
pass "docker compose up"

wait_for_http "api healthz" "http://localhost:${API_GATEWAY_PORT}/healthz"

healthz_body="$(curl -fsS "http://localhost:${API_GATEWAY_PORT}/healthz")"
assert_contains "api healthz body" "$healthz_body" '"status"'
pass "api healthz body"

status_body="$(curl -fsS "http://localhost:${API_GATEWAY_PORT}/status")"
assert_contains "status endpoint" "$status_body" '"targets"'
assert_contains "status endpoint reports per-target state" "$status_body" '"state"'
assert_contains "status endpoint reports per-region results" "$status_body" '"regions"'
pass "status endpoint"

wait_for_body "ping-agent publishes its targets" "http://localhost:${PING_AGENT_PORT}/metrics" 'ping_target_info{'

page_body="$(curl -fsS "http://localhost:${API_GATEWAY_PORT}/")"
assert_contains "status page" "$page_body" '<title>iYup status</title>'
pass "status page"

targets_body="$(curl -fsS "http://localhost:${API_GATEWAY_PORT}/targets")"
assert_contains "targets endpoint" "$targets_body" '"url"'
pass "targets endpoint"

metrics_body="$(curl -fsS "http://localhost:${API_GATEWAY_PORT}/metrics")"
assert_contains "metrics endpoint" "$metrics_body" 'api_gateway_requests_total'
pass "metrics endpoint"

if has_service prometheus; then
  wait_for_http "prometheus ready" "http://localhost:${PROMETHEUS_PORT}/-/ready"

  rules_body="$(curl -fsS "http://localhost:${PROMETHEUS_PORT}/api/v1/rules")"
  for rule in iyup:target_up:quorum iyup:target_error_ratio:rate1h \
    TargetDown ErrorBudgetBurnFast ErrorBudgetBurnSlow TargetRegionalFailure TargetSlow TargetChecksStale \
    CertificateExpiringSoon DomainExpiringSoon AllPingAgentsDown PingAgentDown ApiGatewayDown Watchdog; do
    assert_contains "rule $rule loaded" "$rules_body" "\"$rule\""
  done
  pass "recording and alert rules loaded"

  wait_for_http "history endpoint" "http://localhost:${API_GATEWAY_PORT}/history?window=1h&buckets=4"
  wait_for_http "outages endpoint" "http://localhost:${API_GATEWAY_PORT}/outages?window=1h"
  wait_for_http "error budget endpoint" "http://localhost:${API_GATEWAY_PORT}/slo?window=1h"
fi

if has_service grafana; then
  wait_for_http "grafana ready" "http://localhost:${GRAFANA_PORT}/login"
fi

if has_service alertmanager; then
  wait_for_http "alertmanager ready" "http://localhost:${ALERTMANAGER_PORT}/-/ready"
  # The always-firing Watchdog proves the whole path: rules evaluate, Prometheus
  # reaches Alertmanager, and Alertmanager accepts alerts.
  wait_for_body "heartbeat reaches alertmanager" \
    "http://localhost:${ALERTMANAGER_PORT}/api/v2/alerts?filter=alertname%3D%22Watchdog%22" '"Watchdog"' 45
  wait_for_body "status API reads maintenance windows" \
    "http://localhost:${API_GATEWAY_PORT}/status" '"alertmanager":"reachable"'
fi
