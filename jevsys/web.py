"""The server's web side.

GET  /           live dashboard (same page as GitHub Pages, polling every second)
GET  /api/state  desk state as JSON (?full=1 adds chart series)
POST /api/ingest prices from the Windows MT5 bridge; needs the bridge token
"""
from __future__ import annotations

import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import dashboard

MAX_BODY = 8 * 1024 * 1024


def serve(desk, port: int, token: str | None = None) -> ThreadingHTTPServer:
    token = token if token is not None else os.environ.get("JEV_BRIDGE_TOKEN", "")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, dashboard.build(desk.state(full=True), live=True).encode(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                full = parse_qs(url.query).get("full") == ["1"]
                self._send(200, json.dumps(desk.state(full=full), separators=(",", ":")).encode())
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/api/ingest":
                return self._send(404, b'{"error":"not found"}')
            if not token:
                return self._send(503, b'{"error":"bridge token not configured on the server"}')
            if not hmac.compare_digest(self.headers.get("X-Bridge-Token", ""), token):
                return self._send(401, b'{"error":"wrong token"}')
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(413, b'{"error":"body too large or empty"}')
            try:
                payload = json.loads(self.rfile.read(n))
                res = desk.market.ingest(payload, time.time())
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, json.dumps({"error": f"bad payload: {e}"}).encode())
            first = getattr(desk, "_bridge_seen", False)
            if not first:
                desk._bridge_seen = True
                desk.event(f"MT5 bridge connected: {payload.get('broker') or '?'} "
                           f"({payload.get('account') or '?'} account), broker clock UTC{payload.get('utc_offset_h', 0):+g}h.")
            self._send(200, json.dumps(res).encode())

        def log_message(self, *a):  # keep journalctl quiet
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    return srv
