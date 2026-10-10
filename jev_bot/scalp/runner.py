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
from .engine import Engine, Fanout
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

# the strategies ("desks"), each with its own paper account
DESKS = {
    "a": {"name": "A · Pullback", "page": "scalper.html",
          "about": "Buys dips in a 1-minute uptrend (sells rallies in a downtrend) when the 15-minute trend agrees."},
    "orb": {"name": "B · Breakout", "page": "scalper-orb.html",
            "about": "Trades the first break out of the 06:00-07:00 UTC range, once per instrument per day."},
}


# desk -> its section in config/risk.json (the kill switch)
KS_DESK = {"a": "scalper_a", "orb": "scalper_orb"}


def desk_summary(key: str, book: ScalpBook) -> dict:
    s = book.stats.get("_all", {})
    return {"key": key, "name": DESKS[key]["name"], "page": DESKS[key]["page"], "equity": book.equity(),
            "start": book.start_equity, "n": s.get("n", 0), "wins": s.get("wins", 0),
            "open": len(book.positions)}


def snapshot(book: ScalpBook, full: bool, key: str = "a", peers: list | None = None) -> dict:
    d = asdict(book)
    d["desk"] = {"key": key, **DESKS[key]}
    d["peers"] = peers or [desk_summary(key, book)]
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
    from ..research import panel
    d["research"] = panel.safe(panel.scalp_panel, book, key)
    if not full:
        d.pop("equity_log", None)
        d["events"] = d["events"][-60:]
    return d


class _Handler(BaseHTTPRequestHandler):
    state = {"light": b"{}", "full": b"{}"}

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/state":
            desk = (q.get("desk") or ["a"])[0]
            kind = "full" if q.get("full") == ["1"] else "light"
            body = self.state.get(kind if desk == "a" else f"{desk}:{kind}", b"{}")
            ctype = "application/json"
        elif u.path in ("/", "/index.html"):
            from ..research import panel
            body = panel.inject(PAGE.read_text(encoding="utf-8")).replace("/*__DATA__*/", "null").encode()
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


