#!/usr/bin/env python3
"""Run every PromQL query in the Grafana dashboard against a live Prometheus.

Grafana variables are expanded to "all targets, last hour". Each query is
reported as OK (with its series count), EMPTY, or ERROR. Exits 1 if any query
errors; EMPTY is only a warning because some panels are legitimately empty
(for example "Retries per hour" when nothing has needed a retry).

Usage:
    python3 scripts/check_dashboard_queries.py [prometheus_url] [dashboard.json]
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DASHBOARD = os.path.join(ROOT, "monitoring", "grafana-dashboard.json")

SUBSTITUTIONS = {
    '"$target"': '".+"',
    "$__rate_interval": "1m",
    "$__range": "1h",
    "$__interval": "15s",
}


def expand(expr: str) -> str:
    for variable, value in SUBSTITUTIONS.items():
        expr = expr.replace(variable, value)
    return expr


def queries(dashboard: dict):
    def walk(panels):
        for panel in panels:
            if panel.get("type") == "row":
                yield from walk(panel.get("panels", []))
                continue
            for target in panel.get("targets", []):
                if target.get("expr"):
                    yield panel.get("title", "?"), target.get("refId", "?"), target["expr"]

    yield from walk(dashboard.get("panels", []))
    for annotation in dashboard.get("annotations", {}).get("list", []):
        if annotation.get("expr"):
            yield f"annotation: {annotation.get('name')}", "-", annotation["expr"]


def run(prometheus: str, expr: str) -> tuple[str, str]:
    url = f"{prometheus.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.load(exc)
        except ValueError:
            return "ERROR", f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return "ERROR", f"cannot reach Prometheus: {exc}"
    if payload.get("status") != "success":
        return "ERROR", payload.get("error", "unknown error")
    result = payload["data"]["result"]
    if payload["data"].get("resultType") == "scalar" or result:
        count = 1 if payload["data"].get("resultType") == "scalar" else len(result)
        return "OK", f"{count} series"
    return "EMPTY", "no series"


def main() -> int:
    prometheus = sys.argv[1] if len(sys.argv) > 1 else os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DASHBOARD
    with open(path) as handle:
        dashboard = json.load(handle)

    counts = {"OK": 0, "EMPTY": 0, "ERROR": 0}
    for title, ref, expr in queries(dashboard):
        verdict, detail = run(prometheus, expand(expr))
        counts[verdict] += 1
        print(f"{verdict:<5} {title} [{ref}]: {detail}")
        if verdict == "ERROR":
            print(f"      query: {expand(expr)}")

    print(f"\n{counts['OK']} ok, {counts['EMPTY']} empty, {counts['ERROR']} errors ({dashboard.get('title')})")
    return 1 if counts["ERROR"] else 0


if __name__ == "__main__":
    sys.exit(main())
