"""H5: EUR/USD over-stretched: buy the first sign of a bounce (mirror for rallies).

    stretched (yesterday)  close at least 2.5% below its 50-day average, 20-day change
                           at most -3%, and a 20-day closing low within the last 3 days
    entry (today)          the first close above the previous day's high: BUY
                           (mirror: over-stretched up, first close below the prior low: SELL)
    exits                  stop 1 ATR beyond the stretch low (lowest low of the last 5
                           days); target the 20-day EMA or 2 x the stop, whichever is
                           nearer; out after 10 days

From the Price Analyst's memo (research/price-2026-10-10.md). The Skeptic's warning:
signals bunch into a few strong-dollar episodes, so the sample is small.
"""

from __future__ import annotations

from ..fx import Bar, atr
from .base import DailyStrategy


def _ema(vals, n):
    k, e = 2 / (n + 1), vals[0]
    for v in vals[1:]:
        e = e + k * (v - e)
    return e


class StretchSnap(DailyStrategy):
    stretch: float = 0.025
    move20: float = 0.03

    def _stretched(self, hist):
        """'down', 'up' or None for the state at hist[-1]."""
        if len(hist) < 55:
            return None
        c = [b.close for b in hist]
        sma = sum(c[-50:]) / 50
        ch20 = c[-1] / c[-21] - 1
        recent_low = min(c[-3:]) <= min(c[-20:])
        recent_high = max(c[-3:]) >= max(c[-20:])
        if c[-1] <= sma * (1 - self.stretch) and ch20 <= -self.move20 and recent_low:
            return "down"
        if c[-1] >= sma * (1 + self.stretch) and ch20 >= self.move20 and recent_high:
            return "up"
        return None

    def signal(self, history):
        if len(history) < 56:
            return None
        st = self._stretched(history[:-1])
        today, prev = history[-1], history[-2]
        if st == "down" and today.close > prev.high:
            return "BUY"
        if st == "up" and today.close < prev.low:
            return "SELL"
        return None

    def levels(self, history, side, entry, atr_now):
        a = atr(history, len(history) - 1)
        if a <= 0:
            return None
        ema20 = _ema([b.close for b in history[-60:]], 20)
        if side == "BUY":
            stop = entry - (min(b.low for b in history[-5:]) - a)
            to_ema = ema20 - entry
        else:
            stop = (max(b.high for b in history[-5:]) + a) - entry
            to_ema = entry - ema20
        if stop <= 0:
            return None
        target = min(to_ema, 2 * stop) if to_ema > 0 else 2 * stop
        return stop, max(target, 0.25 * a)

    def conditions(self, history):
        st = self._stretched(history[:-1]) if len(history) > 56 else None
        return {"stretched": st} if st else {}

    def explain(self, history):
        st = self._stretched(history[:-1]) if len(history) > 56 else None
        if not st:
            return "not over-stretched"
        return f"over-stretched {st}; close {history[-1].close:g} vs prior bar {history[-2].low:g}-{history[-2].high:g}"

    def tests(self):
        flat = [Bar(f"d{i:03d}", 1.10, 1.102, 1.098, 1.10) for i in range(40)]
        slide = [Bar(f"d{40 + i:03d}", 1.10 - 0.003 * i, 1.10 - 0.003 * i + 0.001,
                     1.10 - 0.003 * (i + 1) - 0.001, 1.10 - 0.003 * (i + 1)) for i in range(20)]
        base = flat + slide                                     # ~-5.5% in 20 days, far below the average
        last = base[-1]
        bounce = base + [Bar("d060", last.close, last.close + 0.0065, last.close - 0.0005, last.close + 0.006)]
        weak = base + [Bar("d060", last.close, last.close + 0.0008, last.close - 0.002, last.close + 0.0002)]
        ups = [Bar(b.date, 2.2 - b.open, 2.2 - b.low, 2.2 - b.high, 2.2 - b.close) for b in base]
        lu = ups[-1]
        rollover = ups + [Bar("d060", lu.close, lu.close + 0.0005, lu.close - 0.0065, lu.close - 0.006)]
        return [("a close above yesterday's high after a deep slide buys", bounce, "BUY"),
                ("no close above yesterday's high, no trade", weak, None),
                ("the mirror after a stretched rally sells", rollover, "SELL"),
                ("calm markets do nothing", flat + flat[:20], None)]


STRATEGY = StretchSnap(
    name="stretch_snap", version=1,
    hypothesis="When EUR/USD is over-stretched from its 50-day average, the first close beyond the prior day's range starts a snap-back",
    markets=("EURUSD",), risk_pct=0.01, sl_atr=1.5, tp_atr=2.0, max_hold=10)