def build_static(book: ScalpBook, out: str, note: str = "", key: str = "a",
                 peers: list | None = None) -> None:
    """The same page with the data baked in, for GitHub Pages (no live refresh)."""
    data = snapshot(book, True, key, peers)
    data["static_note"] = note
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    from ..research import panel
    html = panel.inject(PAGE.read_text(encoding="utf-8")).replace("/*__DATA__*/", blob)
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

    def __init__(self, book: ScalpBook, sender=alerts.send, name: str = "Scalper", feeds: bool = True):
        self.book, self.send = book, sender
        self.name, self.watch_feeds = name, feeds
        self.per_trade = os.environ.get("SCALP_TRADE_ALERTS") == "1"
        self.sent: dict = {}

    def check(self, opened: list, closed: list, now: float, send: bool = True) -> list[str]:
        msgs = []
        if self.per_trade:
            for p in opened:
                msgs.append(f"⚡ {self.name} OPEN {p['side']} {p['symbol']} at {p['entry']:g}")
            for t in closed:
                msgs.append(f"{'✅' if t['pnl'] > 0 else '🔻'} {self.name} CLOSED {t['side']} {t['symbol']} "
                            f"({t['reason']}) net ${t['pnl']:+,.2f}")
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if self.book.halted_day == day and self.sent.get("halt") != day:
            self.sent["halt"] = day
            msgs.append(f"🛑 {self.name} hit its daily loss limit (-2%). No new trades until 00:00 UTC. "
                        "Open trades keep their stops.")
        for src, f in (self.book.feeds.items() if self.watch_feeds else []):
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
            msgs.append(f"⚡ {self.name} · {h:%a %d %b}\nToday: {len(today)} trades, {wins} won, net ${pnl:+,.2f}\n"
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


def orb_config(a) -> ScalpConfig:
    return ScalpConfig(strategy="orb", crypto_fee=a.crypto_fee, fx_commission_per_100k=a.fx_commission,
                       fx_pause_utc=fx_pause_from_env(), metal_pause_utc=(), htf_slow=0,
                       max_trades_per_day=1, cooldown_min=0, max_minutes=24 * 60)


async def main(a) -> None:
    from ..cli import publish
    crypto = parse_list(a.crypto, instruments.CRYPTO_MAJORS)
    fx = parse_list(a.fx, instruments.FX_MAJORS)
    if not crypto and not fx:
        raise SystemExit("nothing to trade: use --crypto and/or --fx")
    cfg = ScalpConfig(crypto_fee=a.crypto_fee, fx_commission_per_100k=a.fx_commission,
                      fx_pause_utc=fx_pause_from_env())
    # desk key -> (account file, book, engine); A trades everything, B (breakout) forex/gold only
    desks = {"a": (a.account, ScalpBook.load(a.account, a.start))}
    desks["a"][1].ks_desk = KS_DESK["a"]
    desks["a"] += (Engine(desks["a"][1], crypto + fx, cfg),)
    if fx and getattr(a, "orb_account", "none") not in ("", "none"):
        ob = ScalpBook.load(a.orb_account, a.start)
        ob.ks_desk = KS_DESK["orb"]
        desks["orb"] = (a.orb_account, ob, Engine(ob, fx, orb_config(a)))
    driver = Fanout([e for _, _, e in desks.values()])
    book = desks["a"][1]
    srcs = []
    if crypto:
        srcs.append(feeds.BinanceFeed(driver, crypto))
    if fx:
        srcs.append(feeds.MT5Feed(driver, fx))
    als = {}
    if alerts.configured():
        als = {k: ScalpAlerts(b, name="Scalper" if k == "a" else "Breakout desk", feeds=(k == "a"))
               for k, (_, b, _) in desks.items()}
    if a.port:
        serve(a.port)
    print(f"scalper: {len(crypto)} crypto, {len(fx)} forex/gold · desks: {', '.join(desks)} · "
          f"dashboard on port {a.port}", flush=True)

    started_code = code_stamp()

    async def housekeeping():
        last_save = last_pub = 0.0
        while True:
            await asyncio.sleep(1)
            now = time.time()
            try:
                peers = []
                for k, (_, b, _) in desks.items():
                    if k != "a":
                        b.feeds = book.feeds                 # one feed, shown on every desk
                    b.mark(now)
                    peers.append(desk_summary(k, b))
                for k, (path, b, e) in desks.items():
                    pre = "" if k == "a" else f"{k}:"
                    _Handler.state[pre + "light"] = json.dumps(snapshot(b, False, k, peers), separators=(",", ":")).encode()
                    if now - last_save >= 5:
                        _Handler.state[pre + "full"] = json.dumps(snapshot(b, True, k, peers), separators=(",", ":")).encode()
                        b.save(path)
                    opened, closed = e.drain()
                    al = als.get(k)
                    if al:
                        # decide here (same thread as the trading), send in the background
                        for m in al.check(opened, closed, now, send=False):
                            asyncio.get_running_loop().run_in_executor(None, al.send, m)
                if now - last_save >= 5:
                    last_save = now
                if a.publish_every and now - last_pub >= a.publish_every * 60:
                    from .. import journal
                    paths = [p for p, _, _ in desks.values()] + [journal.folder(KS_DESK[k]) for k in desks]
                    ok = await asyncio.get_running_loop().run_in_executor(None, publish, *paths)
                    if ok:
                        last_pub = now
                        if a.restart_on_update and code_stamp() != started_code:
                            for p, b, _ in desks.values():
                                b.save(p)
                            print("code updated from GitHub; restarting to use it", flush=True)
                            os._exit(0)                # the Windows loop / systemd starts us again
                    else:
                        last_pub = now - a.publish_every * 60 + 60      # retry in a minute, not every second
            except Exception:
                traceback.print_exc()

    await asyncio.gather(*(f.run() for f in srcs), feeds.clock(driver, srcs), housekeeping())


def run(a) -> None:
    try:
        asyncio.run(main(a))
    except KeyboardInterrupt:
        print("\nstopped; the account is saved.")
