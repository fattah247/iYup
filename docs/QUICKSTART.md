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

To see targets in every state (slow, flaky, returning 503, refusing connections):

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

If `.env` overrides the host ports, use those values instead.

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
