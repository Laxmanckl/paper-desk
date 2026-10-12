"""H18: month-end dollar rebalancing (Macro Watcher, research/macro-2026-10-12.md).

    setup    the close of the 3rd-to-last weekday of the month. The S&P 500 (the
             broker's US500, data/mt5/US500_D1.csv) is up more than 3% on the month
             so far: foreign holders of US stocks must sell dollars to re-hedge.
             Down more than 3%: they must buy dollars.
    entry    next day's open: sell USD (BUY EURUSD / GBPUSD, SELL USDJPY), or the mirror
    exits    the month's last daily close (1 bar later); a symmetric 1.5 ATR stop and
             target, so the time exit decides most trades. Chance: 50%.

The S&P figure uses only US500 closes dated on or before the signal day (the index
closes about an hour before the forex daily bar), so nothing from the future is used.
Hand-made test histories are dated 1901, with their own small S&P series below.
"""

from __future__ import annotations

import calendar
import copy
from bisect import bisect_left, bisect_right
from datetime import date, datetime
from pathlib import Path

from ..fx import Bar, load_csv
from .base import DailyStrategy

SPX_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "mt5" / "US500_D1.csv"
_cache: dict = {}

# test-only S&P closes (1901 dates never occur in real data)
TEST_SPX = {"1901-03-29": 100.0, "1901-04-01": 100.0, "1901-04-25": 105.0, "1901-05-29": 96.0}


def spx_series() -> tuple[list, list]:
    if "spx" not in _cache:
        if SPX_FILE.exists():
            bars = load_csv(str(SPX_FILE))
            _cache["spx"] = ([b.date[:10] for b in bars], [b.close for b in bars])
        else:
            _cache["spx"] = ([], [])
    return _cache["spx"]


def weekdays_left(d: date) -> int:
    """Weekdays after d in d's month."""
    last = calendar.monthrange(d.year, d.month)[1]
    return sum(1 for day in range(d.day + 1, last + 1) if date(d.year, d.month, day).weekday() < 5)


class MonthEndFlow(DailyStrategy):
    move: float = 0.03
    symbol: str = ""

    def for_symbol(self, symbol):
        c = copy.copy(self)
        c.symbol = symbol.upper()
        return c

    def _month_move(self, history):
        """S&P change this month up to the bar that just closed, on the right day; else None."""
        try:
            d = datetime.strptime(history[-1].date[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
        if d.weekday() >= 5 or weekdays_left(d) != 2:
            return None
        if d.year < 1950:
            dates = sorted(TEST_SPX)
            closes = [TEST_SPX[x] for x in dates]
        else:
            dates, closes = spx_series()
        today = d.isoformat()
        i = bisect_right(dates, today) - 1                        # last S&P close on or before today
        j = bisect_left(dates, f"{d.year:04d}-{d.month:02d}-01") - 1   # last close of the previous month
        if i < 0 or j < 0 or i <= j:
            return None
        if (d - datetime.strptime(dates[i], "%Y-%m-%d").date()).days > 4:
            return None                                           # S&P data missing around today
        return closes[i] / closes[j] - 1

    def signal(self, history):
        m = self._month_move(history)
        if m is None or abs(m) <= self.move:
            return None
        sell_usd = m > 0
        usd_first = self.symbol.startswith("USD")
        if sell_usd:
            return "SELL" if usd_first else "BUY"
        return "BUY" if usd_first else "SELL"

    def conditions(self, history):
        m = self._month_move(history)
        return {"spx_month_pct": round(m * 100, 2)} if m is not None else {}

    def explain(self, history):
        m = self._month_move(history)
        if m is None:
            return "not the 3rd-to-last weekday of the month (or no S&P data)"
        return f"S&P {m * 100:+.1f}% on the month: " + ("re-hedging sells USD" if m > self.move else
                                                        "re-hedging buys USD" if m < -self.move else "too small")

    def tests(self):
        def days(month, upto):
            out, n = [], 0
            for day in range(1, upto + 1):
                if date(1901, month, day).weekday() < 5:
                    out.append(Bar(f"1901-{month:02d}-{day:02d}", 1.10, 1.102, 1.098, 1.10))
            return out
        march = days(3, 29)
        # April 1901: the 30th is a Tuesday, so the 3rd-to-last weekday is Friday the 26th
        april_signal = march + days(4, 26)
        may_signal = march + days(4, 30) + days(5, 29)            # May 31 is a Friday: the 29th is 3rd-to-last
        return [("S&P +5% on the month: sell USD, i.e. buy EURUSD", april_signal, "BUY"),
                ("S&P -9% on the month: buy USD, i.e. sell EURUSD", may_signal, "SELL"),
                ("not the 3rd-to-last weekday: nothing", april_signal[:-1], None),
                ("ordinary days do nothing", march, None)]


STRATEGY = MonthEndFlow(
    name="month_end_flow", version=1, needs_real_dates=True, data_files=("US500_D1.csv",),
    hypothesis="When the S&P 500 is up (down) more than 3% on the month, month-end hedge rebalancing "
               "sells (buys) USD over the last two trading days",
    markets=("EURUSD",), risk_pct=0.01, sl_atr=1.5, tp_atr=1.5, max_hold=1, max_leverage=10.0)
