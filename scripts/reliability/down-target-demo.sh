#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

API_GATEWAY_PORT="${API_GATEWAY_PORT:-8080}"
restore_stack=0

if docker compose ps --services --status running | grep -q .; then
  restore_stack=1
fi

pass() {
  printf 'PASS %s\n' "$1"
}

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

cleanup() {
  docker compose down >/dev/null 2>&1 || true
  unset PING_TARGET_URLS PING_INTERVAL_SECONDS PING_RETRY_COUNT
  if [[ "$restore_stack" -eq 1 ]]; then
    docker compose up -d >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT

export PING_TARGET_URLS="http://127.0.0.1:9"
export PING_INTERVAL_SECONDS="5"
export PING_RETRY_COUNT="2"

# --build: test the code in this checkout, not whatever image is cached.
docker compose up -d --build --force-recreate ping-agent api-gateway >/dev/null

api_ready=0
for _ in {1..30}; do
  if curl -fsS "http://localhost:${API_GATEWAY_PORT}/healthz" >/dev/null 2>&1; then
    pass "api health"
    api_ready=1
    break
  fi
  sleep 2
done

[[ "$api_ready" -eq 1 ]] || fail "api health"

# Wait for the first completed check: before it, the target is "unknown", not down.
status_body=""
for _ in {1..20}; do
  status_body="$(curl -fsS "http://localhost:${API_GATEWAY_PORT}/status")"
  if [[ "$status_body" == *'"state":"down"'* ]]; then
    break
  fi
  sleep 2
done

python3 - "$status_body" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
targets = payload.get("targets", [])
assert targets, "no targets returned"
target = targets[0]
assert target["url"] == "http://127.0.0.1:9", target
assert target["state"] == "down" and target["up"] is False, target
assert target["reason"] == "connection_refused", target
assert target["latency_ms"] is None, target  # no response, so no latency
assert target["total_checks"] >= 1, target
assert target["availability"] == 0, target
assert payload["status"] == "outage", payload["status"]
PY

pass "down target produces failure signal"
pass "verification completed"
