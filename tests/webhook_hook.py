"""A local HTTP server that records webhook POSTs. Shared by unit and integration tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Hook:
    def __init__(self, status=200):
        outer = self
        self.received, self.status = [], status

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.received.append((self.headers["Content-Type"], json.loads(body)))
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *_):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
