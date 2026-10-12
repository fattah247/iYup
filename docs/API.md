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
| current state, reason, latency, connection phases, certificate | ping-agent `/metrics` | yes: the next check rebuilds it |
| `availability`, `total_checks`, percentiles in `/status` | ping-agent counters | no: they count from agent start |
| `/history`, `/outages`, `/uptime-summary-windowed` | Prometheus | yes |

The gateway asks the agent which targets exist (`ping_target_info`), so there is one target list. Set `PING_TARGET_URLS` on the gateway only if you want it to report a fixed list in a fixed order. URLs the agent does not check then show up as `unknown` / `not_monitored`.

## States

Each target has a `state`:

| state | meaning |
|---|---|
| `up` | the latest check succeeded |
| `down` | the latest check failed after all retries; `reason` says why |
| `unknown` | there is no check to trust yet: `reason` is `pending` (never checked) or `not_monitored`, or `stale` is `true` (the latest check is older than `monitor.stale_after_seconds`) |

`reason` values: `ok`, `http_4xx`, `http_5xx`, `timeout`, `dns`, `connection_refused`, `tls`, `network`, `error`, `pending`, `not_monitored`. When the target is stale, `reason` keeps the last known outcome.

The overall `status` is decided from the target states:

| status | rule |
|---|---|
| `outage` | every target is down |
| `degraded` | at least one target is down |
| `unknown` | none are down, but at least one is unknown, so we cannot vouch for everything |
| `operational` | every target is up |

## Endpoints

### `GET /status`

```bash
curl -fsS "$API/status" | python3 -m json.tool
```

```json
{
  "status": "degraded",
  "summary": {"up": 1, "down": 1, "unknown": 0, "total": 2},
  "generated_at": "2026-10-12T17:19:02.512Z",
  "monitor": {"interval_seconds": 30.0, "stale_after_seconds": 91.5, "region": null},
  "targets": [
    {
      "url": "http://demo-targets:9",
      "name": "demo-targets:9",
      "state": "down",
      "up": false,
      "reason": "connection_refused",
      "stale": false,
      "http_status": null,
      "last_checked_at": "2026-10-12T17:18:54.444Z",
      "last_checked_seconds_ago": 8.1,
      "state_since": "2026-10-12T17:18:24.444Z",
      "latency_ms": null,
      "latency_percentiles_ms": {"p50": null, "p95": null, "p99": null},
      "connection_ms": {"dns": 0.3, "tcp": null, "tls": null},
      "availability": 0.0,
      "total_checks": 2,
      "success": 0,
      "failures": 2,
      "retries": 4,
      "ssl_cert_expiry_days": null
    }
  ]
}
```

Notes:

- `latency_ms` is `null` when no HTTP response arrived. A refused connection is not a fast response.
- `availability` is `null` when there have been no checks. No data is not 0%.
- `state_since` is when the current up/down state began, as seen by the running agent.
- `ssl_cert_expiry_days` goes negative once a certificate has expired. The agent reads it even from a certificate it rejected.

### `GET /targets` and `GET /targets/{url}`

```bash
curl -fsS "$API/targets" | python3 -m json.tool
curl -fsS "$API/targets/https%3A%2F%2Fgithub.com" | python3 -m json.tool
```

`/targets` also returns `source`: `agent` (discovered) or `config` (fixed list). The detail endpoint returns one target object as above, or `404` for a URL that is not reported.

### `GET /history?window=24h&buckets=48`

The data behind an uptime bar chart. The window is split into `buckets` equal parts. For each target and bucket you get the percent of samples in which it was up, or `null` where there was no data. Bucket boundaries are aligned to the bucket size so they stay put between refreshes. The newest bucket is still filling.

```bash
curl -fsS "$API/history?window=24h&buckets=48" | python3 -m json.tool
```

```json
{
  "window": "24h",
  "bucket_seconds": 1800,
  "start": "2026-10-11T18:00:00Z",
  "end": "2026-10-12T18:00:00Z",
  "bucket_starts": ["2026-10-11T18:00:00Z", "..."],
  "targets": [
    {"url": "https://example.com", "name": "example.com", "availability": 100.0, "latency_p95_ms": 241.3,
     "buckets": [null, null, "...", 100.0]}
  ]
}
```

Limits: `window` up to `31d`, `buckets` 1–200, and each bucket must be at least 15s (the scrape interval).

### `GET /outages?window=7d&limit=50`

Down periods found from the agent's post-retry verdicts, so a blip that a retry absorbed is not an outage. Newest first. `by_target` totals always cover every outage, even when the list is cut to `limit`.

```bash
curl -fsS "$API/outages?window=24h" | python3 -m json.tool
```

```json
{
  "window": "24h",
  "resolution_seconds": 15,
  "start": "2026-10-11T17:20:00Z",
  "end": "2026-10-12T17:20:00Z",
  "total": 1,
  "truncated": false,
  "by_target": [{"url": "http://demo-targets:9", "name": "demo-targets:9", "count": 1, "downtime_seconds": 95}],
  "outages": [
    {"url": "http://demo-targets:9", "name": "demo-targets:9", "started_at": "2026-10-12T17:18:15Z",
     "ended_at": null, "duration_seconds": 95, "ongoing": true, "started_before_window": false}
  ]
}
```

Times are accurate to `resolution_seconds`. That is 15s up to about 41 hours; beyond that the step grows to keep Prometheus under its point limit. A single failed check is never lost, because each step asks whether *any* sample in it was down. A gap of up to two minutes with the target down on both sides, such as an agent restart, does not split an outage. A longer gap ends it at the last down observation, without inventing a recovery time.

### `GET /uptime-summary` and `GET /uptime-summary-windowed?window=5m`

Check counts and a formatted `availability` (`"99.50%"`, or `null` with no checks). The plain version counts from agent start. The windowed one uses Prometheus `increase()`, which extrapolates, so short windows can show fractional counts.

### `GET /healthz`, `GET /metrics`

`/healthz` reports that the gateway process is alive. It does not check dependencies, on purpose: a status API that goes "unhealthy" whenever the agent is down would hide the useful `502` explaining why. `/metrics` labels requests by route template (`/targets/{target_url:path}`, `/assets`, `unmatched`), never by raw URL, so scanners cannot inflate series counts.

## Errors

Errors are JSON: `{"detail": "..."}`.

| Code | When |
|---|---|
| `400` | invalid `window` (format, zero, too long), buckets finer than 15s, or an outage window under 1m |
| `404` | the target URL is not reported |
| `422` | a query parameter out of range (for example `buckets=0`) |
| `501` | a history endpoint was called but `PROMETHEUS_URL` is not set |
| `502` | the ping-agent or Prometheus did not answer, or answered with something unusable; `detail` says which |

Upstream calls time out after 2s to connect and 5s (agent) or 15s (Prometheus) to read. Only connection failures and `502/503/504` are retried, twice, so a hung dependency costs seconds rather than a minute.
