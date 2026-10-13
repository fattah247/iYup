# Demo Commands

Load the host ports:

```bash
set -a
test -f .env && . ./.env
set +a

API_GATEWAY_PORT="${API_GATEWAY_PORT:-8080}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
ALERTMANAGER_PORT="${ALERTMANAGER_PORT:-9093}"
```

## Start

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
docker compose ps
```

## Status API

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/healthz"
```

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/status" | python3 -m json.tool
```

One line per target, with each region's verdict:

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/status" | python3 -c '
import json, sys
for t in json.load(sys.stdin)["targets"]:
    regions = " ".join(r["region"] + "=" + r["state"] for r in t["regions"])
    print("%-8s %-20s %-20s %s" % (t["state"], t["reason"], t["name"], regions))'
```

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/history?window=1h&buckets=12" | python3 -m json.tool
```

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/outages?window=1h" | python3 -m json.tool
```

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/slo?window=30d" | python3 -m json.tool
```

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/metrics" | head -40
```

## Alerts

```bash
curl -fsS "http://localhost:${PROMETHEUS_PORT}/api/v1/alerts" | python3 -m json.tool
```

What the demo receivers were sent (page, notify, heartbeat):

```bash
docker compose logs demo-targets
```

Send a test notification through the real routing:

```bash
./scripts/send-test-alert.py
```

```bash
./scripts/send-test-alert.py --critical
```

## Maintenance

```bash
./scripts/maintenance.py start http://demo-targets:8080/down --duration 45m --comment "Replacing the disk"
```

```bash
./scripts/maintenance.py list
```

```bash
./scripts/maintenance.py end http://demo-targets:8080/down
```

## Prometheus and Grafana

```bash
curl -fsS "http://localhost:${PROMETHEUS_PORT}/-/ready"
```

```bash
curl -fsS "http://localhost:${PROMETHEUS_PORT}/api/v1/targets" | python3 -m json.tool
```

```bash
python3 scripts/check_dashboard_queries.py "http://localhost:${PROMETHEUS_PORT}"
```

## Checks

```bash
./scripts/verify-local.sh
```

```bash
helm template iyup ./charts/iyup
```
