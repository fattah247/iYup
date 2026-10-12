#!/usr/bin/env python3
"""Endpoints that behave badly on purpose, so every iYup state can be seen locally.

    /        always 200
    /slow    200 after 0.6-1.4s
    /flaky   503 about 40% of the time (retries usually absorb it; sometimes they cannot)
    /down    always 503

Used by docker-compose.demo.yml. Standard library only.
"""

import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._respond(include_body=True)

    def do_HEAD(self) -> None:
        self._respond(include_body=False)

    def log_message(self, format: str, *args) -> None:
        return

    def _respond(self, include_body: bool) -> None:
        path = self.path.split("?", 1)[0]
        status = 200
        if path == "/slow":
            time.sleep(random.uniform(0.6, 1.4))
        elif path == "/flaky":
            status = 503 if random.random() < 0.4 else 200
        elif path == "/down":
            status = 503
        elif path != "/":
            status = 404
        body = f"{status}\n".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), DemoHandler).serve_forever()
