"""MT5 bridge (runs on the Windows PC next to MetaTrader 5).

Every 10 seconds it reads the latest quote, the last closed 5-minute bars and
the last closed 1-hour bars (with tick volume, for the volume breakout account)
for gold + the forex majors from your MT5 terminal, converts the broker's
clock to UTC, and sends them to the Jev Desk on your Lightsail server.

It only READS prices. It never places, changes or closes an order.

    python mt5_bridge.py           run forever (the scheduled task does this)
    python mt5_bridge.py --once    one push, print the result, exit (setup test)

Settings: settings.json next to this file:
    {"server": "http://1.2.3.4:8080", "token": "...", "interval_s": 10,
     "mt5_path": "", "symbols": {"XAUUSD": "XAUUSD.m"}}   # symbols optional
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

import MetaTrader5 as mt5

VERSION = "1.2"
HISTORY_PER_PUSH = 2  # symbols backfilled per push, so no single upload is large
H1_HISTORY_PER_PUSH = 1  # hourly history is ~8000 bars per symbol: one symbol per push
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "settings.json")
OFFSET_FILE = os.path.join(HERE, "utc_offset.json")
ALIVE_FILE = os.path.join(HERE, "alive.txt")
WANTED = ["XAUUSD", "EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCAD", "USDCHF", "NZDUSD"]
ALIASES = {"XAUUSD": ["GOLD"]}


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def load_settings() -> dict:
    with open(SETTINGS, encoding="utf-8-sig") as fh:  # PowerShell 5 writes a BOM
        s = json.load(fh)
    s["server"] = s["server"].rstrip("/")
    if not s["server"].startswith("http"):
        s["server"] = "http://" + s["server"]
    return s


def connect(settings: dict) -> None:
    ok = mt5.initialize(settings["mt5_path"]) if settings.get("mt5_path") else mt5.initialize()
    if not ok:
        raise RuntimeError(f"MT5 not reachable: {mt5.last_error()}. Is MetaTrader 5 open and logged in?")


def resolve_symbols(settings: dict) -> dict[str, str]:
    """Map WANTED names to this broker's names (e.g. XAUUSD -> XAUUSD.m or GOLD)."""
    names = [s.name for s in (mt5.symbols_get() or [])]
    out = {}
    for want in WANTED:
        pick = (settings.get("symbols") or {}).get(want)
        if not pick:
            exact = [n for n in names if n.upper() == want]
            starts = sorted((n for n in names if n.upper().startswith(want)), key=len)
            alias = [n for n in names if n.upper() in ALIASES.get(want, [])]
            pick = (exact or starts or alias or [None])[0]
        if pick and mt5.symbol_select(pick, True):
            out[want] = pick
        else:
            log(f"WARNING: {want} not found at this broker; add it to settings.json 'symbols' if it has another name")
    return out


class Clock:
    """MT5 stamps ticks and bars in the broker's server time (often UTC+2/+3).
    While the market is open the newest tick is only seconds old, so
    (tick time - real time), rounded to 30 min, is the broker's UTC offset.
    When the market is closed the last known offset is reused."""

    def __init__(self):
        self.offset_s = 0.0
        self.known = False
        try:
            with open(OFFSET_FILE, encoding="utf-8-sig") as fh:
                self.offset_s, self.known = float(json.load(fh)["offset_s"]), True
        except (OSError, ValueError, KeyError):
            pass

    def update(self, newest_tick_time: float) -> None:
        diff = newest_tick_time - time.time()
        guess = round(diff / 1800) * 1800
        if abs(diff - guess) < 90:  # a tick from the last ~90 s: market is open
            if guess != self.offset_s or not self.known:
                log(f"broker clock is UTC{guess / 3600:+.1f}h")
                self.offset_s, self.known = guess, True
                with open(OFFSET_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"offset_s": guess}, fh)

    def utc(self, broker_ts: float) -> float:
        return broker_ts - self.offset_s


def _rows(rates, clock: Clock) -> list:
    return [] if rates is None else [
        [clock.utc(float(r["time"])), float(r["open"]), float(r["high"]), float(r["low"]),
         float(r["close"]), float(r["tick_volume"]), float(r["spread"])] for r in rates]


