"""TEMPLATE: copy this file to jev_bot/strategies/<name>.py to make a new strategy.

Files starting with "_" are never run. This one is a working example: a 20-day
breakout (buy a close above the highest high of the previous 20 days, sell a
close below the lowest low). It is here to show the shape, not as a suggestion.

    1. hypothesis: one line, the idea from the ledger (H-number)
    2. signal(): entry rules, using `history` only (history[-1] = the bar that just closed)
    3. sl_atr / tp_atr / max_hold: exit rules;  risk_pct: sizing
    4. tests(): small hand-made histories with the signal you expect
    5. python -m jev_bot research check <name>      (must pass before any backtest)
"""

from __future__ import annotations

from ..fx import Bar
from .base import DailyStrategy


class Breakout(DailyStrategy):
    lookback: int = 20

    def _levels(self, history):
        prior = history[-self.lookback - 1:-1]
        return max(b.high for b in prior), min(b.low for b in prior)

    def signal(self, history):
        if len(history) < self.lookback + 1:
            return None
        hi, lo = self._levels(history)
        c = history[-1].close
        if c > hi:
            return "BUY"
        if c < lo:
            return "SELL"
        return None

    def conditions(self, history):
        if len(history) < self.lookback + 1:
            return {}
        hi, lo = self._levels(history)
        return {"range_high": hi, "range_low": lo}

    def explain(self, history):
        if len(history) < self.lookback + 1:
            return "not enough history"
        hi, lo = self._levels(history)
        return f"close {history[-1].close:g} vs {self.lookback}-day range {lo:g}-{hi:g}"

    def tests(self):
        flat = [Bar(f"d{i:03d}", 100, 101, 99, 100) for i in range(25)]
        up = flat + [Bar("d025", 100, 103, 100, 102.5)]
        down = flat + [Bar("d025", 100, 100, 97, 97.5)]
        return [("a close above the 20-day high buys", up, "BUY"),
                ("a close below the 20-day low sells", down, "SELL"),
                ("inside the range does nothing", flat, None),
                ("too little history does nothing", flat[:5], None)]


STRATEGY = Breakout(name="_template", version=1,
                    hypothesis="Daily closes beyond the 20-day range keep going (example only)",
                    sl_atr=2.0, tp_atr=4.0, max_hold=30)
