"""Holds the prices the Windows MT5 bridge sends: latest quotes, closed M5 bars and
closed H1 bars (the hourly bars feed the volume breakout account, bridge 1.2+).

Lookahead guard, part 1: nothing stamped in the future is accepted. A bar
counts only once it has closed, and quotes from more than 10 s ahead of the
server clock (a broken bridge clock) are rejected.
"""
from __future__ import annotations

import bisect
import threading
from collections import deque

from .instruments import INSTRUMENTS
from .types import BAR_SECONDS, Bar, Quote

CLOCK_SKEW_S = 10.0
HISTORY_WANTED = 1_500  # bars the bridge should backfill on first contact (~5 days)
H1_SECONDS = 3600
H1_KEEP = 9_000          # ~1.4 years of hourly bars: daily EMA50, 20-day volume and ATR averages
H1_HISTORY_WANTED = 8_000
H1_MIN_BARS = 2_000      # below this the bridge is asked for the hourly history


class Series:
    """Closed bars of one timeframe for one symbol, sorted by open time."""

    def __init__(self, keep: int, bar_seconds: int):
        self.keep, self.bar_seconds = keep, bar_seconds
        self.bar_ts: list[float] = []
        self.bars: list[Bar] = []

    def add(self, b: Bar) -> None:
        i = bisect.bisect_left(self.bar_ts, b.ts)
        if i < len(self.bar_ts) and self.bar_ts[i] == b.ts:
            self.bars[i] = b
            return
        self.bar_ts.insert(i, b.ts)
        self.bars.insert(i, b)
        if len(self.bars) > self.keep:
            del self.bar_ts[: len(self.bars) - self.keep]
            del self.bars[: len(self.bars) - self.keep]

    def closed_by(self, t: float) -> list[Bar]:
        return self.bars[:self.count_closed(t)]

    def count_closed(self, t: float) -> int:
        return bisect.bisect_right(self.bar_ts, t - self.bar_seconds)


class SymbolData:
    def __init__(self, keep_bars: int):
        self.h1 = Series(H1_KEEP, H1_SECONDS)
        self.keep = keep_bars
        self.bar_ts: list[float] = []
        self.bars: list[Bar] = []
        self.quotes: deque[Quote] = deque(maxlen=120)
        self.point: float | None = None
        self.broker_symbol: str | None = None

    def add_bar(self, b: Bar) -> None:
        i = bisect.bisect_left(self.bar_ts, b.ts)
        if i < len(self.bar_ts) and self.bar_ts[i] == b.ts:
            self.bars[i] = b  # broker may revise a bar; latest wins
            return
        self.bar_ts.insert(i, b.ts)
        self.bars.insert(i, b)
        if len(self.bars) > self.keep:
            del self.bar_ts[: len(self.bars) - self.keep]
            del self.bars[: len(self.bars) - self.keep]

    def bars_closed_by(self, t: float) -> list[Bar]:
        """Bars whose close time is at or before t."""
        i = bisect.bisect_right(self.bar_ts, t - BAR_SECONDS)
        return self.bars[:i]

    def quote_at(self, t: float) -> Quote | None:
        best = None
        for q in self.quotes:
            if q.ts <= t:
                best = q
            else:
                break
        return best


class MarketStore:
    def __init__(self, keep_bars: int = 2_100):
        self.lock = threading.Lock()
        self.data = {s: SymbolData(keep_bars) for s in INSTRUMENTS}
        self.last_ingest: float | None = None
        self.bridge: dict = {}
        self.rejected = 0

    def ingest(self, payload: dict, now: float) -> dict:
        """Merge one bridge push. Returns which symbols still need history."""
        rejected: list[str] = []
        with self.lock:
            self.last_ingest = now
            self.bridge = {k: payload.get(k) for k in ("broker", "account", "server", "version", "utc_offset_h", "sent")}
            self.bridge["lag_s"] = round(now - payload["sent"], 2) if payload.get("sent") else None
            for sym, d in (payload.get("symbols") or {}).items():
                if sym not in self.data:
                    continue
                sd = self.data[sym]
                sd.point = d.get("point") or sd.point
                sd.broker_symbol = d.get("broker_symbol") or sd.broker_symbol
                q = d.get("quote")
                if q:
                    if q["t"] > now + CLOCK_SKEW_S:
                        rejected.append(f"{sym} quote from the future")
                    elif q["bid"] > 0 and q["ask"] >= q["bid"]:
                        last = sd.quotes[-1].ts if sd.quotes else -1
                        if q["t"] > last:
                            sd.quotes.append(Quote(sym, float(q["t"]), float(q["bid"]), float(q["ask"])))
                for row in d.get("bars") or []:
                    b = Bar(*map(float, row[:7]))
                    if b.close_ts > now + CLOCK_SKEW_S:
                        rejected.append(f"{sym} unclosed bar")
                        continue
                    if not (b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close) and b.low > 0):
                        rejected.append(f"{sym} malformed bar")
                        continue
                    sd.add_bar(b)
                for row in d.get("bars_h1") or []:
                    b = Bar(*map(float, row[:7]))
                    if b.ts + H1_SECONDS > now + CLOCK_SKEW_S:
                        rejected.append(f"{sym} unclosed hourly bar")
                        continue
                    if not (b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close) and b.low > 0):
                        rejected.append(f"{sym} malformed hourly bar")
                        continue
                    sd.h1.add(b)
            self.rejected += len(rejected)
            need = [s for s, sd in self.data.items() if len(sd.bars) < 300]
            need_h1 = [s for s, sd in self.data.items() if len(sd.h1.bars) < H1_MIN_BARS]
        return {"ok": True, "need_history": need, "history_bars": HISTORY_WANTED,
                "need_h1": need_h1, "h1_history_bars": H1_HISTORY_WANTED, "rejected": rejected[:5]}