def build_payload(symbols: dict[str, str], clock: Clock, need: set[str], history: int,
                  need_h1: set[str] = frozenset(), h1_history: int = 8000) -> dict:
    ticks = {w: mt5.symbol_info_tick(b) for w, b in symbols.items()}
    newest = max((t.time_msc / 1000 for t in ticks.values() if t), default=None)
    if newest:
        clock.update(newest)
    acct = mt5.account_info()
    out = {"sent": time.time(), "version": VERSION, "utc_offset_h": clock.offset_s / 3600,
           "broker": acct.company if acct else None, "server": acct.server if acct else None,
           "account": "demo" if acct and acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "live",
           "symbols": {}}
    for want, broker in symbols.items():
        info = mt5.symbol_info(broker)
        t = ticks.get(want)
        n = history if want in need else 3  # need = only the few symbols chosen for this push
        bars = _rows(mt5.copy_rates_from_pos(broker, mt5.TIMEFRAME_M5, 1, n), clock)  # position 1 = last CLOSED bar
        n1 = h1_history if want in need_h1 else 3
        bars_h1 = _rows(mt5.copy_rates_from_pos(broker, mt5.TIMEFRAME_H1, 1, n1), clock)
        out["symbols"][want] = {
            "broker_symbol": broker,
            "point": info.point if info else None,
            "quote": {"t": clock.utc(t.time_msc / 1000), "bid": t.bid, "ask": t.ask} if t and t.bid > 0 else None,
            "bars": bars,
            "bars_h1": bars_h1,
        }
    return out


def push(settings: dict, payload: dict) -> dict:
    req = urllib.request.Request(settings["server"] + "/api/ingest", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-Bridge-Token": settings["token"]},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> None:
    once = "--once" in sys.argv
    settings = load_settings()
    connect(settings)
    symbols = resolve_symbols(settings)
    log(f"bridge {VERSION}: {len(symbols)} symbols -> {settings['server']}  ({', '.join(f'{k}={v}' for k, v in symbols.items())})")
    clock, need, history, fails = Clock(), set(), 1500, 0  # the server says what history it needs
    need_h1, h1_history, h1_sent = set(), 8000, {}  # h1_sent: when each symbol's hourly history last went up
    while True:
        started = time.time()
        try:
            if mt5.terminal_info() is None:
                log("MT5 connection lost; reconnecting")
                connect(settings)
                symbols = resolve_symbols(settings)
            backfill = set(sorted(need)[:HISTORY_PER_PUSH])
            due = sorted(s for s in need_h1 if started - h1_sent.get(s, 0) > 600)  # a short history is not re-sent every push
            backfill_h1 = set() if backfill else set(due[:H1_HISTORY_PER_PUSH])
            for s in backfill_h1:
                h1_sent[s] = started
            payload = build_payload(symbols, clock, backfill, history, backfill_h1, h1_history)
            res = push(settings, payload)
            need = set(res.get("need_history") or []) & set(symbols)
            history = int(res.get("history_bars") or history)
            need_h1 = set(res.get("need_h1") or []) & set(symbols)
            h1_history = int(res.get("h1_history_bars") or h1_history)
            for r in res.get("rejected") or []:
                log(f"server rejected: {r}")
            with open(ALIVE_FILE, "w", encoding="utf-8") as fh:
                fh.write(str(time.time()))
            if fails or once or backfill or backfill_h1:
                log(f"pushed {len(payload['symbols'])} symbols ok" + (" (connection back)" if fails else "")
                    + (f", history loaded for {', '.join(sorted(backfill))}" if backfill else "")
                    + (f", hourly history loaded for {', '.join(sorted(backfill_h1))}" if backfill_h1 else ""))
            fails = 0
        except urllib.error.HTTPError as e:
            fails += 1
            msg = "wrong token: copy it again from the server setup output" if e.code == 401 else f"HTTP {e.code}"
            log(f"push failed: {msg}")
        except Exception as e:  # network down, server restarting, MT5 hiccup: keep going
            fails += 1
            if fails in (1, 6) or fails % 60 == 0:
                log(f"push failed ({fails}x): {e}")
        if once:
            sys.exit(0 if fails == 0 else 1)
        time.sleep(max(1.0, settings.get("interval_s", 10) - (time.time() - started)))


if __name__ == "__main__":
    main()
