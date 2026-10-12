# Screenshot Guide

Use these commands and URLs to capture the portfolio screenshots.

The current set was captured on 2026-10-13 from the Docker Compose stack with the demo overlay (`docker-compose.demo.yml`), so every state is on screen. Browser pages (`05`–`08`, `11`) are headless Chromium captures of the running services. Terminal shots (`01`–`04`, `09`, `10`) are the real output of the listed commands, run in this repo with `.env` loaded, then rendered in a terminal-window frame. `07` and `11` come from a stack that had been running for about an hour, so they show history.

```bash
set -a
test -f .env && . ./.env
set +a

API_GATEWAY_PORT="${API_GATEWAY_PORT:-8080}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
ALERTMANAGER_PORT="${ALERTMANAGER_PORT:-9093}"
```

| File | Command or URL | What it proves | Capture note |
|---|---|---|---|
| `docs/screenshots/01-repo-overview.png` | `tree -L 2 -I '.git\|__pycache__\|node_modules'` and `sed -n '1,18p' README.md` | repo layout and first screen | keep the title and docs list visible |
| `docs/screenshots/02-docker-compose-stack.png` | `docker compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'` | local stack is up | keep all services visible |
| `docs/screenshots/03-api-healthz.png` | `curl -fsS "http://localhost:${API_GATEWAY_PORT}/healthz"` | API health endpoint works | capture the JSON body |
| `docs/screenshots/04-api-status-response.png` | `curl -fsS "http://localhost:${API_GATEWAY_PORT}/status" \| jq '{status, summary, targets: [.targets[] \| {name, state, reason, http_status, latency_ms, last_checked_seconds_ago}]}'` | status API returns live state with reasons | keep one down target visible |
| `docs/screenshots/05-prometheus-targets.png` | `http://localhost:${PROMETHEUS_PORT}/targets` | Prometheus is scraping the jobs | keep `api-gateway` and `ping-agent` visible |
| `docs/screenshots/06-prometheus-metrics.png` | `http://localhost:${PROMETHEUS_PORT}/graph?g0.expr=ping_last_result&g0.tab=1` | Prometheus holds each target's latest result and its reason | show the current vector result |
| `docs/screenshots/07-grafana-dashboard.png` | `http://localhost:${GRAFANA_PORT}/d/iyup-overview?kiosk` | Grafana dashboard is provisioned and follows the configured targets | run the demo overlay first so every state is on screen |
| `docs/screenshots/08-alertmanager.png` | `http://localhost:${ALERTMANAGER_PORT}/#/alerts` | real alerts reach Alertmanager | with the demo overlay, `TargetDown` fires about a minute after start |
| `docs/screenshots/09-helm-template.png` | `helm template iyup ./charts/iyup --show-only templates/grafana-deployment.yaml \| sed -n '1,30p'` | Helm rendering works | avoid secret output |
| `docs/screenshots/10-local-verification.png` | `./scripts/verify-local.sh` | local verification is repeatable | keep the full PASS list visible |
| `docs/screenshots/11-status-page.png` | `http://localhost:${API_GATEWAY_PORT}/?window=1h` | the status page explains state, history and outages | run the demo overlay first; a 1h window fills fastest |

The two browser captures can be taken headlessly with any Chromium browser (shown with Edge; swap in Chrome's path if you prefer):

```bash
BROWSER="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
"$BROWSER" --headless=new --user-data-dir="$(mktemp -d)" --hide-scrollbars --force-device-scale-factor=2 \
  --window-size=1200,1700 --virtual-time-budget=8000 \
  --screenshot=docs/screenshots/11-status-page.png "http://localhost:${API_GATEWAY_PORT}/?window=1h"
```

Grafana needs a login or anonymous viewing for a headless capture; for a local-only capture you can start it with `GF_AUTH_ANONYMOUS_ENABLED=true` and `GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer`.
