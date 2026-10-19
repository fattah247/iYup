# Portfolio Proof Checklist

Use this table to see which repo claims have command output, screenshots, or both.

| Proof area | Status | Evidence | Notes |
|---|---|---|---|
| Docker Compose stack | Verified | `./scripts/verify-local.sh`, `docker compose ps`, `docs/screenshots/02-docker-compose-stack.png` | Local stack starts through Docker Compose; the demo overlay adds a second agent and the demo targets |
| API health endpoint | Verified | `curl /healthz`, `docs/screenshots/03-api-healthz.png` | API host port follows `.env` when overridden |
| Status API | Verified | `curl /status`, `docs/screenshots/04-api-status-response.png` | Majority state, reason, slowness, per-region verdicts and maintenance windows |
| Prometheus scrape targets | Verified | Prometheus targets page, `docs/screenshots/05-prometheus-targets.png` | `api-gateway` and both agents (`lab-a`, `lab-b`) are up |
| Prometheus metrics query | Verified | `ping_last_result == 1`, `docs/screenshots/06-prometheus-metrics.png` | Each region's latest result and its reason |
| Grafana dashboard | Verified | `docs/screenshots/07-grafana-dashboard.png` | Captured from the demo stack after an hour |
| Alertmanager routing | Verified | `docs/screenshots/08-alertmanager.png`, `amtool config routes test` | Critical alerts go to `page`, warnings to `notify`, `Watchdog` to `heartbeat`; a maintenance silence mutes one `TargetDown` |
| Helm rendering | Verified | `helm lint ./charts/iyup`, `helm template iyup ./charts/iyup`, `docs/screenshots/09-helm-template.png` | Render check only |
| Local verification script | Verified | `./scripts/verify-local.sh`, `docs/screenshots/10-local-verification.png` | 18 checks, including recording rules, `/slo`, and the heartbeat reaching Alertmanager; passed on three consecutive cold starts |
| Status page | Verified | `http://localhost:${API_GATEWAY_PORT}/`, `docs/screenshots/11-status-page.png` | Checked in light and dark mode, at 375px and desktop width, with no horizontal overflow |
| Down target replay | Verified | `./scripts/reliability/down-target-demo.sh` | A dead target becomes `down` with `connection_refused` and no latency |
| Transient retry replay | Verified | `./scripts/reliability/transient-target-demo.sh` | One transient `503` is absorbed by a retry and still counted |
| Alert rules behave as intended | Verified | `promtool test rules config/alert.rules.test.yml` (CI job `monitoring-config`) | 14 scenarios: hard down, agent restart, two and three regions, flapping budget burn, one clean outage, young install, slow, stale, agents and API down, certificate and domain expiry, watchdog |
| Checks as code | Verified | `go run . validate` on every file in `config/targets/` (CI job `go`) | Unknown fields, missing variables and duplicates are rejected with every problem listed |
| Region quorum | Verified | demo overlay, `/status` and Prometheus | A target failing only from `lab-b` stays `up` with `regions_failing: 1`, raises `TargetRegionalFailure` (warning) and has no outages |
| Error-budget burn | Verified | demo overlay, `/flapping` target | `ErrorBudgetBurnFast` fired about 4.5 minutes after the target started failing two checks in five, the page reached the demo receiver, and `TargetDown` never fired |
| No page from a young install | Verified | rule test "a young target's first blip"; live stack | A 30-second blip ten minutes after install paged with the first version of the burn rules; error ratios now divide by the whole window |
| Wrong content | Verified | demo overlay | A `200` with the wrong page is `down` with `body_mismatch` |
| Slow target | Verified | demo overlay | `up` and slow at about 1.6s against a 1s threshold; `TargetSlow` fired |
| Maintenance windows | Verified | `scripts/maintenance.py` against the demo stack | `TargetDown` kept firing in Prometheus, was silenced in Alertmanager, sent nothing, and the status page set the target aside and showed the window |
| Heartbeat | Verified | `verify-local.sh`, demo receiver log with timestamps | `Watchdog` reaches Alertmanager within seconds of startup and the `heartbeat` receiver gets it every 60s. The log showed a 2-minute cadence at first: with `repeat_interval` equal to `group_interval`, every other tick fell just short; it is now 50s |
| Test notification | Verified | `scripts/send-test-alert.py` against the demo stack | Arrived at the `notify` receiver, and at `page` with `--critical` |
| Alerts survive agent restarts | Verified | rebuilding both agents while alerts fired | Every firing alert kept its start time; no false resolve was sent |
| Domain expiry | Verified | demo overlay | `example.com` registration read over RDAP: 304 days |
| Dashboard queries | Verified | `python3 scripts/check_dashboard_queries.py` against the demo stack | 38 queries returned data, 1 legitimately empty (no target went down in the range), 0 errors |
| Check counters from startup | Verified | `TestPublishConfigPerTarget`; the demo stack's `/metrics` | A target that never failed reports `ping_failure_total 0` rather than no series, so `success + failure` works from the first check |
| Helm Alertmanager config | Verified | `helm template` with every receiver set, checked with `amtool check-config` and `amtool config routes test` | Same routes as Compose: `page`, `notify`, `heartbeat` |
| Helm alert rules | Verified | rendered ConfigMap run through `promtool test rules` | Same file as Compose, through a symlink |
| Helm checks as code | Verified | `helm template --set-json checks=...`, rendered file run through `ping-agent validate` | |
| Old rule could not fire reliably | Verified | isolated stack on an earlier commit, dead target at the default 30s interval | `increase(ping_failure_total[1m])` alternated 2.67 / 1.33 against a `> 2` threshold |
| Upgrade on an existing Grafana volume | Verified | `./scripts/verify-local.sh` against a volume from an earlier version | Found and fixed a datasource uid change that crash-looped Grafana |
| Unit tests | Verified | `go test -race ./...` (34 tests), `python -m unittest` (62 tests, Python 3.11 and 3.13) | |

All screenshots were recaptured on 2026-10-13 from the current code; see [SCREENSHOT_GUIDE.md](SCREENSHOT_GUIDE.md) for how.
