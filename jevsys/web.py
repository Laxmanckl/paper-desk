"""Live dashboard on the server: the same page as GitHub Pages, but polling
/api/state every second instead of showing a 15-minute snapshot."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import dashboard


def serve(desk, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                body = dashboard.build(desk.state(full=True), live=True).encode()
                ctype = "text/html; charset=utf-8"
            elif url.path == "/api/state":
                full = parse_qs(url.query).get("full") == ["1"]
                body = json.dumps(desk.state(full=full), separators=(",", ":")).encode()
                ctype = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # keep journalctl quiet
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    return srv
