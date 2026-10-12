# Operations Runbook

Load local ports first:

```bash
set -a
test -f .env && . ./.env
set +a

API="http://localhost:${API_GATEWAY_PORT:-8080}"
AGENT="http://localhost:${PING_AGENT_PORT:-18080}"
PROM="http://localhost:${PROMETHEUS_PORT:-9090}"
```

## Everyday commands

```bash
docker compose up -d --build      # start or update
docker compose ps                 # containers and health
docker compose logs -f ping-agent # check results: [UP] [DOWN] [RETRY] [OUTAGE] [RECOVERED] [SLOW]
docker compose down               # stop (data volumes are kept)
./scripts/verify-local.sh         # end-to-end check of the running stack
```

The quickest overview is the status page at `$API/` or `curl -fsS "$API/status" | python3 -m json.tool`.

## Alerts

Each alert's `runbook` annotation links to its section below.

### TargetDown

**Means:** every check of the target has failed for at least a minute, after retries. The `description` gives the latest reason.

**Do:**

1. Read the reason: `curl -fsS "$API/targets/<url-encoded target>" | python3 -m json.tool`.

   | reason | usually means |
   |---|---|
   | `connection_refused` | nothing is listening: the service is stopped, or the wrong port |
   | `timeout` | the host is up but not answering in `PING_TIMEOUT_SECONDS`: overloaded, or packets dropped |
   | `dns` | the name does not resolve: DNS record or resolver problem |
   | `tls` | certificate expired, untrusted, or wrong hostname; check `ssl_cert_expiry_days` |
   | `http_5xx` | the service answers but is failing; `http_status` has the code |
   | `http_4xx` | the URL needs auth, moved without a redirect, or no longer exists; usually the check config, not an outage |

2. Confirm from outside the agent: `curl -sv <target>`. If it works from your machine but not from the agent, suspect the agent's network (one vantage point, see Limitations in the README).
3. Watch the outage close on the status page or with `curl -fsS "$API/outages?window=1h"`.

### TargetUnreliable

**Means:** the target keeps flipping between up and down: it was up less than 90% of the last 15 minutes and changed state more than twice. A single clean outage does not trigger it. Alertmanager suppresses it while `TargetDown` fires for the same target.

**Do:** look at the "Up / down over time" and "Retries per hour" panels in Grafana. A target that keeps needing retries is degrading before it goes down. Check the target's error logs and saturation around the failure times in `/outages`.

### TargetChecksStale

**Means:** the agent is running and being scraped, but has not finished a check of this target within `ping_stale_after_seconds`. The status API reports the target as `unknown`.

**Do:**

1. `docker compose logs --tail 100 ping-agent`. Look for `[SLOW]` lines (cycles longer than the interval).
2. If many targets time out at once and `PING_CONCURRENCY` is low, cycles queue up. Raise the concurrency or lower `PING_TIMEOUT_SECONDS`.
3. If the logs have stopped entirely, restart the agent: `docker compose restart ping-agent`. `TargetDown` will not flap because of the restart.

### CertificateExpiringSoon

**Means:** the target's TLS certificate expires within 14 days. A negative value means it has already expired; `TargetDown` with reason `tls` will usually be firing too.

**Do:** renew the certificate or fix auto-renewal (for example certbot or cert-manager). Confirm with `curl -fsS "$API/targets/<encoded>" | python3 -c "import json,sys; print(json.load(sys.stdin)['ssl_cert_expiry_days'])"` after the next check.

### PingAgentDown

**Means:** Prometheus cannot scrape the agent. Nothing is being checked, and target alerts cannot fire, so this is critical even if every target is fine.

**Do:**

```bash
docker compose ps ping-agent
docker compose logs --tail 50 ping-agent
curl -fsS "$AGENT/healthz"
```

A `config:` line in the logs means the agent refused to start. Usually an entry in `PING_TARGET_URLS` is not an absolute `http(s)://` URL, and the log names it.

### ApiGatewayDown

**Means:** the status page and JSON API are down. Checks, history and alerting continue without it.

**Do:**

```bash
docker compose ps api-gateway
docker compose logs --tail 50 api-gateway
docker compose up -d api-gateway
```

## When the API answers 502

`/status` returns `502` with `detail` naming the upstream:

- `ping-agent is unreachable`: see [PingAgentDown](#pingagentdown).
- `Prometheus is unreachable` (only on `/history`, `/outages`, `/uptime-summary-windowed`): current status still works, and the status page shows that history is unavailable. Check `curl -fsS "$PROM/-/ready"`.

## Checking the monitoring config

```bash
docker run --rm -v "$PWD/config:/c:ro" --entrypoint promtool prom/prometheus:v2.54.1 test rules /c/alert.rules.test.yml
docker run --rm -v "$PWD/config:/c:ro" --entrypoint amtool prom/alertmanager:v0.27.0 check-config /c/alertmanager.yml
python3 scripts/check_dashboard_queries.py "$PROM"
```

Prometheus reads rules at startup. After editing `config/alert.rules.yml`, run `docker compose restart prometheus`.

## Helm rendering

```bash
helm lint ./charts/iyup
helm template iyup ./charts/iyup
```
