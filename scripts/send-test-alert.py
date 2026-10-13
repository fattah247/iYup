#!/usr/bin/env python3
"""Send a test alert through Alertmanager to prove notifications reach you.

    ./scripts/send-test-alert.py                 # a warning: goes to the "notify" receiver
    ./scripts/send-test-alert.py --critical      # goes to the "page" receiver
    ./scripts/send-test-alert.py --resolve       # resolves the test alert

Alertmanager waits group_wait (30s) before the first notification. If nothing
arrives within a minute, check the receiver in your Alertmanager config and
`docker compose logs alertmanager`. Standard library only.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def alertmanager_url() -> str:
    if os.getenv("ALERTMANAGER_URL"):
        return os.environ["ALERTMANAGER_URL"].rstrip("/")
    port = os.getenv("ALERTMANAGER_PORT")
    env_file = os.path.join(ROOT, ".env")
    if not port and os.path.exists(env_file):
        for line in open(env_file):
            if line.strip().startswith("ALERTMANAGER_PORT="):
                port = line.split("=", 1)[1].strip()
    return f"http://localhost:{port or 9093}"


def iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def main():
    parser = argparse.ArgumentParser(description="Send a test alert through Alertmanager.")
    parser.add_argument("--critical", action="store_true", help="test the paging route instead of notifications")
    parser.add_argument("--resolve", action="store_true", help="resolve a test alert sent earlier")
    args = parser.parse_args()

    severity = "critical" if args.critical else "warning"
    now = datetime.now(timezone.utc)
    alert = {
        "labels": {"alertname": "IyupTestNotification", "severity": severity, "target": "test://iyup"},
        "annotations": {
            "summary": f"Test {severity} notification from iYup",
            "description": "Sent by scripts/send-test-alert.py to check that alerts reach you. Nothing is wrong.",
        },
        "startsAt": iso(now - timedelta(seconds=1)),
        "endsAt": iso(now if args.resolve else now + timedelta(minutes=5)),
    }
    req = urllib.request.Request(f"{alertmanager_url()}/api/v2/alerts", data=json.dumps([alert]).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.URLError as exc:
        sys.exit(f"Cannot reach Alertmanager at {alertmanager_url()}: {getattr(exc, 'reason', exc)}")

    if args.resolve:
        print("Test alert resolved. Receivers with send_resolved: true get a 'resolved' message.")
    else:
        receiver = "page" if args.critical else "notify"
        print(f"Test {severity} alert sent to {alertmanager_url()}; it routes to the '{receiver}' receiver.")
        print("Expect it within about a minute. It resolves by itself after 5 minutes (or run with --resolve).")


if __name__ == "__main__":
    main()
