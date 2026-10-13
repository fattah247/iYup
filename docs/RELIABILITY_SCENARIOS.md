# Reliability Scenarios

The demo overlay starts targets in every state at once, checked by two agents labelled as regions `lab-a` and `lab-b`:

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

| Target | Behaviour |
|---|---|
| Example (HTTPS) `https://example.com` | real HTTPS site, with certificate and domain expiry |
| Health API `/health` | `200` with `{"status":"ok"}`, which the check requires |
| Slow page `/slow` | answers in 1.2–1.8s; the demo calls anything over 1s slow |
| Flaky page `/flaky` | `503` for about 40% of requests, at random |
| Flapping page `/flapping` | `503` for the first 30s of every 75s, by the clock, for every region |
| Regional outage `/regional` | `503` for `lab-b` only |
| Wrong content `/wrong-content` | `200`, but a maintenance page instead of the expected text |
| Broken page `/down` | always `503` |
| Nothing listening `http://demo-targets:9` | connection refused |
| Demo TCP port `tcp://demo-targets:8080` | TCP check |
| example.com DNS `dns://example.com/A` | DNS check |

The helper is `scripts/reliability/demo_targets.py` (standard library only). It also stands in for your notification channels: the demo Alertmanager config sends every receiver to it, so this shows exactly what would have reached you:

```bash
docker compose logs -f demo-targets
```

Alert behaviour in every scenario below is also pinned by `promtool` unit tests in `config/alert.rules.test.yml`, which CI runs.

## Healthy endpoint

- state `up`, reason `ok`, latency and DNS/TCP/TLS phases recorded, certificate and domain expiry known
- no alert fires

Verified with the demo overlay: `example.com` reported `up`, HTTP `206` (the agent asks for a single byte), about 74 days of certificate and 304 days of domain registration left.

## Down endpoint

- retries run first, then the check counts as failed
- the API says **why**: `http_5xx` with `http_status: 503`, or `connection_refused` with `latency_ms: null` (no response, no fake fast latency)
- `TargetDown` fires after one minute once a majority of regions agree, with the reason in its description, and goes to the `page` receiver
- the outage appears in `/outages` as `ongoing`

Replay (swaps the targets for a dead port):

```bash
./scripts/reliability/down-target-demo.sh
```

Verified with the demo overlay: both down targets were classified correctly, `TargetDown` fired about a minute after the first failed check, and the `page` notification showed up in the demo receiver's log.

## Wrong content

A server that answers `200` with the wrong page, such as a maintenance page, a login wall or an error rendered with a success code, is down from the user's side.

- `expect.body_contains` fails the check with reason `body_mismatch`, keeping `http_status: 200`
- `TargetDown` fires like any other outage

Verified with the demo overlay: "Wrong content" is `down`, `body_mismatch`, HTTP 200.

## Slow endpoint

- the target stays `up` and `slow` turns `true` once most regions see it over its `degraded_after`; the overall status becomes `degraded`
- `TargetSlow` fires after 5 minutes of it, as a warning
- p95 moves on the status page and in Grafana; the "Where the latest check's time went" panel shows the time is server-side, not network

Verified with the demo overlay: "Slow page" `up` and slow at about 1.6s; `TargetSlow` fired.

The latency histogram buckets are dense between 0.1s and 3s. With Prometheus' default buckets, this endpoint's p95 read 2.4s although no response took longer than 1.8s, because the estimate was interpolated across the empty 1–2.5s bucket.

## Transient failure

- one `503` followed by a success is absorbed by a retry: state stays `up`, availability stays 100%
- the retry is still counted in `ping_retries_total`, so absorbed failures are visible ("Retries per hour" panel)

Replay (one `503`, then healthy):

```bash
./scripts/reliability/transient-target-demo.sh
```

## Intermittent failure (random)

- retries absorb most of `/flaky`'s failures; about one check in sixteen fails all three attempts
- those failures are independent per region, so a majority rarely agrees and the target's quorum availability stays near 100%: the failures users hit are too short and scattered to show up as downtime from two vantage points
- the retries are visible: "Flaky page" keeps climbing in "Retries per hour" while staying up, and its raw availability across both regions on the status page is about 92%

## Intermittent failure (flapping): error budget burn

A target that fails every other check never stays down for a full minute, so `TargetDown` never fires, yet users see constant errors. This is what the error-budget alerts are for.

- `/flapping` fails two checks in five from every region, never two in a row; the majority verdict is down for about a fifth of the time, in stretches of 30s at most
- `TargetDown` stays quiet
- `ErrorBudgetBurnFast` fires (critical, `page`) once the last hour and the last 5 minutes both spend the budget at more than 14.4x the sustainable rate; on a fresh install that takes a few minutes, because downtime is divided by the whole hour
- `/slo` and the status page show the budget draining

