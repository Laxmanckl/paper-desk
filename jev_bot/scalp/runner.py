"""Runs the scalper 24/7: feeds, 1-second clock, saving, the live dashboard,
publishing to GitHub, and Telegram messages.

    python -m jev_bot scalp                       # crypto majors (Binance)
    python -m jev_bot scalp --fx majors           # + forex/gold via MT5 (Windows)
    python -m jev_bot scalp --crypto none --fx majors

The live dashboard is served at http://<server>:8080 and refreshes every second.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import alerts
from . import feeds, instruments
from .book import ScalpBook
from .engine import Engine
from .strategy import ScalpConfig

PAGE = Path(__file__).with_name("dashboard.html")


# --- what the dashboard reads ----------------------------------------------------

def pages_url() -> str:
    """https://<owner>.github.io/<repo>/ from the git remote, for the link to the daily dashboard."""
    import re
    import subprocess
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
        return f"https://{m.group(1).lower()}.github.io/{m.group(2)}/" if m else ""
    except Exception:
        return ""


PAGES = {"url": None}


def snapshot(book: ScalpBook, full: bool) -> dict:
    d = asdict(book)
    if PAGES["url"] is None:
        PAGES["url"] = pages_url()
    d["daily_url"] = PAGES["url"]
    d["equity"] = book.equity()
    d["unrealised"] = {s: round(book.unrealised(s), 2) for s in book.positions}
    d["server_time"] = time.time()
    syms = set(book.prices) | set(book.signals) | set(book.positions) | {t["symbol"] for t in book.trades[-200:]}
    d["specs"] = {s: {"digits": instruments.get(s).digits, "kind": instruments.get(s).kind,
                      "pip": instruments.get(s).pip} for s in syms}
    d["trades"] = d["trades"][-200:]
    if not full:
        d.pop("equity_log", None)
        d["events"] = d["events"][-60:]
    return d


class _Handler(BaseHTTPRequestHandler):
    state = {"light": b"{}", "full": b"{}"}

    def do_GET(self):
        if self.path.startswith("/api/state"):
            body = self.state["full" if "full=1" in self.path else "light"]
            ctype = "application/json"
        elif self.path in ("/", "/index.html"):
            body = PAGE.read_bytes().replace(b"/*__DATA__*/", b"null")
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                       # keep the service log quiet
        pass


def serve(port: int) -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def build_static(book: ScalpBook, out: str, note: str = "") -> None:
    """The same page with the data baked in, for GitHub Pages (no live refresh)."""
    data = snapshot(book, full=True)
    data["static_note"] = note
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = PAGE.read_text(encoding="utf-8").replace("/*__DATA__*/", blob)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    Path(out).write_text("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                         "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">"
                         "<style>*,*::before,*::after{box-sizing:border-box}body{margin:0}"
                         "[hidden]{display:none!important}</style></head><body>" + html + "</body></html>",
                         encoding="utf-8")


# --- alerts ------------------------------------------------------------------------

class ScalpAlerts:
    """Telegram: daily summary, risk halt, feed down >5 min; every trade only if
    SCALP_TRADE_ALERTS=1 (a scalper trades a lot)."""

    def __init__(self, book: ScalpBook, sender=alerts.send):
        self.book, self.send = book, sender
        self.per_trade = os.environ.get("SCALP_TRADE_ALERTS") == "1"
        self.sent: dict = {}

    def check(self, opened: list, closed: list, now: float, send: bool = True) -> list[str]:
        msgs = []
        if self.per_trade:
            for p in opened:
                msgs.append(f"⚡ Scalp OPEN {p['side']} {p['symbol']} at {p['entry']:g}")
            for t in closed:
                msgs.append(f"{'✅' if t['pnl'] > 0 else '🔻'} Scalp CLOSED {t['side']} {t['symbol']} "
                            f"({t['reason']}) net ${t['pnl']:+,.2f}")
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if self.book.halted_day == day and self.sent.get("halt") != day:
            self.sent["halt"] = day
            msgs.append("🛑 Scalper hit its daily loss limit (-2%). No new trades until 00:00 UTC. "
                        "Open trades keep their stops.")
        for src, f in self.book.feeds.items():
            key = f"down_{src}"
            if f.get("status") != "live" and now - f.get("t", now) > 300 and not self.sent.get(key):
                self.sent[key] = True
                msgs.append(f"⚠️ Scalper: {src} prices down for 5+ min ({f.get('detail', '')[:120]}). "
                            "Retrying automatically.")
            elif f.get("status") == "live" and self.sent.pop(key, None):
                msgs.append(f"👍 Scalper: {src} prices back.")
        h = datetime.fromtimestamp(now, timezone.utc)
        if h.hour >= 21 and self.sent.get("daily") != day:
            self.sent["daily"] = day
            s = self.book.stats.get("_all", {})
            today = [t for t in self.book.trades if t["closed"][:10] == day]
            pnl = sum(t["pnl"] for t in today)
            wins = sum(t["pnl"] > 0 for t in today)
            msgs.append(f"⚡ Scalper · {h:%a %d %b}\nToday: {len(today)} trades, {wins} won, net ${pnl:+,.2f}\n"
                        f"Account ${self.book.equity():,.2f} ({(self.book.equity() / self.book.start_equity - 1) * 100:+.2f}%)\n"
                        f"All time: {s.get('n', 0)} trades, costs paid ${s.get('fees', 0):,.2f}")
        if send:
            for m in msgs:
                self.send(m)
        return msgs


