#!/usr/bin/env python3
"""Endpoints that behave badly on purpose, so every iYup state can be seen locally.

    /               always 200
    /health         200 with {"status":"ok"}
    /slow           200 after 1.2-1.8s (the demo marks above 1s as slow)
    /flaky          503 about 40% of the time (retries usually absorb it; sometimes they cannot)
    /flapping       503 for the first 30s of every 75s by the clock, so every region sees the
                    same failures: down a third of the time, never for a full minute
    /down           always 503
    /regional       503 for the agent in region lab-b only (read from its User-Agent)
    /wrong-content  200, but a maintenance page instead of the real one

It also stands in for your notification channels: POST /webhook/<receiver>
(where the demo Alertmanager config sends alerts) prints each notification,
so `docker compose logs demo-targets` shows what would have reached you.

Used by docker-compose.demo.yml. Standard library only.
"""

import json
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._respond(include_body=True)

    def do_HEAD(self) -> None:
        self._respond(include_body=False)

    def do_POST(self) -> None:
        receiver = self.path.rsplit("/", 1)[-1] if self.path.startswith("/webhook/") else None
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        if receiver is None:
            self.send_response(404)
            self.end_headers()
            return
        for alert in payload.get("alerts", []):
            labels, notes = alert.get("labels", {}), alert.get("annotations", {})
            print(f"[{receiver}] {alert.get('status', '?').upper():8} {labels.get('alertname', '?')}"
                  f" {labels.get('target', labels.get('domain', ''))} | {notes.get('summary', '')}", flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return

    def _respond(self, include_body: bool) -> None:
        path = self.path.split("?", 1)[0]
        status = 200
        body = None
        if path == "/slow":
            time.sleep(random.uniform(1.2, 1.8))
        elif path == "/health":
            body = b'{"status":"ok","checks":{"db":"up"}}\n'
        elif path == "/flaky":
            status = 503 if random.random() < 0.4 else 200
        elif path == "/flapping":
            # With 30s checks, 75s periods shift each check 15s against the
            # cycle: every region fails two checks in five, never two in a row.
            status = 503 if time.time() % 75 < 30 else 200
        elif path == "/down":
            status = 503
        elif path == "/regional":
            status = 503 if "region=lab-b" in self.headers.get("User-Agent", "") else 200
        elif path == "/wrong-content":
            body = b"<html><body><h1>Down for maintenance</h1></body></html>\n"
        elif path != "/":
            status = 404
        body = body or f"{status}\n".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), DemoHandler).serve_forever()
