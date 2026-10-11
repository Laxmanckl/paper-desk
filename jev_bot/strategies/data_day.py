"""H7: big moves on US inflation (CPI) and jobs (NFP) report days.

    event    the daily bar that closed is a CPI or NFP release day (us_releases.py)
    filter   that day's close-to-close move is at least 1.0 x ATR(14), measured with
             the ATR as it stood BEFORE the release day (a real surprise, not noise)
    entry    next day: in the SAME direction as the release-day move ("follow", v1)
             or AGAINST it ("fade", the variant the research memo asked to test)
    exits    stop 1.0 x ATR, target 1.5 x ATR, out after 5 trading days

Break-even before costs: 40% wins (a win is 1.5 x a loss). From the Macro Watcher's
memo (research/macro-2026-10-10.md); the Skeptic's conditions: it must hold in both
halves of the history and in both markets, and both directions count as attempts.
"""

from __future__ import annotations

from ..fx import Bar, atr
from . import us_releases
from .base import DailyStrategy


class DataDay(DailyStrategy):
    mode: str = "follow"             # "follow" or "fade"
    min_move_atr: float = 1.0

    def _event(self, history):
        """(kind, move, atr_before) for the bar that just closed, or None."""
        if len(history) < 17:
            return None
        day = history[-1].date
        if day not in us_releases.ALL:
            return None
        a = atr(history, len(history) - 2)            # ATR as it stood before the release day
        move = history[-1].close - history[-2].close
        return us_releases.KIND.get(day, "?"), move, a

    def signal(self, history):
        ev = self._event(history)
        if not ev:
            return None
        _, move, a = ev
        if a <= 0 or abs(move) < self.min_move_atr * a:
            return None
        up = move > 0
        if self.mode == "fade":
            up = not up
        return "BUY" if up else "SELL"

    def conditions(self, history):
        ev = self._event(history)
        if not ev:
            return {}
        kind, move, a = ev
        return {"release": kind, "move_atr": round(move / a, 2) if a else None, "mode": self.mode}

    def explain(self, history):
        ev = self._event(history)
        if not ev:
            return "not a CPI or jobs-report day"
        kind, move, a = ev
        return (f"{kind} day: close-to-close move {move / a:+.2f} ATR (needs {self.min_move_atr:g}); "
                f"{self.mode} mode")

    def tests(self):
        base = [Bar(f"2024-0{1 + i // 28}-{1 + i % 28:02d}", 100, 101, 99, 100 + (0.2 if i % 2 else -0.2))
                for i in range(40)]
        quiet = base[:-1]
        def with_day(date, close):
            prev = quiet[-1]
            return quiet + [Bar(date, prev.close, max(prev.close, close) + 0.2, min(prev.close, close) - 0.2, close)]
        big_up = with_day("2024-02-13", 103.0)        # a CPI day, move far above 1 ATR (~2)
        big_down = with_day("2024-03-08", 97.0)       # an NFP day
        small = with_day("2024-02-13", 100.5)         # CPI day, small move
        plain = with_day("2024-02-14", 103.0)         # big move, but not a release day
        follow = [("a big up move on CPI day follows: BUY", big_up, "BUY"),
                  ("a big down move on NFP day follows: SELL", big_down, "SELL"),
                  ("a small move on CPI day does nothing", small, None),
                  ("a big move on an ordinary day does nothing", plain, None)]
        if self.mode == "fade":
            flip = {"BUY": "SELL", "SELL": "BUY", None: None}
            return [(d.replace("follows", "fades"), h, flip[w]) for d, h, w in follow]
        return follow


# v1 "follow" (2026-10-11): -0.49% on 55 trades, 47% wins, DSR 0.46: failed.
# v2 "fade": the reverse direction the research memo asked to test alongside it.
STRATEGY = DataDay(
    name="data_day", version=2,
    hypothesis="After a CPI or jobs-report day that moves at least 1 ATR, the move reverses over the next 5 days",
    markets=("XAUUSD", "EURUSD"), risk_pct=0.01, sl_atr=1.0, tp_atr=1.5, max_hold=5,
    needs_real_dates=True)
STRATEGY.mode = "fade"
