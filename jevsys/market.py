"""Holds the prices the Windows MT5 bridge sends: latest quotes and closed M5 bars.

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


class SymbolData:
    def __init__(self, keep_bars: int):
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
            self.rejected += len(rejected)
            need = [s for s, sd in self.data.items() if len(sd.bars) < 300]
        return {"ok": True, "need_history": need, "history_bars": HISTORY_WANTED, "rejected": rejected[:5]}
