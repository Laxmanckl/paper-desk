"""H4: gold after a shock day: trade the breakout of the quiet box that forms.

    shock    a day whose close-to-close move is at least 3 x ATR(20)
             (ATR measured before that day)
    box      at least 5 closes since the shock, all inside a band no wider than
             2.5 x that ATR (the market goes quiet after the shock)
    entry    the first close beyond the box by 0.25 ATR: BUY above, SELL below
             (both directions; the research memo also suggested a shock-direction-only
             version, which would be a second attempt)
    exits    stop at the opposite edge of the box, capped at 2 ATR; target 2 x the
             stop distance; out after 15 days

From the Price Analyst's memo (research/price-2026-10-10.md). The Skeptic's warning:
probably under 30 set-ups in the history, so the result may only be "unknown".
"""

from __future__ import annotations

from ..fx import Bar, atr
from .base import DailyStrategy


class ShockBox(DailyStrategy):
    shock_atr: float = 3.0
    box_min: int = 5
    box_width_atr: float = 2.5
    clear_atr: float = 0.25
    look_back: int = 40

    def _box(self, history):
        """(box_high, box_low, atr_at_shock) if a valid box sits behind today's bar."""
        n = len(history)
        if n < 30:
            return None
        for s in range(n - 1 - self.box_min, max(21, n - self.look_back) - 1, -1):    # newest shock first
            a = atr(history, s - 1, 20)
            if a <= 0 or abs(history[s].close - history[s - 1].close) < self.shock_atr * a:
                continue
            closes = [b.close for b in history[s + 1:n - 1]]       # since the shock, up to yesterday
            if len(closes) < self.box_min:
                return None
            hi, lo = max(closes), min(closes)
            return (hi, lo, a) if hi - lo <= self.box_width_atr * a else None
        return None

    def signal(self, history):
        bx = self._box(history)
        if not bx:
            return None
        hi, lo, a = bx
        c = history[-1].close
        if c > hi + self.clear_atr * a:
            return "BUY"
        if c < lo - self.clear_atr * a:
            return "SELL"
        return None

    def levels(self, history, side, entry, atr_now):
        bx = self._box(history)
        if not bx:
            return None
        hi, lo, a = bx
        stop = (entry - lo) if side == "BUY" else (hi - entry)
        stop = min(max(stop, 0.25 * a), 2.0 * a)
        return stop, 2.0 * stop

    def conditions(self, history):
        bx = self._box(history)
        return {"box_high": bx[0], "box_low": bx[1], "atr_at_shock": round(bx[2], 4)} if bx else {}

    def explain(self, history):
        bx = self._box(history)
        if not bx:
            return "no quiet box after a recent shock day"
        return f"box {bx[1]:g}-{bx[0]:g} after a shock; close {history[-1].close:g}"

    def tests(self):
        calm = [Bar(f"d{i:03d}", 100, 100.6, 99.4, 100 + (0.2 if i % 2 else -0.2)) for i in range(40)]
        shock = calm + [Bar("d040", 100, 104, 99.8, 103.8)]                     # ~+3.8 vs ATR ~1.2
        box = shock + [Bar(f"d{41 + i:03d}", 103.8, 104.3, 103.2, 103.8 + (0.3 if i % 2 else -0.3)) for i in range(6)]
        up = box + [Bar("d047", 104, 105.5, 103.9, 105.4)]
        down = box + [Bar("d047", 103.5, 103.6, 102.0, 102.1)]
        inside = box + [Bar("d047", 103.8, 104.2, 103.5, 103.9)]
        early = shock + box[41:43] + [Bar("d043", 104, 105.5, 103.9, 105.4)]   # only 2 box days
        return [("a close above the box after a shock buys", up, "BUY"),
                ("a close below the box sells", down, "SELL"),
                ("inside the box does nothing", inside, None),
                ("too few quiet days does nothing", early, None),
                ("no shock, no trade", calm, None)]


STRATEGY = ShockBox(
    name="shock_box", version=1,
    hypothesis="After a 3-ATR shock day in gold, the first break out of the quiet box that follows keeps going",
    markets=("XAUUSD",), risk_pct=0.01, sl_atr=2.0, tp_atr=4.0, max_hold=15, rare=True)
