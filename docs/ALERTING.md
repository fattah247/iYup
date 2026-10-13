# Alerting

Prometheus decides **when** something is wrong (`config/alert.rules.yml`). Alertmanager decides **who hears about it** (`config/alertmanager.yml`).

## Routing

| Alerts | Receiver | Repeats |
|---|---|---|
| `severity="critical"`: `TargetDown`, `ErrorBudgetBurnFast`, `AllPingAgentsDown` | `page` | every hour while firing |
| everything else: warnings | `notify` | every 4 hours while firing |
| `Watchdog` | `heartbeat` | every minute, forever |

Noise is suppressed where one alert already says it all:

- a target that is down does not also raise `TargetSlow`, `TargetRegionalFailure` or error-budget alerts
- a fast budget burn hides the slow-burn warning for the same target
- with no agents at all, per-agent alerts are muted

## Adding your channels

The shipped config has empty receivers, so alerts show at `http://localhost:9093` and go nowhere else. Keep credentials out of the repo:

```bash
cp config/alertmanager.yml config/alertmanager.local.yml   # git-ignored
echo 'ALERTMANAGER_CONFIG=./config/alertmanager.local.yml' >> .env
```

Fill in the receivers in the local file, check it, and restart Alertmanager:

```bash
docker run --rm -v "$PWD/config:/c:ro" --entrypoint amtool prom/alertmanager:v0.27.0 check-config /c/alertmanager.local.yml
docker compose up -d alertmanager
```

Receiver snippets (Alertmanager 0.27). Use one or several per receiver:

```yaml
receivers:
  - name: page
    telegram_configs:            # phone push, free: create a bot with @BotFather
      - bot_token: "123456:ABC..."
        chat_id: -1001234567890
        send_resolved: true
    email_configs:
      - to: you@example.com
        from: alerts@example.com
        smarthost: smtp.example.com:587
        auth_username: alerts@example.com
        auth_password_file: /etc/alertmanager/secrets/smtp_password
        send_resolved: true
  - name: notify
    discord_configs:
      - webhook_url: "https://discord.com/api/webhooks/..."
        send_resolved: true
    slack_configs:
      - api_url_file: /etc/alertmanager/secrets/slack_url
        channel: "#alerts"
        send_resolved: true
  - name: heartbeat
    webhook_configs:
      - url: "https://hc-ping.com/your-check-uuid"
        send_resolved: false
```

`*_file` fields (`auth_password_file`, `api_url_file`, `bot_token_file`, webhook `url_file`) read the secret from a file. Mount a git-ignored `config/secrets/` directory into the container to use them. Discord's webhook URL has no file variant in 0.27, so keep it in the git-ignored local config.

## Prove it works

```bash
./scripts/send-test-alert.py              # a warning, through the notify route
./scripts/send-test-alert.py --critical   # through the page route
./scripts/send-test-alert.py --resolve
```

It should arrive within about a minute (Alertmanager waits `group_wait`, 30s, before the first notification). If it does not, `docker compose logs alertmanager` shows the delivery error.

The demo overlay routes every receiver to the demo helper, so `docker compose logs demo-targets` shows exactly what was sent, with no accounts needed.

## Heartbeat

`Watchdog` always fires and goes to `heartbeat` every minute. Point that receiver at a dead man's switch, a service that alerts you when a regular ping stops. [Healthchecks.io](https://healthchecks.io) is free for this: create a check with a 1-minute period and 5 minutes of grace, and use its ping URL as the webhook URL.

This covers the failures nothing inside the stack can report: Prometheus or Alertmanager down, the machine off, the network gone. Without it, a dead monitoring stack looks exactly like a quiet day.

## Maintenance windows

Planned work should not page anyone, and should be announced on the status page:

```bash
./scripts/maintenance.py start https://api.example.com --duration 1h --comment "Database upgrade"
./scripts/maintenance.py start https://api.example.com --at 2026-10-14T02:00 --duration 30m --comment "Kernel update"
./scripts/maintenance.py list
./scripts/maintenance.py end https://api.example.com
```

Each window is an Alertmanager silence on the target's alerts, with a comment starting `[maintenance]`. That marker makes it show on the status page: active windows set the target aside from the overall status, and upcoming ones are announced in advance. Silences without the marker still mute alerts but stay private. The status page shows only the comment and the times, never who created the window.

Silences are stored in Alertmanager's data volume, so they survive restarts.

## Error budgets

Every target has an availability objective (`slo`, default 99.9% over 30 days). Its error budget is the downtime the objective allows: 43 minutes a month at 99.9%. The alerts follow the multi-window, multi-burn-rate method from Google's SRE workbook:

| Alert | Fires when | Means |
|---|---|---|
| `ErrorBudgetBurnFast` | the last hour **and** the last 5 minutes spend budget at 14.4x the sustainable rate, or the last 6 hours and 30 minutes at 6x | at this pace the month's budget is gone within days |
| `ErrorBudgetBurnSlow` | the last 3 days **and** 6 hours at more than 1x | the budget will not last the month |

Burn rates use observed downtime divided by the whole window, so a young install or an agent restart does not turn one failed check into a page. Both alerts stay quiet about an outage `TargetDown` already paged for, while that outage is still inside their short window. They exist to catch what `TargetDown` cannot: a target failing every other check never stays down for a full minute.

`/slo` and the status page show each target's budget, and the dashboard's Error budgets section shows budget left and burn rate.
