"""H16: weekend gap, partial fill (Price Analyst, research/price-2026-10-12.md).

    setup    the bar that just closed is the week's first (more than 40 hours after the
             bar before it), and its close is more than 0.5 ATR(14) away from Friday's
             last close
    entry    trade back toward Friday's close at the next bar's open
    exits    target half the gap, stop 1.5x the gap, out after 24 bars
    costs    entry charged 3x the normal spread (the Skeptic: reopen spreads are wide)

Edge claimed: the reopen trades in the week's thinnest liquidity, so jumps overshoot
and part comes back when Tokyo and London arrive. Chance for this shape: 75%.
"""

from __future__ import annotations

from ..fx import Bar, atr
from ._clock import gap_hours
from .base import DailyStrategy


class WeekendGap(DailyStrategy):
    min_gap_atr: float = 0.5

    def _gap(self, history):
        """(gap = first-bar close - Friday close, ATR) if the last bar opened the week."""
        if len(history) < 20:
            return None
        g = gap_hours(history[-2], history[-1])
        if g is None or g < 40:
            return None
        a = atr(history, len(history) - 2)            # the ATR going into the weekend
        gap = history[-1].close - history[-2].close
        return (gap, a) if a > 0 else None

    def signal(self, history):
        x = self._gap(history)
        if not x or abs(x[0]) <= self.min_gap_atr * x[1]:
            return None
        return "SELL" if x[0] > 0 else "BUY"

    def levels(self, history, side, entry, atr_now):
        gap = abs(history[-1].close - history[-2].close)
        return (1.5 * gap, 0.5 * gap) if gap > 0 else None

    def conditions(self, history):
        x = self._gap(history)
        return {"gap_atr": round(x[0] / x[1], 2)} if x else {}

    def explain(self, history):
        x = self._gap(history)
        return f"weekly open gap {x[0] / x[1]:+.2f} ATR" if x else "not the week's first bar"

    def tests(self):
        def week(fri_close, mon_close):
            out = [Bar(f"2026-01-09 {h:02d}:00", 1.10, 1.1006, 1.0994, 1.10) for h in range(20)]   # Friday
            out.append(Bar("2026-01-12 00:00", fri_close, max(fri_close, mon_close) + 0.0002,
                           min(fri_close, mon_close) - 0.0002, mon_close))                          # Monday open
            return out
        mid = [Bar(f"2026-01-09 {h:02d}:00", 1.10, 1.1006, 1.0994, 1.10) for h in range(21)]
        return [("a gap up of several ATR at the weekly open sells", week(1.10, 1.1040), "SELL"),
                ("a gap down buys", week(1.10, 1.0960), "BUY"),
                ("a tiny gap does nothing", week(1.10, 1.1002), None),
                ("an ordinary hour is not a weekly open", mid, None)]


STRATEGY = WeekendGap(
    name="weekend_gap", version=1, timeframe="H1", warmup=30, history_bars=40, rare=True,
    needs_real_dates=True, entry_spread_mult=3.0,
    hypothesis="A weekend gap bigger than 0.5 ATR at the weekly open gives back half of itself before "
               "it grows by 1.5x, more often than the 75% chance rate",
    markets=("XAUUSD", "EURUSD"), risk_pct=0.01, sl_atr=1.5, tp_atr=0.5, max_hold=24, max_leverage=10.0)