# --- main ----------------------------------------------------------------------------

def parse_list(value: str, majors: dict) -> list[str]:
    if not value or value == "none":
        return []
    if value == "majors":
        return list(majors)
    return [instruments.get(s).symbol for s in value.split(",") if s.strip()]


def fx_pause_from_env() -> tuple:
    """SCALP_FX_PAUSE="off" trades straight through rollover; "20.5-22" sets another window."""
    raw = os.environ.get("SCALP_FX_PAUSE", "").strip().lower()
    if raw in ("off", "none", "0"):
        return ()
    try:
        lo, hi = (float(x) for x in raw.split("-"))
        return (lo, hi)
    except ValueError:
        return ScalpConfig.fx_pause_utc


def code_stamp() -> float:
    """Newest modification time of the bot's code: changes when a git pull brings an update."""
    root = Path(__file__).resolve().parent.parent
    return max((f.stat().st_mtime for f in root.rglob("*.py")), default=0.0)


async def main(a) -> None:
    from ..cli import publish
    crypto = parse_list(a.crypto, instruments.CRYPTO_MAJORS)
    fx = parse_list(a.fx, instruments.FX_MAJORS)
    if not crypto and not fx:
        raise SystemExit("nothing to trade: use --crypto and/or --fx")
    book = ScalpBook.load(a.account, a.start)
    cfg = ScalpConfig(crypto_fee=a.crypto_fee, fx_commission_per_100k=a.fx_commission,
                      fx_pause_utc=fx_pause_from_env())
    engine = Engine(book, crypto + fx, cfg)
    book.config = dict(cfg.__dict__)
    srcs = []
    if crypto:
        srcs.append(feeds.BinanceFeed(engine, crypto))
    if fx:
        srcs.append(feeds.MT5Feed(engine, fx))
    al = ScalpAlerts(book) if alerts.configured() else None
    if a.port:
        serve(a.port)
    print(f"scalper: {len(crypto)} crypto, {len(fx)} forex/gold · dashboard on port {a.port}", flush=True)

    started_code = code_stamp()

    async def housekeeping():
        last_save = last_pub = 0.0
        while True:
            await asyncio.sleep(1)
            now = time.time()
            try:
                book.mark(now)
                _Handler.state["light"] = json.dumps(snapshot(book, False), separators=(",", ":")).encode()
                if now - last_save >= 5:
                    _Handler.state["full"] = json.dumps(snapshot(book, True), separators=(",", ":")).encode()
                    book.save(a.account)
                    last_save = now
                opened, closed = engine.drain()
                if al:
                    # decide here (same thread as the trading), send in the background
                    for m in al.check(opened, closed, now, send=False):
                        asyncio.get_running_loop().run_in_executor(None, al.send, m)
                if a.publish_every and now - last_pub >= a.publish_every * 60:
                    ok = await asyncio.get_running_loop().run_in_executor(None, publish, a.account)
                    if ok:
                        last_pub = now
                        if a.restart_on_update and code_stamp() != started_code:
                            book.save(a.account)
                            print("code updated from GitHub; restarting to use it", flush=True)
                            os._exit(0)                # the Windows loop / systemd starts us again
            except Exception:
                traceback.print_exc()

    await asyncio.gather(*(f.run() for f in srcs), feeds.clock(engine, srcs), housekeeping())


def run(a) -> None:
    try:
        asyncio.run(main(a))
    except KeyboardInterrupt:
        print("\nstopped; the account is saved.")
