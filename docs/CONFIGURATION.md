# Configuration

## Targets file

The ping-agent reads its checks from `config/targets/targets.yml`, mounted at `/etc/iyup/targets.yml`. Keep it in version control; it is your monitoring as code.

Apply changes with:

```bash
docker compose up -d ping-agent
```

Check a file without starting anything (CI does this for every file in `config/targets/`):

```bash
cd services/ping-agent && go run . validate ../../config/targets/targets.yml
```

A bad file stops the agent at startup with one message listing every problem: a schemeless URL, an unknown field (typos such as `intervall` are caught), a missing environment variable, a duplicate target.

### Shape

```yaml
defaults:          # applied to every target; each target can override any of them
  interval: 30s
  timeout: 10s
  retries: 2
  slo: 99.9

targets:
  - name: Website
    url: https://example.com

domains:           # registrations to watch for expiry
  - example.com
```

### Target fields

| Field | Applies to | Default | Meaning |
|---|---|---|---|
| `name` | all | host and path | shown on the status page, dashboard and alerts |
| `type` | all | `http` | `http`, `tcp` or `dns` |
| `url` | http | | absolute `http://` or `https://` URL |
| `method` | http | `GET` | `GET` or `HEAD` |
| `headers` | http | none | sent with every check; `${VAR}` reads the agent's environment |
| `expect.status` | http | `200`–`399` | list of codes, classes or ranges: `["200", "2xx", "200-299"]` |
| `expect.body_contains` | http | none | text the response body must contain (first 64 KB); needs `GET` |
| `follow_redirects` | http | `true` | `false` evaluates the redirect response itself |
| `address` | tcp | | `host:port` |
| `tls` | tcp | `false` | also complete a TLS handshake and record certificate expiry |
| `host` | dns | | name to resolve |
| `record` | dns | `A` | `A`, `AAAA`, `CNAME`, `MX`, `TXT`, `NS` |
| `server` | dns | system resolver | resolver to ask, `host:port` such as `1.1.1.1:53` |
| `expect.answers` | dns | none | values that must all be among the answers |
| `interval` | all | `30s` | time between checks of this target (at least `1s`) |
| `timeout` | all | `10s` | per attempt (at least `100ms`) |
| `retries` | all | `2` | extra attempts before a check counts as failed (`0`–`10`) |
| `degraded_after` | all | off | a successful check slower than this counts as slow |
| `slo` | all | `99.9` | availability objective, percent, over 30 days |

### What counts as down, slow and retried

- **Down:** no response, a status outside `expect.status`, a body without `expect.body_contains`, or a DNS answer missing an expected value. Each failure carries a reason: `timeout`, `dns`, `connection_refused`, `tls`, `network`, `http_4xx`, `http_5xx`, `unexpected_status`, `body_mismatch`, `unexpected_answer`, `error`.
- **Slow:** up, but slower than `degraded_after`.
- **Retried:** failures a second attempt might fix (timeouts, refused or reset connections, DNS errors, 5xx, body mismatches), with backoff of 0.5s, 1s, 2s… A 4xx, a rejected certificate or a wrong DNS answer is not retried.

### Target ids

Each target has an id used in metrics, alerts and the API:

| Type | Id |
|---|---|
| http | the `url` as written, with `${VAR}` placeholders left in so secrets never reach a metric label |
| tcp | `tcp://host:port`, or `tls://host:port` with `tls: true` |
| dns | `dns://host/RECORD`, plus `@server` when a resolver is set |

### Secrets

Header values and URLs can reference environment variables with `${NAME}`. Only the braced form is expanded. A missing variable is a startup error, not an empty token. Pass the variable to the agent, for example in `docker-compose.override.yml` or a Kubernetes Secret, and never write the value into the targets file.

### Examples

```yaml
targets:
  - name: API health
    url: https://api.example.com/health
    headers:
      Authorization: "Bearer ${API_TOKEN}"
    expect:
      status: ["200"]
      body_contains: '"status":"ok"'
    degraded_after: 800ms
    interval: 15s
    slo: 99.95

  - name: Old URL still redirects
    url: https://example.com/old
    follow_redirects: false
    expect:
      status: ["301"]

  - name: Postgres
    type: tcp
    address: db.example.com:5432

  - name: Mail certificate
    type: tcp
    address: mail.example.com:465
    tls: true

  - name: Mail routing
    type: dns
    host: example.com
    record: MX
    expect:
      answers: [mx1.example.com]
```

## Quick override

Setting `PING_TARGET_URLS` (comma-separated URLs) makes the agent ignore the targets file and check those URLs with the defaults below. It is handy for a quick experiment; the reliability replay scripts use it.

## Environment variables

### ping-agent

| Variable | Default | Purpose |
|---|---|---|
| `PING_TARGETS_FILE` | `/etc/iyup/targets.yml` in Compose | targets file |
| `PING_TARGET_URLS` | empty | quick override, see above |
| `PING_REGION` | empty | this agent's region label; set it when several agents report to one Prometheus |
| `PING_CONCURRENCY` | `5` | checks running at the same time |
| `PING_INTERVAL_SECONDS`, `PING_TIMEOUT_SECONDS`, `PING_RETRY_COUNT`, `PING_HTTP_METHOD`, `PING_SLO` | `30`, `10`, `2`, `GET`, `99.9` | defaults beneath the file's `defaults` |
| `PING_RANGE_REQUEST` | `true` | ask for one byte (`Range: bytes=0-0`) when nothing needs the body |
| `PING_BODY_MAX_BYTES` | `65536` | how much of a body is read |
| `PING_RDAP_URL` | `https://rdap.org/domain/` | RDAP lookup for domain expiry |

### api-gateway

| Variable | Default | Purpose |
|---|---|---|
| `PING_AGENT_METRICS_URL` | `http://ping-agent:8080/metrics` | one agent, or several comma-separated (one per region) |
| `PROMETHEUS_URL` | empty | needed for history, outages, SLOs |
| `ALERTMANAGER_URL` | empty | needed to show maintenance windows |
| `PROMETHEUS_QUERY_CACHE_SECONDS` | `15` | cache for Prometheus queries |
| `PING_TARGET_URLS` | empty | report exactly these targets, in this order, instead of discovering them |
| `CORS_ORIGINS` | `*` | allowed origins for the read-only API |

### Docker Compose

| Variable | Default | Purpose |
|---|---|---|
| `API_GATEWAY_PORT`, `PING_AGENT_PORT`, `PROMETHEUS_PORT`, `GRAFANA_PORT`, `ALERTMANAGER_PORT` | `8080`, `18080`, `9090`, `3000`, `9093` | host ports |
| `PROMETHEUS_RETENTION` | `35d` | long enough to judge a 30-day error budget |
| `ALERTMANAGER_CONFIG` | `./config/alertmanager.yml` | point at a git-ignored copy that holds your receivers |
| `GRAFANA_PASSWORD` | `admin` | Grafana admin password |

## More than one region

Run one agent per location, each with its own `PING_REGION` and the same targets file. Have Prometheus scrape them all by listing them in `config/agents.yml`, and give the gateway every agent's `/metrics` URL. A target is down only when more than half of the regions with fresh data agree; a failure seen from a minority raises `TargetRegionalFailure` instead of paging.

The demo overlay runs two agents (`lab-a`, `lab-b`) on one machine to show this. They are labels, not real locations.
