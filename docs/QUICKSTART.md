# Quickstart

## Local Docker Compose path

Run:

```bash
cp .env.example .env
docker compose up -d --build
./scripts/verify-local.sh
```

Open:

- `http://localhost:8080`: status page
- `http://localhost:8080/docs`: API reference
- `http://localhost:3000`: Grafana (opens on the iYup dashboard)
- `http://localhost:9090/alerts`: Prometheus alerts
- `http://localhost:9093`: Alertmanager

If `.env` overrides the host ports, use those values instead.

## Watch your own sites

Edit `config/targets/targets.yml` (every field is in [CONFIGURATION.md](CONFIGURATION.md)), check it, and restart the agent:

```bash
cd services/ping-agent && go run . validate ../../config/targets/targets.yml && cd -
docker compose up -d ping-agent
```

## Get alerts on your phone

Copy `config/alertmanager.yml` to the git-ignored `config/alertmanager.local.yml`, add a receiver (Telegram is free and pushes to your phone), set `ALERTMANAGER_CONFIG=./config/alertmanager.local.yml` in `.env`, then:

```bash
docker compose up -d alertmanager
./scripts/send-test-alert.py
```

Point the `heartbeat` receiver at a dead man's switch such as healthchecks.io, so you also hear about it when the monitoring itself stops. Details are in [ALERTING.md](ALERTING.md).

## See every state at once

Targets that are slow, flaky, flapping, failing from one region, serving the wrong content, returning 503 and refusing connections, checked by two agents labelled as regions:

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
docker compose logs -f demo-targets     # the notifications the demo "receives"
```

[RELIABILITY_SCENARIOS.md](RELIABILITY_SCENARIOS.md) describes what each one shows.

## Helm render path

Run:

```bash
helm lint ./charts/iyup
helm template iyup ./charts/iyup
```

## Optional Minikube path

Run:

```bash
minikube start
eval "$(minikube -p minikube docker-env)"
docker build -t ping-agent:latest services/ping-agent
docker build -t api-gateway:latest services/api-gateway
helm upgrade --install iyup ./charts/iyup
```
