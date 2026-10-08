"""Strategy B: the London opening-range breakout.

Per instrument, per day (all times UTC):

  range     the high and low of the 1-minute candles from 06:00 to 07:00
  entry     07:00-12:00: the first 1-minute close above the range high + 10%
            of the range height -> BUY; below the low - 10% -> SELL.
            One trade per instrument per day.
  stop      the middle of the range
  target    1.5 x the stop distance
  exit      at 16:00 UTC if neither stop nor target was hit

The idea: Europe's open brings the day's first big flow of orders, and a clean
break out of the quiet early range often keeps going for a while. Costs are
checked the same way as the pullback scalper (round trip <= 25% of the risk).
"""

from __future__ import annotations

from .bars import Bar, atr

DAY = 86_400


def _hm(x: float) -> str:
    return f"{int(x):02d}:{round(x % 1 * 60):02d}"


def opening_range(bars: list[Bar], day0: int, lo_h: float, hi_h: float) -> tuple | None:
    """(high, low, candles) of the range window on the day starting at day0, or None."""
    a, b = day0 + lo_h * 3600, day0 + hi_h * 3600
    hs, ls = [], []
    for bar in reversed(bars):
        if bar.t < a:
            break
        if bar.t < b:
            hs.append(bar.h)
            ls.append(bar.l)
    if not hs:
        return None
    return max(hs), min(ls), len(hs)


def evaluate(bars: list[Bar], cfg, t: float, st: dict):
    from .strategy import Signal
    if not bars:
        return Signal(None, 0.0, "", None, "waiting for prices")
    now = bars[-1].t + 60                    # the moment the newest candle closed
    day0 = int(now // DAY) * DAY
    h = (now - day0) / 3600
    lo_h, hi_h = cfg.orb_range_utc
    a = atr(bars, 14)
    if h < hi_h:
        if h < lo_h:
            return Signal(None, a, "", None, f"waiting for the {_hm(lo_h)}-{_hm(hi_h)} UTC range")
        return Signal(None, a, "", None, f"measuring the opening range (until {_hm(hi_h)} UTC)")
    rng = st.get("range")
    if not rng or rng[0] != day0:
        r = opening_range(bars, day0, lo_h, hi_h)
        need = int((hi_h - lo_h) * 60 * 0.8)
        if r is None or r[2] < need:
            st["range"] = (day0, None, None)
        else:
            st["range"] = (day0, r[0], r[1])
        rng = st["range"]
    _, top, bot = rng
    if top is None:
        return Signal(None, a, "", None, "no opening range today (missing prices 06-07 UTC)")
    if st.get("traded") == day0:
        return Signal(None, a, "", None, "done for today (one breakout trade per day)")
    if h >= cfg.orb_entry_until:
        return Signal(None, a, "", None, f"breakout window closed at {_hm(cfg.orb_entry_until)} UTC")
    height = top - bot
    if height <= 0:
        return Signal(None, a, "", None, "flat opening range")
    c = bars[-1].c
    mid = (top + bot) / 2
    deadline = day0 + cfg.orb_exit_utc * 3600
    pad = cfg.orb_buffer * height
    if c > top + pad:
        return Signal("BUY", a, "up", None, "broke above the opening range", stop=mid, deadline=deadline)
    if c < bot - pad:
        return Signal("SELL", a, "down", None, "broke below the opening range", stop=mid, deadline=deadline)
    return Signal(None, a, "", None, f"inside the range, waiting for a breakout until {_hm(cfg.orb_entry_until)} UTC")
