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
docker compose logs -f ping-agent # check results: [UP] [DOWN] [RETRY] [OUTAGE] [RECOVERED] [SLOW] [OVERRUN] [DOMAIN]
docker compose down               # stop (data volumes are kept)
./scripts/verify-local.sh         # end-to-end check of the running stack
```

The quickest overview is the status page at `$API/` or `curl -fsS "$API/status" | python3 -m json.tool`.

## Alerts

Each alert's `runbook` annotation links to its section below. Planned work: see [Maintenance](#maintenance) first, so it does not page anyone.

### TargetDown

**Means:** a majority of regions have seen every check of the target fail for at least a minute, after retries. The `description` gives the latest reason.

**Do:**

1. Read the reason and the per-region results: `curl -fsS "$API/targets/<url-encoded target id>" | python3 -m json.tool`.

   | reason | usually means |
   |---|---|
   | `connection_refused` | nothing is listening: the service is stopped, or the wrong port |
   | `timeout` | the host is up but not answering in time: overloaded, or packets dropped |
   | `dns` | the name does not resolve: DNS record or resolver problem |
   | `tls` | certificate expired, untrusted, or wrong hostname; check `ssl_cert_expiry_days` |
   | `http_5xx` | the service answers but is failing; `http_status` has the code |
   | `http_4xx`, `unexpected_status` | the URL needs auth, moved, or no longer exists; often the check config, not an outage |
   | `body_mismatch` | the page answered but without the expected text: an error page, a maintenance page, a broken deploy |
   | `unexpected_answer` | a DNS record changed: check for a mistaken or malicious change |

2. Confirm from outside: `curl -sv <url>`. If it works from your machine and every region agrees it is down, suspect the target's network edge (firewall, CDN, upstream provider).
3. Watch it recover on the status page or with `curl -fsS "$API/outages?window=1h"`.

### ErrorBudgetBurn

`ErrorBudgetBurnFast` (critical) and `ErrorBudgetBurnSlow` (warning).

**Means:** the target is failing often enough to use up its 30-day error budget early, without staying down long enough for `TargetDown`. The fast alert means the budget would be gone within days. The slow alert means it will not last the month. Both ignore an outage `TargetDown` already paged for.

**Do:** look at the "Up / down over time" and "Retries per hour" panels and at `curl -fsS "$API/slo" | python3 -m json.tool`. Short, repeated failures usually mean an overloaded backend, a failing instance behind a load balancer, or a dependency timing out. Fix the cause, or if the objective is unrealistic for this target, lower its `slo` deliberately in the targets file.

### TargetRegionalFailure

**Means:** checks from some regions have failed for 5 minutes while most regions still succeed. The description names the failing regions. No page, because users elsewhere are fine.

**Do:** compare regions in `/targets/<id>` (`regions`). One region failing is usually DNS (a resolver serving a stale record), a CDN edge, a regional cloud problem, or the agent's own network. If it is the agent's network, every target fails from that region at once. If several targets fail from the same region, check that agent's host before the targets.

### TargetSlow

**Means:** the target answers, but most regions have found it slower than its `degraded_after` threshold for 5 minutes.

**Do:** the "Where the latest check's time went" panel shows whether the time is DNS, TCP, TLS or server. Server time points at the application or database. DNS or TLS time points at the provider or certificate chain. If the threshold is simply too tight, raise `degraded_after` for that target.

### TargetChecksStale

**Means:** an agent is running and being scraped, but has not finished a check of this target within its staleness budget. That region's view of the target is `unknown`.

**Do:**

1. `docker compose logs --tail 100 ping-agent`. Look for `[OVERRUN]` lines (checks longer than their interval, including any wait for a free slot).
2. If many targets time out at once and `PING_CONCURRENCY` is low, checks queue up. Raise the concurrency or lower the timeouts.
3. If the logs have stopped entirely, restart the agent: `docker compose restart ping-agent`. `TargetDown` will not flap because of the restart.

### CertificateExpiringSoon

**Means:** the target's TLS certificate expires within 14 days. A negative value means it has already expired; `TargetDown` with reason `tls` will usually be firing too.

**Do:** renew the certificate or fix auto-renewal (for example certbot or cert-manager), then confirm `ssl_cert_expiry_days` after the next check.

### DomainExpiringSoon

**Means:** a domain listed under `domains:` in the targets file expires within 30 days, according to its registry's RDAP record.

**Do:** renew it at the registrar or check that auto-renew and the payment method work. A lapsed domain takes the website, email and certificate renewals down with it.

### AllPingAgentsDown

**Means:** Prometheus cannot scrape any agent. Nothing is being checked and target alerts cannot fire, so this is critical even if every target is fine.

**Do:**

```bash
docker compose ps ping-agent
docker compose logs --tail 50 ping-agent
curl -fsS "$AGENT/healthz"
```

A `config:` line in the logs means the agent refused to start. The message lists every problem in the targets file; `go run . validate <file>` shows the same without starting anything.

### PingAgentDown

**Means:** one of several agents cannot be scraped. The others keep checking, but the quorum rests on fewer votes, and with two regions a single agent decides alone.

**Do:** the same checks as above, on the agent named in the alert.

### ApiGatewayDown

**Means:** the status page and JSON API are down. Checks, history and alerting continue without it.

**Do:**

```bash
docker compose ps api-gateway
docker compose logs --tail 50 api-gateway
docker compose up -d api-gateway
```

### Watchdog

Always firing, by design: it is the heartbeat. If your dead man's switch reports that the heartbeat stopped, Prometheus, Alertmanager or the host is down. Nothing inside the stack can tell you that. See [ALERTING.md](ALERTING.md#heartbeat).

## Maintenance

Before planned work, so nobody gets paged and the status page says what is happening:

```bash
./scripts/maintenance.py start <target id> --duration 1h --comment "What you are doing"
./scripts/maintenance.py list
./scripts/maintenance.py end <target id>     # when done early
```

Target ids are the URLs (or `tcp://`, `dns://` ids) shown by `curl -fsS "$API/targets"`. Use `--regex` to cover several targets, for example `'https://.*\.example\.com'`.

## When the API answers 502

`/status` returns `502` with `detail` naming the upstream:

- `ping-agent is unreachable`: see [PingAgentDown](#pingagentdown).
- `Prometheus is unreachable` (only on `/history`, `/outages`, `/uptime-summary-windowed`): current status still works, and the status page shows that history is unavailable. Check `curl -fsS "$PROM/-/ready"`.

## Checking the monitoring config

```bash
docker run --rm -v "$PWD/config:/c:ro" --entrypoint promtool prom/prometheus:v2.54.1 test rules /c/alert.rules.test.yml
docker run --rm -v "$PWD/config:/c:ro" --entrypoint amtool prom/alertmanager:v0.27.0 check-config /c/alertmanager.yml
(cd services/ping-agent && go run . validate ../../config/targets/targets.yml)
python3 scripts/check_dashboard_queries.py "$PROM"
./scripts/send-test-alert.py
```

Prometheus reads rules at startup. After editing `config/alert.rules.yml`, run `docker compose restart prometheus`.

## Helm rendering

```bash
helm lint ./charts/iyup
helm template iyup ./charts/iyup
```
