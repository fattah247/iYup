# API

The live OpenAPI schema, with every field described, is at `/docs` (Swagger UI) and `/openapi.json`.

Load local ports first:

```bash
set -a
test -f .env && . ./.env
set +a

API="http://localhost:${API_GATEWAY_PORT:-8080}"
```

## Where the data comes from

| Data | Source | Survives an agent restart |
|---|---|---|
| current state, reason, latency, connection phases, certificate | the agents' `/metrics`, one agent per region | yes: the next check rebuilds it |
| `availability`, `total_checks`, percentiles in `/status` | agent counters, summed across regions | no: they count from agent start |
| `/history`, `/outages`, `/slo`, `/uptime-summary-windowed` | Prometheus (the region-quorum recording rules) | yes |
| `maintenance` | Alertmanager silences marked `[maintenance]` | yes (Alertmanager's data volume) |

The gateway asks the agents which targets exist (`ping_target_info`), so there is one target list. Set `PING_TARGET_URLS` on the gateway only to report a fixed list in a fixed order; URLs no agent checks then show as `unknown` / `not_monitored`.

## States

Each target has a `state`, decided by majority across the regions with fresh data:

| state | meaning |
|---|---|
| `up` | at least half of the regions' latest checks succeeded; `slow` is `true` when most of them were slower than the target's `degraded_after` |
| `down` | more than half of the regions' latest checks failed after all retries; `reason` is the most common failure |
| `unknown` | no region has a check to trust yet: `reason` is `pending` (never checked) or `not_monitored`, or `stale` is `true` (every region's latest check is older than its staleness budget) |

`regions` lists each region's own verdict, and `regions_failing` / `regions_total` count them. A target that is `up` with `regions_failing > 0` is failing from a minority of regions: a regional problem, not an outage.

`reason` values: `ok`, `http_4xx`, `http_5xx`, `unexpected_status`, `body_mismatch`, `unexpected_answer`, `timeout`, `dns`, `connection_refused`, `tls`, `network`, `error`, `pending`, `not_monitored`. When a target is stale, `reason` keeps the last known outcome.

The overall `status`:

| status | rule |
|---|---|
| `outage` | every target is down |
| `degraded` | at least one target is down or slow |
| `unknown` | none are down, but at least one is unknown, so we cannot vouch for everything |
| `maintenance` | every target is in an active maintenance window |
| `operational` | every target is up |

Targets in an **active** maintenance window are set aside: they are counted in `summary.maintenance` and do not affect the overall status. Their own `state` stays truthful.

## Endpoints

### `GET /status`

```bash
curl -fsS "$API/status" | python3 -m json.tool
```

Trimmed from the demo stack, with one of the ten targets shown:

```json
{
  "status": "degraded",
  "summary": {"up": 7, "down": 2, "unknown": 0, "slow": 1, "maintenance": 1, "total": 10},
  "generated_at": "2026-10-13T06:19:32.861Z",
  "monitor": {
    "interval_seconds": 30.0,
    "agents": [{"region": "lab-a", "reachable": true}, {"region": "lab-b", "reachable": true}],
    "alertmanager": "reachable"
  },
  "maintenance": [
    {"active": true, "starts_at": "2026-10-13T06:03:14.920000Z", "ends_at": "2026-10-13T06:48:14.554000Z",
     "comment": "Replacing the disk", "targets": ["Broken page"]},
    {"active": false, "starts_at": "2026-10-13T19:00:00Z", "ends_at": "2026-10-13T19:30:00Z",
     "comment": "Certificate rotation", "targets": ["Example (HTTPS)"]}
  ],
  "targets": [
    {
      "url": "http://demo-targets:8080/regional",
      "name": "Regional outage",
      "kind": "http",
      "state": "up",
      "up": true,
      "slow": false,
      "reason": "ok",
      "stale": false,
      "http_status": 200,
      "last_checked_at": "2026-10-13T06:19:17.720353Z",
      "last_checked_seconds_ago": 15.1,
      "state_since": "2026-10-13T06:08:46.160324Z",
      "latency_ms": 2.4,
      "latency_percentiles_ms": {"p50": 5.2, "p95": 10.0, "p99": 39.0},
      "connection_ms": {"dns": 0.68, "tcp": 0.3, "tls": null},
      "availability": 50.0,
      "total_checks": 44,
      "success": 22,
      "failures": 22,
      "retries": 44,
      "ssl_cert_expiry_days": null,
      "interval_seconds": 30.0,
      "stale_after_seconds": 91.5,
      "slow_threshold_ms": null,
      "slo_objective": 99.9,
      "regions_total": 2,
      "regions_failing": 1,
      "regions": [
        {"region": "lab-a", "state": "up", "reason": "ok", "slow": false, "stale": false, "http_status": 200,
         "latency_ms": 2.4, "last_checked_at": "2026-10-13T06:19:16.203525Z", "last_checked_seconds_ago": 16.7},
        {"region": "lab-b", "state": "down", "reason": "http_5xx", "slow": false, "stale": false, "http_status": 503,
         "latency_ms": 2.3, "last_checked_at": "2026-10-13T06:19:17.720353Z", "last_checked_seconds_ago": 15.1}
      ],
      "maintenance": null
    }
  ]
}
```

Notes:

- The example target fails from `lab-b` only, so it stays `up` with `regions_failing: 1`. Its `availability` of 50% is raw checks from both regions since the agents started; the region-majority uptime over a window is in `/history` and `/slo`.
- `latency_ms` is `null` when no response arrived. A refused connection is not a fast response.
- `availability` is `null` with no checks. No data is not 0%.
- `state_since` is when the current up/down state began, as seen by the running agents.
- `ssl_cert_expiry_days` goes negative once a certificate has expired. The agent reads it even from a certificate it rejected.
- `interval_seconds`, `stale_after_seconds`, `slow_threshold_ms` and `slo_objective` come from the target's configuration.
- If some agents are unreachable, the rest still vote and `monitor.agents` shows which. Only when none answer is the response a `502`.
- A target's `maintenance` is its active window, or else its next planned one. Windows publish only the comment and the times, never who created them.

### `GET /targets` and `GET /targets/{id}`

```bash
curl -fsS "$API/targets" | python3 -m json.tool
curl -fsS "$API/targets/https%3A%2F%2Fexample.com" | python3 -m json.tool
```

`/targets` returns `source`: `agent` (discovered) or `config` (fixed list), and each target's id and name. The id is the URL for HTTP checks, `tcp://` or `tls://host:port` for TCP checks, and `dns://host/RECORD` for DNS checks. The detail endpoint returns one target object as above, or `404`.

### `GET /history?window=24h&buckets=48`

The data behind an uptime bar chart. The window is split into `buckets` equal parts. For each target and bucket you get the percent of time it was up by region majority, or `null` where there was no data. Bucket boundaries are aligned to the bucket size so they stay put between refreshes. The newest bucket is still filling.

```bash
curl -fsS "$API/history?window=24h&buckets=48" | python3 -m json.tool
```

```json
{
  "window": "24h",
  "bucket_seconds": 1800,
  "start": "2026-10-12T06:30:00Z",
  "end": "2026-10-13T06:30:00Z",
  "bucket_starts": ["2026-10-12T06:30:00Z", "..."],
  "targets": [
    {"url": "https://example.com", "name": "Example (HTTPS)", "availability": 100.0, "latency_p95_ms": 241.3,
     "buckets": [null, null, "...", 100.0]}
  ]
}
```

Limits: `window` up to `31d`, `buckets` 1–200, and each bucket must be at least 15s (the scrape interval).

### `GET /outages?window=7d&limit=50`

Down periods from the region-majority verdict, so a blip that a retry absorbed, or a failure seen from a minority of regions, is not an outage. Newest first. `by_target` totals always cover every outage, even when the list is cut to `limit`.

```bash
curl -fsS "$API/outages?window=24h" | python3 -m json.tool
```

```json
{
  "window": "24h",
  "resolution_seconds": 15,
  "start": "2026-10-12T06:20:00Z",
  "end": "2026-10-13T06:20:00Z",
  "total": 1,
  "truncated": false,
  "by_target": [{"url": "http://demo-targets:9", "name": "Nothing listening", "count": 1, "downtime_seconds": 1095}],
  "outages": [
    {"url": "http://demo-targets:9", "name": "Nothing listening", "started_at": "2026-10-13T06:02:00Z",
     "ended_at": null, "duration_seconds": 1095, "ongoing": true, "started_before_window": false}
  ]
}
```

Times are accurate to `resolution_seconds`. That is 15s up to about 41 hours; beyond that the step grows to keep Prometheus under its point limit. A single failed check is never lost, because each step asks whether *any* sample in it was down. A gap of up to two minutes with the target down on both sides, such as an agent restart, does not split an outage. A longer gap ends it at the last down observation, without inventing a recovery time.

### `GET /slo?window=30d`

The error budget per target over a rolling window, from 1h to 90d:

```bash
curl -fsS "$API/slo" | python3 -m json.tool
```

```json
{
  "window": "30d",
  "targets": [
    {"url": "https://example.com", "name": "Example (HTTPS)", "objective": 99.9, "availability": 99.95,
     "coverage_seconds": 2592000, "allowed_downtime_seconds": 2592, "downtime_seconds": 1296,
     "budget_remaining": 50.0, "burn_rate_1h": 0.0, "burn_rate_6h": 0.4}
  ]
}
```

- `allowed_downtime_seconds` is the budget for the whole window: (1 − objective) × window.
- `downtime_seconds` is what the region-majority series actually saw.
- `budget_remaining` is the percent of the budget left, negative once overspent.
- `coverage_seconds` says how much of the window has data. On a young install the budget is still the whole window's, and the downtime counts only what was observed.
- A `burn_rate_*` of 1 means spending the budget exactly as fast as it accrues. The fast-burn alert fires at 14.4 (see [ALERTING.md](ALERTING.md#error-budgets)).

### `GET /uptime-summary` and `GET /uptime-summary-windowed?window=5m`

Check counts and a formatted `availability` (`"99.50%"`, or `null` with no checks). The plain version sums all agents since they started. The windowed one uses Prometheus `increase()`, which extrapolates, so short windows can show fractional counts.

### `GET /healthz`, `GET /metrics`

`/healthz` reports that the gateway process is alive. It does not check dependencies, on purpose: a status API that goes "unhealthy" whenever an agent is down would hide the useful `502` explaining why. `/metrics` labels requests by route template (`/targets/{target_url:path}`, `/assets`, `unmatched`), never by raw URL, so scanners cannot inflate series counts.

## Errors

Errors are JSON: `{"detail": "..."}`.

| Code | When |
|---|---|
| `400` | invalid `window` (format, zero, too long), buckets finer than 15s, an outage window under 1m, or an SLO window under 1h |
| `404` | the target is not reported |
| `422` | a query parameter out of range (for example `buckets=0`) |
| `501` | a history endpoint was called but `PROMETHEUS_URL` is not set |
| `502` | no ping-agent answered, or Prometheus did not answer or answered with something unusable; `detail` says which |

An unreachable Alertmanager is not an error: `/status` still answers, with `monitor.alertmanager` set to `unreachable` and no maintenance windows.

Upstream calls time out after 2s to connect and 5s (agents, Alertmanager) or 15s (Prometheus) to read. Only connection failures and `502/503/504` are retried, twice, so a hung dependency costs seconds rather than a minute.
