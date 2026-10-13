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

A `config:` line means the configuration was rejected; it lists every problem at once. Common causes: a URL without `http://` or `https://`, a misspelled field in the targets file (unknown fields are errors), a `${VAR}` the agent's environment does not have, the same target listed twice, or `PING_HTTP_METHOD` other than `GET` or `HEAD`. Check a file without starting anything:

```bash
cd services/ping-agent && go run . validate ../../config/targets/targets.yml
```

## The agent ignores the targets file

A non-empty `PING_TARGET_URLS` overrides `config/targets/targets.yml`. Older `.env.example` files set it, so check `.env`:

```bash
grep PING_TARGET_URLS .env
docker compose logs ping-agent | grep "checking"
```

The startup line says which source was used. Comment out `PING_TARGET_URLS` and run `docker compose up -d ping-agent`.

## A target is down but works in a browser

Read the `reason` and `http_status` in `/status`:

- `http_4xx` with 403 or 429: bot protection or rate limiting. The agent sends `User-Agent: iyup-ping-agent/2`; allow it, or check a health endpoint that is not behind the protection
- `http_4xx` with 405: the server rejects `HEAD`; use `method: GET`
- `unexpected_status`: the code is outside `expect.status`. Redirects are followed by default, so this is the final page's code; `follow_redirects: false` checks the redirect itself
- `body_mismatch`: the expected text is not in the first 64 KB of the body; the page may be rendered by JavaScript, which the agent does not run
- `tls`: the certificate is expired, self-signed or for another name; browsers may be using a cached exception
- `timeout` or `dns` from one region only: a regional network problem; the target stays up until most regions agree

## A target shows `unknown`

Look at its `reason` and `stale` fields in `/status`:

- `pending`: it has not been checked yet; wait one interval
- `not_monitored`: `PING_TARGET_URLS` is set on the gateway and lists a target no agent checks; remove the gateway setting to use the agents' list
- `stale: true`: no region finishes checks any more; see [TargetChecksStale](OPERATIONS_RUNBOOK.md#targetchecksstale)

## Regions disagree

`regions` in `/status` and the dashboard's "By region" panel show each region's own verdict. A failure in one region while others succeed is usually that region's network, DNS or a CDN edge, not the service. It raises `TargetRegionalFailure`, not a page. See [the runbook](OPERATIONS_RUNBOOK.md#targetregionalfailure).

## The status page says history is unavailable

`/history`, `/outages` and `/slo` need Prometheus. Check `PROMETHEUS_URL` on the gateway and `curl -fsS http://localhost:9090/-/ready`. Current status keeps working without Prometheus.

## The error budget looks wrong on a new install

`/slo` divides observed downtime by the budget for the whole 30 days, and `coverage_seconds` says how much of the window has data. Early on, availability is computed over a short period, so one outage moves it a lot while the budget barely moves. That is intended: the budget answers "how much more downtime can we afford this month", which does not depend on how long the monitor has been running.

## No notifications arrive

```bash
./scripts/send-test-alert.py
docker compose logs alertmanager | grep -i -E "notify|error"
```

- the shipped config has empty receivers on purpose; put yours in a git-ignored copy and set `ALERTMANAGER_CONFIG` (see [ALERTING.md](ALERTING.md#adding-your-channels))
- check the file with `amtool check-config` before restarting Alertmanager
- the first notification waits `group_wait` (30s); repeats come every hour for critical alerts and every 4 hours for warnings
- a silence, or an inhibition by a bigger alert for the same target, also stops notifications; Alertmanager's UI shows both

## The heartbeat service says the stack is down

Your dead man's switch stopped receiving `Watchdog`. Check that Prometheus and Alertmanager run, that `http://localhost:9090/alerts` shows `Watchdog` firing, and that Alertmanager can reach the heartbeat URL (`docker compose logs alertmanager`). See [the runbook](OPERATIONS_RUNBOOK.md#watchdog).

## A maintenance window does not show on the status page

- the silence comment must start with `[maintenance]`; `scripts/maintenance.py` adds it
- the silence must match on the `target` label; the target id is exactly as in `/targets`
- the gateway needs `ALERTMANAGER_URL`; `/status` shows `monitor.alertmanager`
- the gateway caches silences for 15 seconds

## Domain expiry is missing

The agent looks domains up over RDAP and logs every lookup:

```bash
docker compose logs ping-agent | grep DOMAIN
```

A failed lookup is retried with backoff from 5 minutes. Some country-code domains have no public RDAP service; their expiry stays unknown and `ping_domain_expiry_errors_total` keeps counting.

## Grafana panel has no data

Check Prometheus before Grafana, then run every dashboard query:

```bash
curl -fsS http://localhost:9090/api/v1/targets | python3 -m json.tool
python3 scripts/check_dashboard_queries.py http://localhost:9090
```

## An alert rule change has no effect

Prometheus reads rules at startup. Test the change, then restart:

```bash
docker run --rm -v "$PWD/config:/c:ro" --entrypoint promtool prom/prometheus:v2.54.1 test rules /c/alert.rules.test.yml
docker compose restart prometheus
curl -fsS http://localhost:9090/api/v1/rules | python3 -m json.tool | grep '"name"'
```

Alertmanager also reads its config at startup: `docker compose restart alertmanager`. Silences survive the restart.

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
