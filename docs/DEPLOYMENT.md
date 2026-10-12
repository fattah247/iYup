# Deployment

## Docker Compose

Run:

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

Stop:

```bash
docker compose down
```

Host ports come from `.env` when they are overridden there.

## Helm render check

Run:

```bash
helm lint ./charts/iyup
helm template iyup ./charts/iyup
```

That is the Kubernetes claim this repo makes today: the chart renders cleanly, and the rendered Alertmanager config and alert rules pass `amtool` and `promtool`.

Alert receivers come from values. Every alert goes to the in-cluster `alert-logger`; email and Slack are added only when configured. Keep real credentials in a git-ignored values file (`charts/iyup/values.local.yaml` is ignored):

```bash
helm upgrade --install iyup ./charts/iyup -f charts/iyup/values.local.yaml
```

The chart reads `alert.smtp.{smarthost,from,user,password,to}` and `alert.slackWebhook`.

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

Notes for moving an existing stack to this version:

- **Latency percentiles blip once.** The agent's latency histogram now has buckets suited to web responses. Until a query's window has fully passed the upgrade, PromQL mixes old and new buckets and `histogram_quantile` can read high. That is up to 5 minutes on the dashboard's p95 panel, and up to the selected window on the status page.
- **The gateway no longer needs `PING_TARGET_URLS`.** It discovers targets from the agent. If you keep setting it, the gateway reports exactly that list, and URLs the agent does not check show as `unknown` / `not_monitored`.
- **Grafana keeps hand-saved dashboards.** The provisioned dashboard is now `iYup uptime overview` (uid `iyup-overview`). A copy of the old `iYup Dashboard` that was saved in the UI is your data and stays until you delete it.
- **Alert names are unchanged** (`TargetDown` keeps its name), so silences and routes keyed on `alertname` still match. The rule group names changed to `iyup-targets` and `iyup-pipeline`.
- **Helm: Alertmanager config is now a Secret** named `<release>-alertmanager-config`. The old `-alertmanager-smtp` Secret and `-alertmanager-config` ConfigMap are no longer rendered. The SMTP defaults are now empty, so email stays off until `alert.smtp.smarthost` is set.
- **Helm: config changes roll pods.** Prometheus, Alertmanager and Grafana carry a checksum of their config, so changing rules, receivers or the dashboard restarts them on `helm upgrade`.
