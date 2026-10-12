"""H17: gold's Asia-hours drift (Price Analyst, research/price-2026-10-12.md).

    entry    BUY at the open of the 2nd bar of each trading day. The day's first bar is
             the first bar after gold's daily pause (or the weekend); found from gaps in
             the bar times, so it works whatever time zone the broker's server uses.
             On US-hours brokers that is ~18:00 New York, the start of Asian trading.
    exits    out after 8 hours (~03:00 New York, around the London open); a symmetric
             2 ATR stop and target, so the time exit decides nearly every trade

Edge claimed: Asian physical buying in Asian hours, Western selling in London/NY hours.
Chance for a symmetric stop/target: 50%. Best case expected ~55%.
"""

from __future__ import annotations

from ..fx import Bar
from ._clock import gap_hours
from .base import DailyStrategy


class GoldAsia(DailyStrategy):
    def signal(self, history):
        if len(history) < 20:
            return None
        g = gap_hours(history[-2], history[-1])
        return "BUY" if g is not None and g >= 2 else None

    def explain(self, history):
        return "first bar after gold's daily pause: Asia hours start" if self.signal(history) else "mid-session"

    def tests(self):
        day = [Bar(f"2026-01-06 {h:02d}:00", 4200, 4205, 4195, 4200) for h in range(0, 23)]   # 23:00 missing
        nxt = day + [Bar("2026-01-07 00:00", 4200, 4205, 4195, 4201)]
        return [("the first bar after the daily pause buys", nxt, "BUY"),
                ("an ordinary hour does nothing", day, None),
                ("the first bar after the weekend buys", day + [Bar("2026-01-11 23:00", 4200, 4205, 4195, 4202)], "BUY")]


STRATEGY = GoldAsia(
    name="gold_asia", version=1, timeframe="H1", warmup=30, history_bars=40, needs_real_dates=True,
    hypothesis="Gold rises during Asian hours: long from the day's 2nd bar for 8 hours wins more than 50%",
    markets=("XAUUSD",), risk_pct=0.01, sl_atr=2.0, tp_atr=2.0, max_hold=7, max_leverage=10.0)   # 7 more bars = 8 hours
