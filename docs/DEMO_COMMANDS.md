# Demo Commands

```bash
set -a
test -f .env && . ./.env
set +a

API_GATEWAY_PORT="${API_GATEWAY_PORT:-8080}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
ALERTMANAGER_PORT="${ALERTMANAGER_PORT:-9093}"
```

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
docker compose ps
```

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/healthz"
```

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/status" | python3 -m json.tool
```

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/history?window=1h&buckets=12" | python3 -m json.tool
```

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/outages?window=1h" | python3 -m json.tool
```

```bash
curl -fsS "http://localhost:${PROMETHEUS_PORT}/api/v1/alerts" | python3 -m json.tool
```

```bash
python3 scripts/check_dashboard_queries.py "http://localhost:${PROMETHEUS_PORT}"
```

```bash
curl -fsS "http://localhost:${API_GATEWAY_PORT}/metrics" | head -40
```

```bash
curl -fsS "http://localhost:${PROMETHEUS_PORT}/-/ready"
```

```bash
curl -fsS "http://localhost:${PROMETHEUS_PORT}/api/v1/targets" | python3 -m json.tool
```

```bash
helm template iyup ./charts/iyup
```

```bash
./scripts/verify-local.sh
```
