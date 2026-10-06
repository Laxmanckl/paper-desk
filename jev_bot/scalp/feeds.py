"""Live prices for the scalper.

  BinanceFeed   crypto, from Binance's public market-data stream
                (data-stream.binance.vision): no account, no API key.
                Best bid/ask on every change, pushed to us.
  MT5Feed       forex + gold from your MetaTrader 5 terminal (Windows only:
                the MetaTrader5 Python package talks to a running, logged-in
                MT5 terminal on the same machine). Polled twice a second.
  Clock         every second, re-feeds each instrument's latest price so
                minute candles close and time-stops fire even in quiet moments.
                Instruments with no real price for 2 minutes (market closed,
                feed down) are skipped, so weekends don't create fake candles.

All of them call engine.on_tick(symbol, bid, ask, unix_time).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.request import Request, urlopen

from .bars import Bar

STALE_AFTER = 120          # seconds without a real price -> treat as closed / down


def _now() -> float:
    return time.time()


class Status:
    """Shared per-source status for the dashboard."""

    def __init__(self, book, source: str):
        self.book, self.source = book, source

    def set(self, status: str, detail: str = "") -> None:
        prev = self.book.feeds.get(self.source, {})
        if prev.get("status") != status or prev.get("detail") != detail[:200]:
            print(f"[{self.source}] {status}" + (f": {detail}" if detail else ""), flush=True)   # goes to the log
        self.book.feeds[self.source] = {"status": status, "detail": detail[:200], "t": _now()}


# --- Binance (crypto) -----------------------------------------------------------

BINANCE_REST = "https://data-api.binance.vision/api/v3/klines"
BINANCE_WS = "wss://data-stream.binance.vision/stream?streams="


def binance_history(symbol: str, limit: int = 120) -> list[Bar]:
    """Recent closed 1-minute candles to warm the indicators up."""
    url = f"{BINANCE_REST}?symbol={symbol}&interval=1m&limit={limit + 1}"
    with urlopen(Request(url, headers={"User-Agent": "paper-desk"}), timeout=15) as r:
        rows = json.loads(r.read().decode())
    bars = [Bar(int(k[0] // 1000), float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in rows]
    return bars[:-1]                       # the last one is still forming


def parse_binance(msg: str):
    """(symbol, bid, ask) from a combined-stream bookTicker message, else None."""
    try:
        d = json.loads(msg)
        d = d.get("data", d)
        return d["s"], float(d["b"]), float(d["a"])
    except (ValueError, KeyError, TypeError):
        return None


class BinanceFeed:
    def __init__(self, engine, symbols: list[str], url: str = BINANCE_WS, history=binance_history):
        self.engine, self.symbols = engine, symbols
        self.url = url + "/".join(f"{s.lower()}@bookTicker" for s in symbols)
        self.history = history
        self.status = Status(engine.book, "binance")
        self.last_real: dict = {}

    def warm_up(self) -> None:
        for s in self.symbols:
            try:
                self.engine.seed(s, self.history(s))
            except Exception as e:
                self.engine.book.signals[s] = {"reason": f"history unavailable ({type(e).__name__}); "
                                                         "warming up from live prices"}

    async def run(self) -> None:
        import websockets                                  # pip/apt package, only needed live
        await asyncio.get_running_loop().run_in_executor(None, self.warm_up)
        backoff = 1
        while True:
            try:
                self.status.set("connecting")
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20,
                                              max_size=2 ** 20) as ws:
                    self.status.set("live")
                    backoff = 1
                    async for msg in ws:
                        p = parse_binance(msg)
                        if p and p[0] in self.engine.builders:
                            t = _now()
                            self.last_real[p[0]] = t
                            self.engine.on_tick(p[0], p[1], p[2], t)
            except asyncio.CancelledError:
                raise
            except Exception as e:                         # dropped, refused, DNS, ...
                self.status.set("reconnecting", f"{type(e).__name__}: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


# --- MetaTrader 5 (forex + gold) -----------------------------------------------------

ALIASES = {"XAUUSD": ["XAUUSD", "GOLD"], "XAGUSD": ["XAGUSD", "SILVER"]}


def resolve_mt5_symbols(wanted: list[str], available: list[str], overrides: dict) -> dict:
    """Map our names to the broker's (brokers add suffixes: EURUSD.a, EURUSDm, GOLD#)."""
    out = {}
    for w in wanted:
        if w in overrides:
            out[w] = overrides[w]
            continue
        for stem in ALIASES.get(w, [w]):              # first alias that the broker has wins
            exact = [a for a in available if a.upper() == stem]
            close = [a for a in available if a.upper().startswith(stem) and len(a) <= len(stem) + 4]
            if exact or close:
                out[w] = (exact or sorted(close, key=len))[0]
                break
    return out


