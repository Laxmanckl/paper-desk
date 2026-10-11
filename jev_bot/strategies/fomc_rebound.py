"""H8: gold rebounds after a Federal Reserve decision day that follows a sell-off.

    event    the daily bar that closed is an FOMC decision day (fomc_dates.py)
    filter   gold's close the day BEFORE the decision is at least 1% below its close
             5 trading days earlier (a pre-meeting sell-off)
    entry    BUY gold the next day (the research memo said at the decision-day close;
             the backtester enters at the next open, a few hours later)
    exits    stop 1.5 x ATR(14), target 2.5 x ATR(14), out after 10 trading days

Break-even before costs: 37.5% wins. From the Central-Bank Reader's memo
(research/central-2026-10-10.md). The Skeptic's condition: it only counts if it
beats the same set-up on ordinary days, so a pass here is followed by that check.
Few meetings qualify, so expect a small sample.
"""

from __future__ import annotations

from ..fx import Bar
from . import fomc_dates
from .base import DailyStrategy


class FomcRebound(DailyStrategy):
    drop: float = 0.01
    lookback: int = 5

    def _drop(self, history):
        if len(history) < self.lookback + 3 or history[-1].date not in fomc_dates.DECISIONS:
            return None
        before, earlier = history[-2].close, history[-2 - self.lookback].close
        return before / earlier - 1

    def signal(self, history):
        d = self._drop(history)
        return "BUY" if d is not None and d <= -self.drop else None

    def conditions(self, history):
        d = self._drop(history)
        return {"fomc_day": True, "pre_meeting_change_pct": round(d * 100, 2)} if d is not None else {}

    def explain(self, history):
        d = self._drop(history)
        if d is None:
            return "not an FOMC decision day"
        return f"FOMC decision day; gold {d * 100:+.2f}% over the {self.lookback} days before (needs -{self.drop:.0%})"

    def tests(self):
        flat = [Bar(f"2024-01-{i + 1:02d}", 2000, 2005, 1995, 2000) for i in range(25)]
        sold = flat[:-6] + [Bar(f"2024-01-{20 + i:02d}", 2000, 2002, 1970, 2000 - 6 * (i + 1)) for i in range(5)]
        fomc = sold + [Bar("2024-01-31", 1970, 1985, 1960, 1980)]          # a real decision day
        calm = flat + [Bar("2024-01-31", 2000, 2005, 1995, 2001)]
        other = sold + [Bar("2024-02-01", 1970, 1985, 1960, 1980)]          # not a decision day
        return [("a sell-off into an FOMC day buys gold", fomc, "BUY"),
                ("no sell-off before the meeting, no trade", calm, None),
                ("the same sell-off on an ordinary day, no trade", other, None)]


STRATEGY = FomcRebound(
    name="fomc_rebound", version=1,
    hypothesis="Gold rebounds over the 10 days after an FOMC decision that follows a 1%+ pre-meeting drop",
    markets=("XAUUSD",), risk_pct=0.01, sl_atr=1.5, tp_atr=2.5, max_hold=10, needs_real_dates=True)
