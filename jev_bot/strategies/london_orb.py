"""H19: London-open range breakout on 1-hour bars (H3's idea, moved off 1-minute bars).

    range    the first hour of London trading (08:00-09:00 London time)
    entry    the first 1-hour close beyond the range by 10% of its height, in the next
             4 hours (until ~13:00 London): BUY above, SELL below; once per day
    exits    stop at the middle of the range, target 1.5x the stop, out after 6 bars
             (~afternoon London). Chance for this shape: 40%.

Edge claimed: London's open is when the day's largest orders arrive; a clean break of
its first hour shows which side they are on.

Time zones: MT5 bars are stamped in the broker's server time. The week opens at 17:00
New York for forex (18:00 for gold), and London opens 10 hours (gold: 9) after that, so
the London-open bar is found from the hour of the week's first bar. No time zone is
assumed. (For a few weeks a year the US and UK change clocks on different dates; those
weeks are an hour off.)
"""

from __future__ import annotations

import copy

from ..fx import Bar
from ._clock import gap_hours, when
from .base import DailyStrategy


class LondonORB(DailyStrategy):
    buffer: float = 0.10
    entry_bars: int = 4
    after_open: int = 10          # hours from the weekly open to London's open (gold: 9)

    def for_symbol(self, symbol):
        c = copy.copy(self)
        c.after_open = 9 if symbol.upper().startswith("XAU") else 10
        return c

    def _london_hour(self, history):
        for i in range(len(history) - 1, 0, -1):
            g = gap_hours(history[i - 1], history[i])
            if g is not None and g >= 40:                      # the week's first bar
                t = when(history[i])
                return (t.hour + self.after_open) % 24 if t else None
        return None

    def _range(self, history):
        """(high, low, bars since the range bar) if the range bar is 1..entry_bars bars back."""
        lh = self._london_hour(history[:-1])
        if lh is None:
            return None
        for k in range(1, self.entry_bars + 1):
            if len(history) < k + 2:
                return None
            rb = history[-1 - k]
            t = when(rb)
            if t and t.hour == lh:
                for i in range(k):                              # no break in trading since
                    g = gap_hours(history[-2 - i], history[-1 - i])
                    if g is None or g != 1:
                        return None
                return rb.high, rb.low, k
        return None

    def _side(self, close, hi, lo):
        pad = self.buffer * (hi - lo)
        return "BUY" if close > hi + pad else "SELL" if close < lo - pad else None

    def signal(self, history):
        if len(history) < 30:
            return None
        r = self._range(history)
        if not r or r[0] <= r[1]:
            return None
        hi, lo, k = r
        side = self._side(history[-1].close, hi, lo)
        if not side:
            return None
        if any(self._side(b.close, hi, lo) for b in history[-k:-1]):      # not the first break today
            return None
        return side

    def levels(self, history, side, entry, atr_now):
        r = self._range(history)
        if not r:
            return None
        mid = (r[0] + r[1]) / 2
        stop = entry - mid if side == "BUY" else mid - entry
        return (stop, 1.5 * stop) if stop > 0 else None

    def explain(self, history):
        r = self._range(history)
        if not r:
            return "outside the London-open window"
        return f"London range {r[1]:g}-{r[0]:g}; close {history[-1].close:g}"

    def tests(self):
        def week(closes_after):
            # Friday, then a Sunday 21:00 weekly open (a UTC-stamped broker): London = 07:00
            out = [Bar(f"2026-01-09 {h:02d}:00", 1.1000, 1.1005, 1.0995, 1.1000) for h in range(0, 21)]
            hrs = [f"2026-01-11 {h:02d}:00" for h in range(21, 24)] + [f"2026-01-12 {h:02d}:00" for h in range(0, 7)]
            out += [Bar(d, 1.1000, 1.1004, 1.0996, 1.1000) for d in hrs]
            out.append(Bar("2026-01-12 07:00", 1.1000, 1.1020, 1.1000, 1.1010))            # the London range bar
            for i, c in enumerate(closes_after):
                out.append(Bar(f"2026-01-12 {8 + i:02d}:00", 1.1010, max(c, 1.1010) + 0.0002,
                               min(c, 1.1010) - 0.0002, c))
            return out
        return [("a close above the London range (+10%) buys", week([1.1025]), "BUY"),
                ("a close below it sells", week([1.0995]), "SELL"),
                ("a close inside the range does nothing", week([1.1012]), None),
                ("only the first break of the day trades", week([1.1025, 1.1030]), None),
                ("too late in the day does nothing", week([1.1010] * 4 + [1.1030]), None)]


STRATEGY = LondonORB(
    name="london_orb", version=1, timeframe="H1", warmup=150, history_bars=160, needs_real_dates=True,
    hypothesis="The first 1-hour close beyond London's opening hour range runs 1.5x the distance to the "
               "range middle before coming back to it, more often than the 40% chance rate",
    markets=("XAUUSD", "EURUSD"), risk_pct=0.01, sl_atr=1.0, tp_atr=1.5, max_hold=6, max_leverage=10.0)