def mt5_overrides() -> dict:
    """MT5_SYMBOLS="XAUUSD=GOLD.r,EURUSD=EURUSD.r" if auto-matching picks wrong."""
    raw = os.environ.get("MT5_SYMBOLS", "")
    return dict(p.split("=", 1) for p in raw.split(",") if "=" in p)


class MT5Feed:
    POLL = 0.5

    def __init__(self, engine, symbols: list[str], mt5=None):
        self.engine, self.symbols = engine, symbols
        self.mt5 = mt5
        self.status = Status(engine.book, "mt5")
        self.map: dict = {}
        self.last_real: dict = {}
        self._last_msc: dict = {}

    def connect(self) -> None:
        if self.mt5 is None:
            import MetaTrader5 as mt5                      # Windows only
            self.mt5 = mt5
        kw = {"timeout": 30_000}                           # ms; fail with a reason instead of hanging
        if os.environ.get("MT5_PATH"):
            kw["path"] = os.environ["MT5_PATH"]
        if os.environ.get("MT5_LOGIN"):
            kw.update(login=int(os.environ["MT5_LOGIN"]), password=os.environ.get("MT5_PASSWORD", ""),
                      server=os.environ.get("MT5_SERVER", ""))
        if not self.mt5.initialize(**kw):
            err = self.mt5.last_error()
            hint = ""
            if err and err[0] in (-10003, -10005, -10004):     # IPC init / timeout / no connection
                hint = (" — is MT5 open and logged in, with Tools > Options > Expert Advisors > "
                        "'Allow algorithmic trading' ticked?")
            raise RuntimeError(f"MT5 initialize failed: {err}{hint}")
        info = self.mt5.account_info()
        if info is not None:
            print(f"[mt5] connected to account {info.login} on {info.server}", flush=True)
        names = [s.name for s in (self.mt5.symbols_get() or [])]
        self.map = resolve_mt5_symbols(self.symbols, names, mt5_overrides())
        missing = [s for s in self.symbols if s not in self.map]
        for ours, theirs in self.map.items():
            self.mt5.symbol_select(theirs, True)
            rates = self.mt5.copy_rates_from_pos(theirs, self.mt5.TIMEFRAME_M1, 1, 120)
            if rates is not None and len(rates):
                # broker bar times are in the broker's own time zone; only the order
                # matters for indicators, so re-stamp them on our UTC minute grid
                m = int(_now() // 60) * 60
                n = len(rates)
                self.engine.seed(ours, [Bar(m - (n - i) * 60, float(r["open"]), float(r["high"]),
                                            float(r["low"]), float(r["close"])) for i, r in enumerate(rates)])
        self.status.set("live", ("not offered by your broker: " + ", ".join(missing)) if missing else "")

    def poll_once(self) -> None:
        t = _now()
        for ours, theirs in self.map.items():
            tick = self.mt5.symbol_info_tick(theirs)
            if tick is None or not tick.bid or not tick.ask:
                continue
            msc = getattr(tick, "time_msc", None) or tick.time
            if self._last_msc.get(ours) == msc:            # nothing new (or market closed)
                continue
            self._last_msc[ours] = msc
            self.last_real[ours] = t
            self.engine.on_tick(ours, float(tick.bid), float(tick.ask), t)

    async def run(self) -> None:
        backoff = 5
        while True:
            try:
                self.status.set("connecting")
                await asyncio.get_running_loop().run_in_executor(None, self.connect)
                backoff = 5
                while True:
                    self.poll_once()
                    await asyncio.sleep(self.POLL)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status.set("reconnecting", f"{type(e).__name__}: {e}")
                try:
                    self.mt5 and self.mt5.shutdown()
                except Exception:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)


# --- the 1-second clock ----------------------------------------------------------------

async def clock(engine, feeds: list) -> None:
    """Every second, re-send each live instrument's latest price at the current time."""
    while True:
        await asyncio.sleep(1)
        tick_clock(engine, feeds, _now())


def tick_clock(engine, feeds: list, t: float) -> None:
    for f in feeds:
        for sym, last in list(f.last_real.items()):
            px = engine.book.prices.get(sym)
            if px and t - last <= STALE_AFTER:
                engine.on_tick(sym, px["bid"], px["ask"], t)
