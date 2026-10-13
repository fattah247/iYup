# Deployment

## Docker Compose

Run:

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

Stop (keeps history, dashboards and silences in their volumes):

```bash
docker compose down
```

Host ports come from `.env` when they are overridden there. Targets are in `config/targets/targets.yml`, alert receivers in a git-ignored copy of `config/alertmanager.yml`; see [CONFIGURATION.md](CONFIGURATION.md) and [ALERTING.md](ALERTING.md).

### More regions

The demo overlay runs a second agent on the same machine. For real geographic diversity, run the agent somewhere else (a small VM, or any container host in another region) with the same targets file and its own region:

```bash
docker build -t ping-agent:latest services/ping-agent
docker run -d --name iyup-agent --restart unless-stopped -p 18080:8080 \
  -e PING_TARGETS_FILE=/etc/iyup/targets.yml -e PING_REGION=eu-west \
  -v "$PWD/config/targets:/etc/iyup:ro" ping-agent:latest
```

Then add it to `config/agents.yml` (Prometheus picks up changes to that file without a restart) and to the gateway's `PING_AGENT_METRICS_URL`. Give every agent a `PING_REGION`, including the one in this Compose file. The agent's `/metrics` has no authentication, so expose it only over a private network or a VPN, or put a reverse proxy with authentication in front of it.

## Helm render check

Run:

```bash
helm lint ./charts/iyup
helm template iyup ./charts/iyup
```

That is the Kubernetes claim this repo makes today: the chart renders cleanly, and the rendered Alertmanager config and alert rules pass `amtool` and `promtool`.

### Values

Keep real values in a git-ignored file (`charts/iyup/values.local.yaml` is ignored):

```bash
helm upgrade --install iyup ./charts/iyup -f charts/iyup/values.local.yaml
```

| Value | Purpose |
|---|---|
| `checks` | checks as code, in the targets-file format; replaces `pingTargets` when set |
| `pingTargets` | plain URL list, used when `checks` is empty |
| `pingAgent.region` | this cluster's region label, when several agents report to one Prometheus |
| `pingAgent.{intervalSeconds,timeoutSeconds,retryCount,concurrency,httpMethod,rangeRequest}` | defaults beneath the checks' own settings |
| `alert.smtp.{smarthost,from,user,password,to}` | email for the `page` and `notify` receivers |
| `alert.slackWebhook`, `alert.discordWebhook`, `alert.webhookUrl` | chat and generic webhook receivers |
| `alert.telegram.{botToken,chatId}` | Telegram, for free phone push |
| `alert.heartbeatUrl` | dead man's switch for the `Watchdog` heartbeat, e.g. a healthchecks.io ping URL |
| `prometheus.retention` | `35d` by default, long enough for 30-day error budgets |
| `{prometheus,grafana,alertmanager}.pvcSize` | volume sizes; Alertmanager's holds silences (maintenance windows) |

Every alert also goes to the in-cluster `alert-logger`, so `kubectl logs` on it shows what was sent. A receiver is added only when its values are set.

## Optional local cluster path

For a local Minikube run:

```bash
minikube start
eval "$(minikube -p minikube docker-env)"
docker build -t ping-agent:latest services/ping-agent
docker build -t api-gateway:latest services/api-gateway
helm upgrade --install iyup ./charts/iyup
```

The chart uses service port `80` for `api-gateway` and target port `8080`.

## Upgrading an existing install

### To checks as code, regions and error budgets

- **An old `.env` overrides the targets file.** Earlier `.env.example` files set `PING_TARGET_URLS`. A non-empty `PING_TARGET_URLS` takes precedence over `config/targets/targets.yml`, so comment it out to use the file. The agent logs which source it used at startup.
- **`TargetUnreliable` is gone.** `ErrorBudgetBurnFast` and `ErrorBudgetBurnSlow` replace it. Silences and routes keyed on `TargetUnreliable` no longer match anything.
- **Alertmanager receivers are now `page`, `notify` and `heartbeat`.** Move receivers configured under the old `default` into a git-ignored copy of the new `config/alertmanager.yml` (see [ALERTING.md](ALERTING.md#adding-your-channels)) and point `ALERTMANAGER_CONFIG` at it.
- **Alerts and history use the region quorum.** New recording rules (group `iyup-recording`) compute `iyup:target_up:quorum`; with a single agent it equals `ping_up`. History bars and outages from before the upgrade fall back to raw `ping_up`, so they stay. Whole-window uptime and `/slo` count from the upgrade, and `coverage_seconds` says so.
- **Per-target budgets.** `ping_interval_seconds` and `ping_stale_after_seconds` now carry a `target` label, because each target can have its own interval. Queries that expected a single series should aggregate with `max()` or `min()`.
- **Status API additions.** `summary` gains `slow` and `maintenance`; the overall `status` can be `maintenance`; each target gains `kind`, `slow`, `regions`, `regions_failing`, `regions_total`, `maintenance` and its configured budgets. `monitor` now lists `agents` and the Alertmanager connection, replacing `monitor.region` and `monitor.stale_after_seconds` (each target has its own `stale_after_seconds`).
- **Prometheus retention defaults to 35 days** (was 14), and Alertmanager has a data volume so silences survive restarts. Both use more disk.
- **Helm.** `checks` replaces `pingTargets` when set; new values for Discord, Telegram, a generic webhook and the heartbeat; Alertmanager gets a PVC; the agent rolls when `checks` change.

### From the first release

- **Latency percentiles blip once.** The agent's latency histogram has buckets suited to web responses. Until a query's window has fully passed the upgrade, PromQL mixes old and new buckets and `histogram_quantile` can read high. That is up to 5 minutes on the dashboard's p95 panel, and up to the selected window on the status page.
- **The gateway no longer needs `PING_TARGET_URLS`.** It discovers targets from the agents. If you keep setting it, the gateway reports exactly that list, and URLs no agent checks show as `unknown` / `not_monitored`.
- **Grafana keeps hand-saved dashboards.** The provisioned dashboard is `iYup uptime overview` (uid `iyup-overview`). A copy of the old `iYup Dashboard` that was saved in the UI is your data and stays until you delete it.
- **`TargetDown` kept its name**, so silences and routes keyed on it still match.
- **Helm: Alertmanager config is a Secret** named `<release>-alertmanager-config`. The old `-alertmanager-smtp` Secret and `-alertmanager-config` ConfigMap are no longer rendered. The SMTP defaults are empty, so email stays off until `alert.smtp.smarthost` is set.
- **Helm: config changes roll pods.** Prometheus, Alertmanager, Grafana and the agent carry a checksum of their config, so changing checks, rules, receivers or the dashboard restarts them on `helm upgrade`.
