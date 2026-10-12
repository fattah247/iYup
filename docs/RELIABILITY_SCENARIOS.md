# Reliability Scenarios

The demo overlay starts targets in every state at once:

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

| Target | Behaviour |
|---|---|
| `https://example.com` | real HTTPS site |
| `http://demo-targets:8080/slow` | answers in 0.6–1.4s |
| `http://demo-targets:8080/flaky` | `503` about 40% of the time |
| `http://demo-targets:8080/down` | always `503` |
| `http://demo-targets:9` | nothing listening: connection refused |

The helper is `scripts/reliability/demo_targets.py` (standard library only).

## Healthy endpoint

- state `up`, reason `ok`, latency and DNS/TCP/TLS phases recorded, certificate expiry known
- `TargetDown` does not fire

Verified with the demo overlay: `example.com` reported `up`, HTTP `206` (the agent asks for a single byte), and about 74 days of certificate left.

## Down endpoint

- retries run first, then the check counts as failed
- the API says **why**: `http_5xx` with `http_status: 503`, or `connection_refused` with `latency_ms: null` (no response, no fake fast latency)
- `TargetDown` fires after one minute, with the reason in its description
- the outage appears in `/outages` as `ongoing`

Replay (swaps the targets for a dead port):

```bash
./scripts/reliability/down-target-demo.sh
```

Verified with the demo overlay: both down targets were classified correctly, and `TargetDown` fired about a minute after the first failed check.

## Slow endpoint

- the target stays `up` while latency rises
- p95 moves on the status page and in Grafana; the "Where the latest check's time went" panel shows the time is server-side, not network

The latency histogram buckets are dense between 0.1s and 3s. With Prometheus' default buckets, this endpoint's p95 read 2.4s although no response took longer than 1.4s. The estimate was interpolated across the empty 1–2.5s bucket.

## Transient failure

- one `503` followed by a success is absorbed by a retry: state stays `up`, availability stays 100%
- the retry is still counted in `ping_retries_total`, so absorbed failures are visible ("Retries per hour" panel)

Replay (one `503`, then healthy):

```bash
./scripts/reliability/transient-target-demo.sh
```

## Intermittent failure

- `/flaky` fails some checks outright (all three attempts hit a `503`) but never for a full minute, so `TargetDown` never fires
- `TargetUnreliable` fires when the target is up less than 90% of 15 minutes *and* changed state more than twice; one clean outage that recovers does not leave this warning behind
- Alertmanager suppresses `TargetUnreliable` while `TargetDown` fires for the same target

Pinned by the "flapping target" and "one clean outage" cases in `config/alert.rules.test.yml`.

## Agent restart

- the agent's in-process state (lifetime counts, percentiles, `state_since`) restarts; Prometheus history does not
- `TargetDown` keeps firing across the restart instead of resolving and paging again a minute later

Verified live: alerts stayed firing through `docker compose restart ping-agent`. Pinned by the "agent restart" case in the rule tests.

## Agent stuck or overloaded

- if checks stop finishing, targets turn `unknown` with `stale: true` after `ping_stale_after_seconds`, and the overall status becomes `unknown`, never `operational`
- `TargetChecksStale` fires

Pinned by the "stale" case in the rule tests and by `test_stale_data_is_unknown_not_up` in the gateway tests.

## Agent down

- `/status` answers `502` with `ping-agent is unreachable`; the status page keeps the last data, dims it, and says it could not refresh
- `PingAgentDown` fires; without it, a dead monitor would look like silence

## Prometheus unavailable

- `/status` keeps working: it reads the agent directly
- `/history` and `/outages` answer `502`; the status page shows "History unavailable" and falls back to the agent's lifetime numbers, labelled as such
- `/healthz` stays separate from both dependencies

## Startup

- before the first check, targets are `unknown` / `pending` and the overall status is `unknown`, not `degraded`
- the gateway's target list comes from the agent, so a target configured in one place and not the other cannot be reported as down
