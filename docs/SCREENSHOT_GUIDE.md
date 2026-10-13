# Screenshot Guide

Use these commands and URLs to capture the portfolio screenshots.

The current set was captured on 2026-10-13 from the Docker Compose stack with the demo overlay (`docker-compose.demo.yml`), so every state is on screen: two agents labelled as regions, a maintenance window, a regional failure, a slow target, and an error-budget burn. Browser pages (`05`–`08`, `11`) are headless Chromium captures of the running services. Terminal shots (`01`–`04`, `09`, `10`) are the real output of the listed commands, rendered in a terminal-window frame. `07` and `11` come from a stack that had been running for about an hour, so they show history. The demo stack ran on non-default host ports, set through the variables below; `10` ran against a second stack on the default ports with Grafana moved to 23001, because port 3000 was taken on the capture machine.

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
| `docs/screenshots/01-repo-overview.png` | `git ls-files -co --exclude-standard \| tree --fromfile . -L 2` and `sed -n '1,12p' README.md` | repo layout and first screen | lists only files git would publish, not local or ignored ones |
| `docs/screenshots/02-docker-compose-stack.png` | `docker compose -f docker-compose.yml -f docker-compose.demo.yml ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'` | the stack, with two agents and the demo targets, is up | keep all services visible |
| `docs/screenshots/03-api-healthz.png` | `curl -fsS "http://localhost:${API_GATEWAY_PORT}/healthz"` | API health endpoint works | capture the JSON body |
| `docs/screenshots/04-api-status-response.png` | `curl -fsS "http://localhost:${API_GATEWAY_PORT}/status" \| jq '{status, summary, maintenance: [...]}'`, then one line per target with each region's verdict (full commands are in the shot) | the status API returns majority state, reasons, slowness, regions and maintenance | keep a down, a slow and a regional target visible |
| `docs/screenshots/05-prometheus-targets.png` | `http://localhost:${PROMETHEUS_PORT}/targets` | Prometheus scrapes the gateway and both agents | |
| `docs/screenshots/06-prometheus-metrics.png` | `http://localhost:${PROMETHEUS_PORT}/graph?g0.expr=ping_last_result%20%3D%3D%201&g0.tab=1` | each region's latest result and its reason, per target | show the table view |
| `docs/screenshots/07-grafana-dashboard.png` | `http://localhost:${GRAFANA_PORT}/d/iyup-overview?orgId=1&kiosk&from=now-1h&to=now&theme=dark` | the dashboard is provisioned and follows the configured targets | run the demo overlay for an hour first |
| `docs/screenshots/08-alertmanager.png` | `http://localhost:${ALERTMANAGER_PORT}/#/alerts?silenced=true&inhibited=true`, groups expanded | alerts reach Alertmanager and are routed to `page`, `notify` and `heartbeat`; the maintenance window silences one | start a maintenance window first (`scripts/maintenance.py`) |
| `docs/screenshots/09-helm-template.png` | `helm lint ./charts/iyup` and `helm template ... --set-json 'checks=...' --show-only templates/ping-agent-targets-configmap.yaml` | the chart lints and renders checks as code | no secrets in the output |
| `docs/screenshots/10-local-verification.png` | `./scripts/verify-local.sh` | local verification is repeatable | keep the full PASS list visible |
| `docs/screenshots/11-status-page.png` | `http://localhost:${API_GATEWAY_PORT}/?window=1h` | the status page explains state, regions, maintenance, history and outages | run the demo overlay first; a 1h window fills fastest |

The two browser captures can be taken headlessly with any Chromium browser (shown with Edge; swap in Chrome's path if you prefer):

```bash
BROWSER="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
"$BROWSER" --headless=new --user-data-dir="$(mktemp -d)" --hide-scrollbars --force-device-scale-factor=2 \
  --window-size=1200,1700 --virtual-time-budget=8000 \
  --screenshot=docs/screenshots/11-status-page.png "http://localhost:${API_GATEWAY_PORT}/?window=1h"
```

Grafana needs a login or anonymous viewing for a headless capture; for a local-only capture you can start it with `GF_AUTH_ANONYMOUS_ENABLED=true` and `GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer`.
