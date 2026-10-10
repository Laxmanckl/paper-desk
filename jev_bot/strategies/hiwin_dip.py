"""High win rate by design: buy short dips inside an established trend, take a
small profit fast, give the trade a wide stop.

    trend    close above its 50-day average AND the average higher than 10 days ago
             (mirror for a downtrend)
    dip      the close is the lowest of the last 3 days (a rally in a downtrend)
    entry    BUY the dip in an uptrend / SELL the rally in a downtrend
    exits    target 0.5 x ATR(14), stop 2.0 x ATR(14), out after 5 days

The arithmetic it lives or dies by: a win is worth 0.25 of a loss, so it needs
to win more than 80% of trades just to break even before costs, and about 85%
to make a little. High win rate is the point of the idea, not a promise of profit.
"""

from __future__ import annotations

from ..fx import Bar, atr
from .base import DailyStrategy


class HiWinDip(DailyStrategy):
    sma: int = 50
    slope_days: int = 10
    dip_days: int = 3

    def _trend(self, history):
        if len(history) < self.sma + self.slope_days + 1:
            return None
        closes = [b.close for b in history]
        now = sum(closes[-self.sma:]) / self.sma
        then = sum(closes[-self.sma - self.slope_days:-self.slope_days]) / self.sma
        if closes[-1] > now and now > then:
            return "up"
        if closes[-1] < now and now < then:
            return "down"
        return "flat"

    def signal(self, history):
        trend = self._trend(history)
        if trend in (None, "flat"):
            return None
        recent = [b.close for b in history[-self.dip_days:]]
        if trend == "up" and history[-1].close <= min(recent):
            return "BUY"
        if trend == "down" and history[-1].close >= max(recent):
            return "SELL"
        return None

    def conditions(self, history):
        t = self._trend(history)
        return {"trend": t, "atr": round(atr(history, len(history) - 1), 5)} if t else {}

    def explain(self, history):
        t = self._trend(history)
        if t in (None, "flat"):
            return "no clear trend"
        return f"{t}trend; close {history[-1].close:g} vs last {self.dip_days} closes"

    def tests(self):
        up = [Bar(f"d{i:03d}", 100 + i * 0.5, 101 + i * 0.5, 99 + i * 0.5, 100 + i * 0.5) for i in range(70)]
        dip = up + [Bar("d070", 134, 134.5, 132, 132.5)]          # a down close inside the uptrend
        down = [Bar(f"d{i:03d}", 200 - i * 0.5, 201 - i * 0.5, 199 - i * 0.5, 200 - i * 0.5) for i in range(70)]
        rally = down + [Bar("d070", 166, 168, 165.5, 167.5)]      # an up close inside the downtrend
        flat = [Bar(f"d{i:03d}", 100, 101, 99, 100 + (0.3 if i % 2 else -0.3)) for i in range(70)]
        return [("a dip in an uptrend buys", dip, "BUY"),
                ("a rally in a downtrend sells", rally, "SELL"),
                ("a new high in an uptrend is not a dip", up, None),
                ("no trend, no trade", flat, None),
                ("too little history does nothing", up[:30], None)]


STRATEGY = HiWinDip(
    name="hiwin_dip", version=1,
    hypothesis="Buying 3-day dips inside a rising 50-day trend, with a 0.5 ATR target and a 2 ATR stop, wins 85%+ of trades",
    markets=("XAUUSD", "EURUSD"), risk_pct=0.01, sl_atr=2.0, tp_atr=0.5, max_hold=5)
