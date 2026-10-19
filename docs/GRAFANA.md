# Grafana

## The dashboard

`iYup uptime overview` (uid `iyup-overview`) is Grafana's home dashboard in Docker Compose and Helm. Every panel follows the **Target** variable (`label_values(ping_target_info, target)`), so it works for whatever the targets file holds. Nothing is hardcoded to particular sites.

"Up" on this dashboard is the region majority (`iyup:target_up:quorum`), the same series the alerts, `/history` and `/slo` use, so the dashboard cannot disagree with the pager.

Sections, in the order an operator asks:

| Section | Answers |
|---|---|
| Right now | How many targets are up, down, slow or stale? Any alerts firing (not counting the always-on `Watchdog`)? A table with one row per target: state, last result (the reason), failing regions, how long in this state, check age, latency, HTTP code, uptime over the time range, 30-day error budget left, and certificate days. Down targets sort first. |
| History | When was each target up, slow or down? A state timeline of the majority verdict, and below it each region's own verdict, so a regional failure is visible at a glance. Gaps mean no data. |
| Error budgets | How much of each target's 30-day budget is left, and how fast is it burning? The burn-rate panel is log scale with the 14.4x paging line drawn in. |
| Latency | p95 per target, and where the latest check's time went (DNS, TCP, TLS, server). |
| Early warnings | Retries per hour (failures retries absorbed, a sign of trouble ahead), certificate days left, and domain registration days left. |
| Monitoring pipeline | How many agents (regions) can Prometheus scrape, and the gateway? Shortest interval, longest staleness budget, the age of each target's last check against the stale line, and the gateway's own traffic and latency. |

The summary tiles stay neutral when things are fine and turn red or amber only when something needs attention. The colours match the status page: sage for up, brick for down, honey for slow and warnings, lavender for unknown.

The "Target down" annotation marks the moments targets went down, by region majority, on the time-series panels. Toggle it in the dashboard controls.

## Provisioning

Docker Compose mounts:

- `config/grafana-datasources.yml` (Prometheus as the default datasource; panels pick it through the `datasource` variable, so no fixed uid is needed and existing Grafana volumes upgrade cleanly)
- `config/grafana-dashboards.yml` (file provider reading `/etc/grafana/dashboards`)
- `monitoring/grafana-dashboard.json` as `/etc/grafana/dashboards/iyup.json`

Helm renders the same provider file and dashboard through symlinks in `charts/iyup/files/`, and templates the datasource URL.

The dashboard is provisioned read-only. To change it, edit `monitoring/grafana-dashboard.json` (or export from a scratch copy in the UI), then restart Grafana; Grafana 13 did not pick up edits to the mounted file without a restart:

```bash
docker compose restart grafana
```

## Login

`http://localhost:3000`, user `admin`, password `admin` unless `GRAFANA_PASSWORD` overrides it.

## If a panel looks wrong

Run every query in the dashboard against Prometheus, with the variables expanded to "all targets, last hour":

```bash
python3 scripts/check_dashboard_queries.py http://localhost:9090
```

Each query reports `OK` with its series count, `EMPTY`, or `ERROR` with the expanded query. Some panels are legitimately empty, for example "Retries per hour" when nothing needed a retry, or "Domain registration days left" when the targets file lists no domains. The script reads the queries from the dashboard file itself, so it cannot drift from what Grafana runs.
