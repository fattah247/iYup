#!/usr/bin/env python3
"""Plan, list and end maintenance windows.

A window is an Alertmanager silence on the target's alerts whose comment starts
with "[maintenance]", so one command mutes the alerts and tells the status page.

    ./scripts/maintenance.py start https://api.example.com --duration 1h --comment "Database upgrade"
    ./scripts/maintenance.py start https://api.example.com --at 2026-10-14T02:00 --duration 30m --comment "Kernel update"
    ./scripts/maintenance.py start 'https://.*\\.example\\.com' --regex --duration 2h --comment "Network change"
    ./scripts/maintenance.py list
    ./scripts/maintenance.py end https://api.example.com      # or a silence id

Alertmanager is found at $ALERTMANAGER_URL, else http://localhost:$ALERTMANAGER_PORT
(read from .env, default 9093). Standard library only.
"""

import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

MARKER = "[maintenance]"
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


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(alertmanager_url() + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else None
    except urllib.error.HTTPError as exc:
        sys.exit(f"Alertmanager refused the request: HTTP {exc.code} {exc.read().decode().strip()}")
    except urllib.error.URLError as exc:
        sys.exit(f"Cannot reach Alertmanager at {alertmanager_url()}: {exc.reason}")


def parse_duration(text: str) -> timedelta:
    parts = re.findall(r"(\d+)([smhd])", text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r} (use e.g. 30m, 2h, 1h30m, 1d)")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    return sum((timedelta(**{unit[u]: int(n)}) for n, u in parts), timedelta())


def parse_start(text: str) -> datetime:
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid time {text!r} (use e.g. 2026-10-14T02:00)")
    return when.astimezone() if when.tzinfo is None else when


def iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def maintenance_silences():
    silences = call("GET", "/api/v2/silences") or []
    return [s for s in silences
            if s.get("comment", "").lower().startswith(MARKER) and s["status"]["state"] in ("active", "pending")]


def target_of(silence) -> str:
    for m in silence.get("matchers", []):
        if m.get("name") == "target":
            return ("~" if m.get("isRegex") else "") + m["value"]
    return "?"


def start(args):
    begins = args.at or datetime.now().astimezone()
    ends = begins + args.duration
    if ends <= datetime.now().astimezone():
        sys.exit("That window would already be over.")
    body = {
        "matchers": [{"name": "target", "value": args.target, "isRegex": args.regex, "isEqual": True}],
        "startsAt": iso(begins),
        "endsAt": iso(ends),
        "createdBy": args.author,
        "comment": f"{MARKER} {args.comment}".strip(),
    }
    result = call("POST", "/api/v2/silences", body)
    when = "now" if args.at is None else begins.strftime("%Y-%m-%d %H:%M %Z")
    print(f"Maintenance planned for {args.target}: starts {when}, ends {ends.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"Alerts for it are muted for that time and the status page shows the window. Id: {result['silenceID']}")


def list_windows(_args):
    windows = maintenance_silences()
    if not windows:
        print("No active or planned maintenance.")
        return
    for s in sorted(windows, key=lambda s: s["startsAt"]):
        starts = datetime.fromisoformat(s["startsAt"].replace("Z", "+00:00")).astimezone()
        ends = datetime.fromisoformat(s["endsAt"].replace("Z", "+00:00")).astimezone()
        state = "now    " if s["status"]["state"] == "active" else "planned"
        print(f"{state}  {starts:%Y-%m-%d %H:%M} -> {ends:%H:%M}  {target_of(s)}  {s['comment'][len(MARKER):].strip()}  ({s['id']})")


def end(args):
    windows = maintenance_silences()
    matching = [s for s in windows if s["id"] == args.which or target_of(s).lstrip("~") == args.which]
    if not matching:
        sys.exit(f"No active or planned maintenance for {args.which!r}. See: ./scripts/maintenance.py list")
    for s in matching:
        call("DELETE", f"/api/v2/silence/{s['id']}")
        print(f"Ended maintenance for {target_of(s)} ({s['id']}); its alerts are live again.")


def main():
    parser = argparse.ArgumentParser(description="Plan, list and end maintenance windows.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="start or schedule a window")
    p_start.add_argument("target", help="target id as shown by /targets (its URL, tcp://..., dns://...)")
    p_start.add_argument("--duration", type=parse_duration, default=timedelta(hours=1), help="default 1h")
    p_start.add_argument("--at", type=parse_start, help="local start time, e.g. 2026-10-14T02:00 (default: now)")
    p_start.add_argument("--comment", required=True, help="shown on the status page")
    p_start.add_argument("--regex", action="store_true", help="treat target as a regular expression")
    p_start.add_argument("--author", default=getpass.getuser(), help="recorded in Alertmanager, never shown publicly")
    p_start.set_defaults(func=start)

    sub.add_parser("list", help="show active and planned windows").set_defaults(func=list_windows)

    p_end = sub.add_parser("end", help="end a window early, by target or id")
    p_end.add_argument("which")
    p_end.set_defaults(func=end)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
