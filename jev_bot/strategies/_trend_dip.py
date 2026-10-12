"""Shared rules for H14 (1-hour) and H15 (4-hour): buy short, sharp dips inside an
established uptrend (mirror: sell sharp pops in a downtrend), take a SMALL profit.

    trend (on the bar that just closed)
        up    50-bar average above the 200-bar average, and the close above the 200
        down  the mirror
    trigger
        RSI(2) below 10 in an uptrend: BUY at the next bar's open
        RSI(2) above 90 in a downtrend: SELL
    exits
        stop 2.0 x ATR(14), target 0.8 x ATR (0.4 x the stop), out after 24 bars
        (1 day on 1-hour bars, 4 days on 4-hour bars)

Why a small target: the win rate comes mostly from where the target sits against the
stop. With the target at 0.4 x the stop, breaking even needs ~71% wins before costs;
the 85% goal is written into the prediction. One loss costs ~2.5 wins, so the signal
has to be genuinely good. The honest test decides.
"""

from __future__ import annotations

from ..fx import Bar
from .base import DailyStrategy


def rsi2(closes: list[float]) -> float:
    """Wilder RSI(2) over the closes given (seeded on the first two changes)."""
    ch = [b - a for a, b in zip(closes, closes[1:])]
    if len(ch) < 2:
        return 50.0
    up = sum(max(c, 0) for c in ch[:2]) / 2
    dn = sum(max(-c, 0) for c in ch[:2]) / 2
    for c in ch[2:]:
        up = (up * 1 + max(c, 0)) / 2
        dn = (dn * 1 + max(-c, 0)) / 2
    if dn == 0:
        return 100.0 if up > 0 else 50.0
    return 100 - 100 / (1 + up / dn)


class TrendDip(DailyStrategy):
    fast: int = 50
    slow: int = 200
    low: float = 10.0
    high: float = 90.0

    def _state(self, hist):
        if len(hist) < self.slow + 1:
            return None
        c = [b.close for b in hist]
        f = sum(c[-self.fast:]) / self.fast
        s = sum(c[-self.slow:]) / self.slow
        trend = "up" if f > s and c[-1] > s else "down" if f < s and c[-1] < s else "none"
        return {"trend": trend, "rsi2": round(rsi2(c[-30:]), 1), "sma50": f, "sma200": s}

    def signal(self, history):
        st = self._state(history)
        if not st:
            return None
        if st["trend"] == "up" and st["rsi2"] < self.low:
            return "BUY"
        if st["trend"] == "down" and st["rsi2"] > self.high:
            return "SELL"
        return None

    def conditions(self, history):
        st = self._state(history)
        return {"trend": st["trend"], "rsi2": st["rsi2"]} if st else {}

    def explain(self, history):
        st = self._state(history)
        if not st:
            return "not enough history yet"
        return f"{st['trend']} trend, RSI(2) {st['rsi2']:.0f}"

    def tests(self):
        def bars(closes):
            return [Bar(f"2026-01-01 {i:05d}", c, c + 0.0005, c - 0.0005, c) for i, c in enumerate(closes)]
        rise = [1.0 + 0.001 * i for i in range(220)]
        dip = rise + [rise[-1] - 0.004, rise[-1] - 0.010]              # two sharp down bars in an uptrend
        pop = rise + [rise[-1] + 0.001]
        fall = [2.5 - c for c in rise]
        rally = fall + [fall[-1] + 0.004, fall[-1] + 0.010]
        fall_dip = fall + [fall[-1] - 0.004, fall[-1] - 0.010]
        return [("a sharp dip inside an uptrend buys", bars(dip), "BUY"),
                ("an uptrend that keeps rising does nothing", bars(pop), None),
                ("a sharp pop inside a downtrend sells", bars(rally), "SELL"),
                ("a dip inside a downtrend is not bought", bars(fall_dip), None),
                ("too little history does nothing", bars(dip[-100:]), None)]