Verified with the demo overlay: `ErrorBudgetBurnFast` fired about 4.5 minutes after `/flapping` started failing (majority verdict down 17% of the last 6 minutes), the `page` notification reached the demo receiver, and `TargetDown` never fired. Pinned by the "flapping target" case in the rule tests.

## Failure from one region

- `/regional` fails only for `lab-b`. One region of two is not a majority, so the target stays `up`, with `regions_failing: 1`, and the status page shows "failing from lab-b"
- `TargetRegionalFailure` fires after 5 minutes, as a warning, not a page
- with both regions failing it becomes `TargetDown`; with three regions, two failing is down and one failing is regional

Verified with the demo overlay: the target stayed `up` for the whole hour and `/outages` lists nothing for it. That check caught a bug: history and outages fell back to raw per-region data for any 15-second step that happened to miss a majority sample, which turned this target into a string of false 15-second outages. The fallback now applies only where the recording rules have produced nothing for 5 minutes, and votes by majority too. Pinned by the two-region and three-region cases in the rule tests and by `test_uses_the_quorum_series_with_a_fallback`.

## A young install

Error-budget math on a ten-minute-old install is dangerous: two failed checks out of twenty-seven look like a 74x burn if you divide by what was observed. The error ratios divide observed downtime by the whole window instead, so missing data counts as unknown rather than as failure.

Pinned by the "young target, first blip" case in the rule tests.

## Planned maintenance

```bash
./scripts/maintenance.py start http://demo-targets:8080/down --duration 45m --comment "Replacing the disk"
```

- Alertmanager silences the target's alerts: Prometheus still evaluates `TargetDown`, but nothing is sent
- the status page shows the window, sets the target aside from the overall status and summary, and keeps its own state truthful
- upcoming windows (`--at`) are announced before they start
- downtime during the window still counts against the error budget

Verified with the demo overlay: `TargetDown` for "Broken page" stayed `firing` in Prometheus, appeared as suppressed in Alertmanager, no notification reached the demo receiver, and the status page showed "Replacing the disk" with the target set aside.

## Agent restart

- the agent's in-process state (lifetime counts, percentiles, `state_since`) restarts; Prometheus history does not
- `TargetDown` keeps firing across the restart instead of resolving and paging again a minute later: the recording rules bridge a 2-minute gap

Verified live: alerts stayed firing through `docker compose restart ping-agent ping-agent-b`. Pinned by the "agent restart" case in the rule tests.

## One region lost

- `PingAgentDown` fires for that agent, as a warning
- the remaining regions keep voting: once the lost region's data is 2 minutes old, it drops out of the count rather than being counted as up or down
- `/status` shows the agent as unreachable in `monitor.agents` and still answers

Pinned by the "one of two agents down" case in the rule tests, and by `test_one_agent_down_still_answers` and `test_stale_regions_do_not_vote` in the gateway tests.

## Agent stuck or overloaded

- if checks stop finishing, targets turn `unknown` with `stale: true` after `ping_stale_after_seconds`, and the overall status becomes `unknown`, never `operational`
- `TargetChecksStale` fires

Pinned by the "stale" case in the rule tests and by `test_stale_data_is_unknown_not_up` in the gateway tests.

## Every agent down

- `/status` answers `502` with `ping-agent is unreachable`; the status page keeps the last data, dims it, and says it could not refresh
- `AllPingAgentsDown` fires (critical); per-agent alerts are muted so it says everything in one page

Pinned by the "losing the only agent" case in the rule tests and by `test_status_when_every_agent_is_unreachable`.

## Prometheus or Alertmanager down, or the whole machine

- `/status` keeps working without Prometheus: it reads the agents directly
- `/history`, `/outages` and `/slo` answer `502`; the status page shows "History unavailable" and falls back to the agents' lifetime numbers, labelled as such
- without Alertmanager, `/status` still answers, with `monitor.alertmanager: "unreachable"` and no maintenance windows
- nothing inside the stack can report its own death, so the `Watchdog` heartbeat stops reaching your external dead man's switch, and that service alerts you (see [ALERTING.md](ALERTING.md#heartbeat))

Pinned by the "watchdog" case in the rule tests and by `test_status_survives_alertmanager_outage`; the demo receiver logs a heartbeat every minute.

## Certificates and domains

- `CertificateExpiringSoon` fires 14 days before a certificate expires, `DomainExpiringSoon` 30 days before a domain registration does
- an expired or otherwise rejected certificate fails the check with reason `tls`, and its expiry is still read and reported (negative days)
- a failed RDAP lookup is retried with backoff from 5 minutes, so a network blip at startup does not leave the expiry unknown for half a day

Pinned by the certificate and domain cases in the rule tests.

## Startup

- before the first check, targets are `unknown` / `pending` and the overall status is `unknown`, not `degraded`
- the gateway's target list comes from the agents, so a target configured in one place and not the other cannot be reported as down
- a broken targets file stops the agent with every problem listed, rather than starting with fewer checks than you think
