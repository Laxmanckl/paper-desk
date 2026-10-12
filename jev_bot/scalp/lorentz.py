"""Scalper D: the Lorentzian Classification model on 15-minute candles.

The live engine closes a 1-minute candle every minute; this groups them into
lz_minutes candles (15 by default) and feeds each finished one to the model
(jev_bot/lorentzian.py). A trade starts only on the 1-minute close that completes
a 15-minute candle with a new long/short signal.

    entry    the model's new signal (neighbour vote + volatility/regime filters
             + kernel direction), as on TradingView
    exits    4 x 15-minute candles (60 min, the model's horizon) or stop / target:
             stop 1.5 x ATR(14) of 15-min candles, target 1.5 x the stop
    costs    the usual scalper rules: spread + fees at most 25% of the risk

Warm-up: about 3 days of history (the regime filter needs 200 candles).
"""

from __future__ import annotations

from ..lorentzian import Lorentzian
from .bars import Bar, atr


def _feed(st: dict, b: Bar, tfs: int) -> dict | None:
    """Add one 1-minute candle; returns the model output if a 15-min candle just closed."""
    key = b.t // tfs
    cur = st.get("cur")
    out = None
    if cur is not None and cur[0] != key:                 # a gap: close the unfinished candle
        out = _close(st, cur, tfs)
        cur = None
    if cur is None:
        cur = [key, b.o, b.h, b.l, b.c]
    else:
        cur[2], cur[3], cur[4] = max(cur[2], b.h), min(cur[3], b.l), b.c
    if (b.t + 60) % tfs == 0:                             # this minute completes the candle
        out = _close(st, cur, tfs)
        cur = None
    st["cur"] = cur
    return out


def _close(st: dict, cur: list, tfs: int) -> dict:
    key, o, h, l, c = cur
    st.setdefault("tf_bars", []).append(Bar(key * tfs, o, h, l, c))
    st["tf_bars"] = st["tf_bars"][-60:]
    st["n"] = st.get("n", 0) + 1
    return st["model"].update(o, h, l, c)


def evaluate(bars: list[Bar], cfg, t: float, st: dict):
    from .strategy import Signal
    if not bars:
        return Signal(None, 0.0, "", None, "waiting for prices")
    tfs = cfg.lz_minutes * 60
    if "model" not in st:
        st["model"] = Lorentzian()
        st["last_t"] = None
    out = None
    i = len(bars)
    while i > 0 and (st["last_t"] is None or bars[i - 1].t > st["last_t"]):
        i -= 1                                             # find the candles not fed yet
    for b in bars[i:]:
        o = _feed(st, b, tfs)
        st["last_t"] = b.t
        if b is bars[-1]:
            out = o
    tf_bars = st.get("tf_bars", [])
    a = atr(tf_bars, 14) if len(tf_bars) > 15 else 0.0
    trend = ""
    if out is None:
        left = (tfs - (bars[-1].t + 60) % tfs) // 60
        return Signal(None, a, trend, None, f"waiting for the 15-min candle to close ({left} min)")
    trend = "up" if out["signal"] > 0 else "down" if out["signal"] < 0 else "flat"
    if not out["warm"] or st["n"] < 220:
        return Signal(None, a, trend, None, f"model warming up ({st['n']}/220 candles of {cfg.lz_minutes} min)")
    vote = f"vote {out['prediction']:+d}"
    if out["start_long"]:
        return Signal("BUY", a, trend, None, f"ML long signal ({vote}, kernel rising)")
    if out["start_short"]:
        return Signal("SELL", a, trend, None, f"ML short signal ({vote}, kernel falling)")
    why = []
    if not out["volatility_ok"]:
        why.append("volatility filter")
    if not out["regime_ok"]:
        why.append("regime filter")
    return Signal(None, a, trend, None, f"{vote}, holding {trend}" + (f" ({', '.join(why)} blocking)" if why else ""))
