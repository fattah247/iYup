# Portfolio Proof Checklist

Use this table to see which repo claims have command output, screenshots, or both.

| Proof area | Status | Evidence | Notes |
|---|---|---|---|
| Docker Compose stack | Verified | `./scripts/verify-local.sh`, `docker compose ps`, `docs/screenshots/02-docker-compose-stack.png` | Local stack starts through Docker Compose |
| API health endpoint | Verified | `curl /healthz`, `docs/screenshots/03-api-healthz.png` | API host port follows `.env` when overridden |
| Status API | Verified | `curl /status`, `docs/screenshots/04-api-status-response.png` | Shows target state, latency, and availability |
| Prometheus scrape targets | Verified | Prometheus targets page, `docs/screenshots/05-prometheus-targets.png` | `api-gateway` and `ping-agent` are up |
| Prometheus metrics query | Verified | Prometheus graph query, `docs/screenshots/06-prometheus-metrics.png` | Uses the real `ping_up` metric |
| Grafana dashboard | Verified | `docs/screenshots/07-grafana-dashboard.png` | Captured from the running Docker Compose stack |
| Alertmanager path | Verified | `docs/screenshots/08-alertmanager.png`, `docs/PHASE_0_VERIFICATION.md` | No real outbound credentials are configured |
| Helm rendering | Verified | `helm lint ./charts/iyup`, `helm template iyup ./charts/iyup`, `docs/screenshots/09-helm-template.png` | Render check only |
| Local verification script | Verified | `./scripts/verify-local.sh`, `docs/screenshots/10-local-verification.png` | Repeatable local verification path |
| Down target replay | Verified | `./scripts/reliability/down-target-demo.sh` | Confirms that a dead target becomes down in the API status |
| Transient retry replay | Verified | `./scripts/reliability/transient-target-demo.sh` | Confirms one transient `503` does not leave a failure signal |
| Alert rules behave as intended | Verified | `promtool test rules config/alert.rules.test.yml` (CI job `monitoring-config`) | Covers hard-down, flapping, one clean outage, agent restart, stale checks, agent/API down, certificate expiry |
| TargetDown fires on a real outage | Verified | demo overlay; Prometheus `/api/v1/alerts` | Fired about a minute after the first failed check, with the failure reason in the description |
| TargetDown survives an agent restart | Verified | `docker compose restart ping-agent` while alerts were firing | Stayed firing through the restart; the previous rule resolved and re-paged |
| Inhibition | Verified | Alertmanager `/api/v2/alerts` | `TargetUnreliable` was `suppressed` while `TargetDown` fired for the same target |
| Old rule could not fire reliably | Verified | isolated stack on the previous commit, dead target at the default 30s interval | `increase(ping_failure_total[1m])` alternated 2.67 / 1.33 against a `> 2` threshold |
| Status page | Verified | `http://localhost:${API_GATEWAY_PORT}/`, `docs/screenshots/11-status-page.png` | Checked in light and dark mode, at 375px and desktop width, with keyboard navigation of the uptime bars |
| Status API states and reasons | Verified | `curl /status` with the demo overlay | `http_5xx` (503), `connection_refused` with null latency, certificate days, retries |
| History and outages endpoints | Verified | `curl /history`, `curl /outages` with the demo overlay | A target down for 35 minutes across four agent restarts is one ongoing outage; the flaky target's 30s outages stay separate |
| Dashboard queries | Verified | `python3 scripts/check_dashboard_queries.py` against the demo stack | 31 queries returned data, 0 errors |
| Dashboard rendering | Verified | `docs/screenshots/07-grafana-dashboard.png` | Captured from the demo stack |
| Helm Alertmanager config | Verified | `helm template` output checked with `amtool check-config` and `amtool config routes test` | With email and Slack set, a `TargetDown` alert reaches `alert-log`, `email` and `slack` |
| Helm alert rules | Verified | rendered ConfigMap run through `promtool test rules` | Same file as Compose, through a symlink |
| Upgrade on an existing Grafana volume | Verified | `./scripts/verify-local.sh` against a volume from the previous version | Found and fixed a datasource uid change that crash-looped Grafana |
| Unit tests | Verified | `go test -race ./...` (20 tests), `python -m unittest` (45 tests, Python 3.11 and 3.13) | |

All screenshots were recaptured on 2026-10-13 from the current code; see [SCREENSHOT_GUIDE.md](SCREENSHOT_GUIDE.md) for how.
