# Troubleshooting

## Docker daemon is not available

Run:

```bash
docker ps
```

If that fails, start Docker Desktop or switch back from the Minikube Docker context:

```bash
eval "$(minikube docker-env -u)"
```

## `api-gateway` is up but the host port is different

Check `.env` first:

```bash
cat .env
docker compose ps
```

This repo supports host port overrides such as `API_GATEWAY_PORT=8081`.

## Prometheus targets are down

Run:

```bash
curl -fsS http://localhost:9090/api/v1/targets | python3 -m json.tool
docker compose ps
```

If `ping-agent` or `api-gateway` is unhealthy, restart the stack:

```bash
docker compose up -d
```

## The ping-agent exits right after starting

Read its log:

```bash
docker compose logs ping-agent
```

A `config:` line means a setting was rejected at startup. The most common cause is an entry in `PING_TARGET_URLS` that is not an absolute `http(s)://` URL (for example `google.com`); the message names every bad entry. `PING_HTTP_METHOD` must be `GET` or `HEAD`.

## A target shows `unknown`

Look at its `reason` and `stale` fields in `/status`:

- `pending`: it has not been checked yet; wait one interval
- `not_monitored`: `PING_TARGET_URLS` is set on the gateway and lists a URL the agent does not check; remove the gateway setting to use the agent's list
- `stale: true`: the agent stopped finishing checks; see [TargetChecksStale](OPERATIONS_RUNBOOK.md#targetchecksstale)

## The status page says history is unavailable

`/history` and `/outages` need Prometheus. Check `PROMETHEUS_URL` on the gateway and `curl -fsS http://localhost:9090/-/ready`. Current status keeps working without Prometheus.

## Grafana panel has no data

Check Prometheus before Grafana, then run every dashboard query:

```bash
curl -fsS http://localhost:9090/api/v1/targets | python3 -m json.tool
python3 scripts/check_dashboard_queries.py http://localhost:9090
```

## An alert rule change has no effect

Prometheus reads rules at startup:

```bash
docker compose restart prometheus
curl -fsS http://localhost:9090/api/v1/rules | python3 -m json.tool | grep '"name"'
```

## Minikube cannot pull the images

Build inside the Minikube Docker context:

```bash
eval "$(minikube -p minikube docker-env)"
docker build -t ping-agent:latest services/ping-agent
docker build -t api-gateway:latest services/api-gateway
```

## Helm render fails

Run:

```bash
helm lint ./charts/iyup
helm template iyup ./charts/iyup
```

Most failures here come from chart values or template syntax, not from a live cluster.
